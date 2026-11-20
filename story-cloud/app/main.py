import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import tempfile
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .archive import ArchiveError, MAX_UPLOAD, extract_tdata
from .config import Config
from .db import DB, now
from .telegram import TelegramError, TelegramGateway, friendly_error, normalize_chat, api_credentials
from .worker import VIEW_LIMIT_24H, Worker
from .snapshot import SnapshotStore, SnapshotError

STATIC = Path(__file__).parent / 'static'
COOKIE = 'story_cloud_session'


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        limit = MAX_UPLOAD+128*1024 if scope['path']=='/api/account/tdata' else 128*1024
        headers=dict(scope.get('headers',[]))
        try:
            content_length=int(headers.get(b'content-length',b'0'))
        except ValueError:
            return await JSONResponse({'detail':'Некорректная длина запроса.'},status_code=400)(scope,receive,send)
        if content_length>limit:
            return await JSONResponse({'detail':'Размер запроса превышает лимит.'},status_code=413)(scope,receive,send)
        total=0
        started=False

        async def limited_receive():
            nonlocal total
            message=await receive()
            total+=len(message.get('body',b''))
            if total>limit:
                raise HTTPException(413,'Размер запроса превышает лимит.')
            return message

        async def guarded_send(message):
            nonlocal started
            if message['type']=='http.response.start':
                started=True
            await send(message)
        try:
            await self.app(scope,limited_receive,guarded_send)
        except HTTPException as exc:
            if not started:
                await JSONResponse({'detail':exc.detail},status_code=exc.status_code)(scope,receive,send)
            else:
                raise


class LoginInput(BaseModel):
    password: str = Field(min_length=1,max_length=512)


class SourceInput(BaseModel):
    ref: str = Field(min_length=1,max_length=200)
    mode: str = 'members'
    max_users: int = Field(default=300,ge=1,le=5000)


class ViewInput(BaseModel):
    user_ids: list[str] = Field(min_length=1,max_length=500)
    delay: int = Field(default=15,ge=5,le=300)


class ControlInput(BaseModel):
    action: str


def create_app(config=None, gateway_factory=TelegramGateway, run_worker=True):
    config=config or Config.from_env()
    config.validate()
    db=DB(config.data_dir/'story-cloud.sqlite3')
    cipher=config.cipher()
    gateway=gateway_factory(db,cipher)
    storage=SnapshotStore(db,config) if config.snapshot_site_url else None
    worker=Worker(db,gateway,storage)
    logging.getLogger('telethon').setLevel(logging.CRITICAL)
    logging.getLogger('opentele').setLevel(logging.CRITICAL)

    @asynccontextmanager
    async def lifespan(app):
        storage_task=asyncio.create_task(storage.run(gateway.close)) if storage else None
        task=asyncio.create_task(worker.run()) if run_worker else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await gateway.close()
            if storage_task:
                storage_task.cancel()
                with suppress(asyncio.CancelledError):
                    await storage_task
                await storage.close()

    app=FastAPI(title='Лупа',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.db=db
    app.state.gateway=gateway
    app.state.worker=worker
    app.state.storage=storage
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware('http')
    async def headers(request,call_next):
        if storage and not storage.ready and request.url.path.startswith('/api/') and request.url.path!='/api/auth/me':
            return JSONResponse({'detail':'Бесплатный сервер восстанавливается. Подождите около минуты.'},status_code=503)
        if request.url.path.startswith('/api/') and request.url.path!='/api/auth/login' and request.method not in ('GET','HEAD','OPTIONS'):
            try:
                authenticated(request)
            except HTTPException as exc:
                return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
        response=await call_next(request)
        if storage and request.url.path.startswith('/api/') and request.method not in ('GET','HEAD','OPTIONS') and response.status_code<400:
            try:
                await storage.flush()
            except SnapshotError as exc:
                return JSONResponse({'detail':str(exc)},status_code=503)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if config.cookie_secure:
            response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    def check_origin(request):
        origin=request.headers.get('origin')
        expected=config.public_origin or str(request.base_url).rstrip('/')
        if origin and origin!=expected:
            raise HTTPException(403,'Запрос с другого сайта отклонён.')

    def authenticated(request: Request):
        bridge=request.headers.get('x-story-bridge-secret','')
        bridge_user=request.headers.get('x-story-user','')
        if config.bridge_secret and bridge_user and hmac.compare_digest(bridge,config.bridge_secret):
            token=hmac.new(config.bridge_secret.encode(),bridge_user.encode(),hashlib.sha256).hexdigest()
            login={'csrf':token,'token_hash':'bridge','expires':now()+43200}
            if request.method not in ('GET','HEAD','OPTIONS'):
                check_origin(request)
                if not hmac.compare_digest(request.headers.get('x-csrf-token',''),token):
                    raise HTTPException(403,'Обновите страницу и повторите действие.')
            return login
        token=request.cookies.get(COOKIE,'')
        login=db.one('SELECT * FROM logins WHERE token_hash=? AND expires>?',
                     (hashlib.sha256(token.encode()).hexdigest(),now())) if token else None
        if not login:
            raise HTTPException(401,'Войдите в панель.')
        if request.method not in ('GET','HEAD','OPTIONS'):
            check_origin(request)
            if not hmac.compare_digest(request.headers.get('x-csrf-token',''),login['csrf']):
                raise HTTPException(403,'Обновите страницу и повторите действие.')
        return login

    def need_account():
        if not db.account():
            raise HTTPException(409,'Сначала подключите аккаунт во вкладке «Аккаунт».')

    @app.get('/health')
    def health():
        return {'ok':True,'service':'story-cloud','ready':not storage or storage.ready,'mode':'free' if config.free_mode else 'standard',
                'api_mode':'custom' if config.telegram_api_id and config.telegram_api_hash else 'public' if config.public_api_defaults else 'required'}

    @app.post('/api/auth/login')
    def login(body: LoginInput, request: Request, response: Response):
        check_origin(request)
        # Password and request bodies are never written to logs or application state.
        ip=hashlib.sha256(((request.client.host if request.client else 'unknown')+config.encryption_secret).encode()).hexdigest()
        db.execute('DELETE FROM login_failures WHERE created_at<?',(now()-900,))
        if db.one('SELECT COUNT(*) n FROM login_failures WHERE ip=?',(ip,))['n']>=5:
            raise HTTPException(429,'Слишком много попыток. Подождите 15 минут.')
        if not hmac.compare_digest(hashlib.sha256(body.password.encode()).digest(),hashlib.sha256(config.admin_password.encode()).digest()):
            db.execute('INSERT INTO login_failures VALUES(?,?)',(ip,now()))
            raise HTTPException(401,'Неверный пароль панели.')
        token=secrets.token_urlsafe(32)
        csrf=secrets.token_urlsafe(32)
        db.execute('DELETE FROM logins WHERE expires<=?',(now(),))
        db.execute('INSERT INTO logins VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,now()+43200))
        response.set_cookie(COOKIE,token,httponly=True,secure=config.cookie_secure,samesite='strict',max_age=43200,path='/')
        return {'csrf':csrf}

    @app.get('/api/auth/me')
    def me(login=Depends(authenticated)):
        return {'csrf':login['csrf']}

    @app.post('/api/auth/logout')
    def logout(request: Request,response: Response,login=Depends(authenticated)):
        db.execute('DELETE FROM logins WHERE token_hash=?',(login['token_hash'],))
        response.delete_cookie(COOKIE,path='/',secure=config.cookie_secure,httponly=True,samesite='strict')
        return {'ok':True}

    @app.get('/api/state')
    def state(login=Depends(authenticated)):
        account=db.account()
        people=db.all('SELECT stories FROM people')
        active=sum(any(s['expires']>now() for s in json.loads(p['stories'])) for p in people)
        sources=db.all('''SELECT s.*,(SELECT COUNT(*) FROM source_people sp WHERE sp.source_id=s.id) AS collected
                          FROM sources s ORDER BY created_at DESC''')
        jobs=[]
        for row in db.all('SELECT * FROM jobs ORDER BY created_at DESC,rowid DESC LIMIT 50'):
            p=json.loads(row['payload'])
            jobs.append(db.public_job(row)|{'total':len(p.get('user_ids',[])) if row['kind']=='view' else row['collected'],
                                           'phase':p.get('phase','gather')})
        cooldown=db.one("SELECT value FROM runtime WHERE key='telegram_wait_until'")
        return {'account':{k:account[k] for k in ('user_id','name','username','created_at')} if account else None,
                'sources':sources,'jobs':jobs,'events':db.all('SELECT * FROM events ORDER BY id DESC LIMIT 50'),
                'stats':{'people':len(people),'active':active,'views_24h':db.one('SELECT COUNT(*) n FROM views WHERE viewed_at>?',(now()-86400,))['n']},
                'view_limit_24h':VIEW_LIMIT_24H,'cooldown_until':cooldown['value'] if cooldown else 0,
                'telegram_api_configured':bool(config.telegram_api_id and config.telegram_api_hash) or config.public_api_defaults,
                'telegram_api_mode':'custom' if config.telegram_api_id and config.telegram_api_hash else 'public' if config.public_api_defaults else 'required',
                'auth_mode':'chatgpt' if config.bridge_secret else 'password',
                'free_mode':config.free_mode,'storage_error':storage.error if storage else ''}

    @app.post('/api/account/tdata')
    async def import_tdata(request: Request,file: UploadFile=File(...),api_id: int=Form(0),
                           api_hash: str=Form(''),passcode: str=Form(''),password: str=Form(''),
                           login=Depends(authenticated)):
        if db.account():
            raise HTTPException(409,'Сначала отключите текущий аккаунт.')
        if gateway.lock.locked():
            raise HTTPException(409,'Другое действие ещё выполняется. Попробуйте позже.')
        try:
            api_id,api_hash=api_credentials(config,api_id,api_hash)
        except TelegramError as exc:
            raise HTTPException(400,str(exc)) from None
        if len(passcode)>512 or len(password)>512:
            raise HTTPException(400,'Слишком длинный пароль.')
        try:
            async with gateway.lock:
                with tempfile.TemporaryDirectory(prefix='story-tdata-') as temp:
                    root=Path(temp)
                    archive=root/'account.zip'
                    size=0
                    with open(archive,'wb') as dst:
                        archive.chmod(0o600)
                        while chunk:=await file.read(64*1024):
                            size+=len(chunk)
                            if size>MAX_UPLOAD:
                                raise HTTPException(413,'Максимальный размер ZIP — 20 МБ.')
                            dst.write(chunk)
                    extracted=root/'tdata'
                    extracted.mkdir(mode=0o700)
                    folder=await asyncio.to_thread(extract_tdata,archive,extracted)
                    async with asyncio.timeout(90):
                        await gateway.import_tdata(folder,api_id,api_hash,passcode,password)
        except HTTPException:
            raise
        except ArchiveError as exc:
            raise HTTPException(400,str(exc)) from None
        except Exception as exc:
            raise HTTPException(400,friendly_error(exc)) from None
        finally:
            await file.close()
        return {'ok':True}

    @app.delete('/api/account')
    async def disconnect(login=Depends(authenticated)):
        # Cancel first. Once the current RPC ends, the lock protects session revocation.
        db.execute("UPDATE jobs SET status='cancelled',message='Аккаунт отключён.' WHERE status IN ('queued','running','waiting','paused')")
        revoked=False
        async with gateway.lock:
            if db.account():
                try:
                    async with asyncio.timeout(15):
                        client=await gateway.client_for_account()
                        await client.log_out()
                        revoked=True
                except Exception:
                    pass
            await gateway.close()
            with db.connect() as c:
                c.execute('DELETE FROM account')
                c.execute('DELETE FROM people')
                c.execute('DELETE FROM views')
                c.execute('DELETE FROM view_attempts')
                c.execute('DELETE FROM runtime')
            db.event('Аккаунт отключён. Локальные данные сессии и аудитории удалены.')
        return {'ok':True,'revoked':revoked,'message':('Облачная сессия завершена.' if revoked else 'Данные удалены. Завершите сессию Story Cloud в настройках Telegram, если она осталась активной.')}

    @app.post('/api/sources')
    def add_source(body: SourceInput,login=Depends(authenticated)):
        try:
            ref=normalize_chat(body.ref)
        except TelegramError as exc:
            raise HTTPException(400,str(exc)) from None
        if body.mode not in ('members','authors'):
            raise HTTPException(400,'Выберите участников или авторов сообщений.')
        if db.one('SELECT id FROM sources WHERE ref=? COLLATE NOCASE',(ref,)):
            raise HTTPException(409,'Этот чат уже добавлен.')
        if db.one('SELECT COUNT(*) n FROM sources')['n']>=50:
            raise HTTPException(400,'Максимум 50 источников.')
        source_id=uuid.uuid4().hex
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',(source_id,ref,ref,body.mode,body.max_users,now()))
        return {'id':source_id}

    @app.delete('/api/sources/{source_id}')
    def delete_source(source_id: str,login=Depends(authenticated)):
        if db.one("SELECT id FROM jobs WHERE source_id=? AND status IN ('queued','running','waiting','paused')",(source_id,)):
            raise HTTPException(409,'Сначала остановите задание этого чата.')
        db.execute('DELETE FROM sources WHERE id=?',(source_id,))
        return {'ok':True}

    @app.post('/api/sources/{source_id}/scan')
    def scan(source_id: str,login=Depends(authenticated)):
        need_account()
        if not db.one('SELECT id FROM sources WHERE id=?',(source_id,)):
            raise HTTPException(404,'Чат не найден.')
        if db.one("SELECT id FROM jobs WHERE source_id=? AND status IN ('queued','running','waiting','paused')",(source_id,)):
            raise HTTPException(409,'Для этого чата уже есть незавершённое задание.')
        return {'id':db.create_job('scan',{'phase':'gather','cursor':0},source_id)}

    def people_query(search='',source='',active=False):
        sql='SELECT DISTINCT p.* FROM people p'
        args=[]
        filters=[]
        if source:
            sql+=' JOIN source_people sp ON sp.user_id=p.user_id'
            filters.append('sp.source_id=?')
            args.append(source)
        if search:
            filters.append('(p.name LIKE ? OR p.username LIKE ? OR p.user_id=?)')
            args.extend(['%'+search[:100]+'%','%'+search[:100]+'%',search[:100]])
        if active:
            filters.append("EXISTS(SELECT 1 FROM json_each(p.stories) s WHERE json_extract(s.value,'$.expires')>?)")
            args.append(now())
        if filters:
            sql+=' WHERE '+' AND '.join(filters)
        return sql+' ORDER BY p.checked_at DESC,p.user_id',args

    @app.get('/api/people')
    def people(search: str='',source: str='',active: bool=True,offset: int=0,login=Depends(authenticated)):
        if offset<0 or offset>100000:
            raise HTTPException(400,'Недопустимое смещение.')
        sql,args=people_query(search,source,active)
        total=db.one('SELECT COUNT(*) n FROM ('+sql+')',args)['n']
        rows=db.all(sql+' LIMIT 100 OFFSET ?',[*args,offset])
        account=db.account()
        result=[]
        for row in rows:
            item=db.public_person(row)
            item['viewed_count']=len(db.viewed_ids(account['user_id'],row['user_id'])) if account else 0
            result.append(item)
        return {'people':result,'total':total,'offset':offset}

    @app.get('/api/people/export')
    def export(search: str='',source: str='',active: bool=True,login=Depends(authenticated)):
        sql,args=people_query(search,source,active)
        out=io.StringIO()
        writer=csv.writer(out)
        writer.writerow(['user_id','name','username','active_stories','checked_at'])
        def safe(value):
            value=str(value or '')
            return "'"+value if value.startswith(('=','+','-','@','\t','\r','\n')) else value
        for row in db.all(sql,args):
            p=db.public_person(row)
            writer.writerow([p['user_id'],safe(p['name']),safe(p['username']),p['stories_count'],p['checked_at']])
        return Response(out.getvalue().encode('utf-8-sig'),media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition':'attachment; filename="story-audience.csv"'})

    @app.post('/api/jobs/view')
    def view(body: ViewInput,login=Depends(authenticated)):
        need_account()
        if db.one("SELECT id FROM jobs WHERE kind='view' AND status IN ('queued','running','waiting','paused')"):
            raise HTTPException(409,'Сначала завершите или остановите текущее задание просмотра.')
        ids=list(dict.fromkeys(body.user_ids))
        if not all(re.fullmatch(r'[0-9]{1,20}',uid) for uid in ids):
            raise HTTPException(400,'Неверный список пользователей.')
        selected=[]
        for uid in ids:
            person=db.one('SELECT * FROM people WHERE user_id=?',(uid,))
            if person and db.public_person(person)['stories_count']:
                selected.append(uid)
        if not selected:
            raise HTTPException(400,'У выбранных пользователей сейчас нет проверенных активных историй.')
        return {'id':db.create_job('view',{'user_ids':selected,'position':0,'delay':body.delay})}

    @app.post('/api/jobs/{job_id}/control')
    def control(job_id: str,body: ControlInput,login=Depends(authenticated)):
        job=db.job(job_id)
        if not job:
            raise HTTPException(404,'Задание не найдено.')
        action=body.action
        if action=='pause' and job['status'] in ('queued','running','waiting'):
            db.update_job(job_id,status='paused',message='Приостановлено пользователем.')
        elif action=='cancel' and job['status'] in ('queued','running','waiting','paused','failed'):
            db.update_job(job_id,status='cancelled',message='Остановлено пользователем.')
        elif action=='resume' and job['status'] in ('paused','failed'):
            need_account()
            payload=job['payload']
            if payload.get('uncertain'):
                payload['position']=payload.get('position',0)+1
                payload.pop('uncertain')
                db.event('Истории с неподтверждённым результатом пропущены при продолжении.',job_id,'warning')
            db.update_job(job_id,status='waiting' if job['next_run']>now() else 'queued',
                           payload=payload,message='Задание продолжено.',retries=0)
        else:
            raise HTTPException(409,'Состояние задания уже изменилось. Обновите страницу.')
        return {'ok':True}

    app.mount('/assets',StaticFiles(directory=STATIC),name='assets')

    @app.get('/')
    def index():
        return FileResponse(STATIC/'index.html')

    return app
