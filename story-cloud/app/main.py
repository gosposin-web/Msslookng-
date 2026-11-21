import asyncio
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import re
import secrets
import tempfile
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .archive import ArchiveError, MAX_UPLOAD, extract_tdata
from .config import Config
from .db import DB, now
from .telegram import TelegramError, TelegramGateway, normalize_chat, api_credentials
from .worker import VIEW_LIMIT_24H, Worker
from .snapshot import SnapshotStore, SnapshotError
from .account_access import AccountAccessError, account_error
from .avatar import MAX_PHOTO_UPLOAD, prepare_avatar
from .diagnostics import import_diagnostic
from .chat_files import MAX_CHAT_FILE, ChatFileError, parse_chat_file
from .source_store import SourceStoreError, add_source as store_source, import_sources, control_import, start_scan
from .people_imports import (MAX_PEOPLE_FILE, PeopleImportError, PeopleList,
                             normalize_person, parse_people_file, queue_people)

STATIC = Path(__file__).parent / 'static'
COOKIE = 'story_cloud_session'


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        limit = (MAX_UPLOAD+128*1024 if scope['path']=='/api/account/tdata' else
                 MAX_CHAT_FILE+64*1024 if scope['path'] in ('/api/sources/import','/api/people/import') else
                 MAX_PHOTO_UPLOAD+64*1024 if scope['path']=='/api/account/profile/photo' else 128*1024)
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


class PersonInput(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    request_id: str = Field(default='', max_length=80)


class ControlInput(BaseModel):
    action: str


class NameInput(BaseModel):
    first_name: str = Field(max_length=64)
    last_name: str = Field(default='',max_length=64)


class UsernameInput(BaseModel):
    username: str = Field(default='',max_length=33)


def create_app(config=None, gateway_factory=TelegramGateway, run_worker=True):
    config=config or Config.from_env()
    config.validate()
    db=DB(config.data_dir/'story-cloud.sqlite3')
    cipher=config.cipher()
    gateway=gateway_factory(db,cipher)
    storage=SnapshotStore(db,config) if config.snapshot_site_url else None
    worker=Worker(db,gateway,storage)
    # An unprivileged readiness document may be embedded only by the configured
    # panel. The return URL is configuration, never a visitor-supplied URL.
    try:
        origin=urlsplit(config.public_origin)
        wake_origin=(config.public_origin.rstrip('/') if origin.scheme=='https' and origin.hostname
                     and re.fullmatch(r'[A-Za-z0-9.-]+',origin.hostname) and not origin.username
                     and not origin.password and not origin.port and origin.path in ('','/')
                     and not origin.query and not origin.fragment else '')
    except ValueError:
        wake_origin=''
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
        response.headers.setdefault('Cache-Control','no-store')
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path=='/wake' and wake_origin and response.status_code==200:
            # /wake contains only public readiness and a fixed return link.
            del response.headers['X-Frame-Options']
            response.headers['Content-Security-Policy']=response.headers['Content-Security-Policy'].replace("frame-ancestors 'none'",'frame-ancestors '+wake_origin)
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

    @app.get('/wake',response_class=Response)
    def wake():
        if not wake_origin:
            raise HTTPException(404,'Страница запуска не настроена.')
        return Response((STATIC/'wake.html').read_text().replace('__STORY_PANEL_ORIGIN__',html.escape(wake_origin,quote=True)),
                        media_type='text/html',headers={'Cache-Control':'no-store'})

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
        counts=db.one("SELECT COUNT(*) people,COALESCE(SUM(EXISTS(SELECT 1 FROM json_each(p.stories) s WHERE json_extract(s.value,'$.expires')>?)),0) active FROM people p",(now(),))
        sources=db.all('''SELECT s.*,(SELECT COUNT(*) FROM source_people sp WHERE sp.source_id=s.id) AS collected,
                          j.status scan_status,j.message scan_message,j.id scan_id
                          FROM sources s LEFT JOIN jobs j ON j.id=(SELECT id FROM jobs WHERE source_id=s.id AND kind='scan' ORDER BY created_at DESC,rowid DESC LIMIT 1)
                          ORDER BY s.created_at DESC,s.rowid DESC''')
        jobs=[]
        for row in db.all("SELECT * FROM jobs ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 WHEN 'queued' THEN 2 WHEN 'paused' THEN 3 ELSE 4 END,created_at DESC,rowid DESC LIMIT 600"):
            p=json.loads(row['payload'])
            item = db.public_job(row)|{'total':len(p.get('user_ids',[])) if row['kind']=='view' else row['collected'],
                                       'phase':p.get('phase','gather')}
            if row['kind']=='people_import':
                item.update(total=len(p.get('refs',[])), processed=p.get('position',0),
                            added=p.get('added',0), rejected=p.get('rejected',0),
                            duplicate_users=p.get('duplicate_users',0), import_errors=p.get('errors',[])[:20])
            jobs.append(item)
        cooldown=db.one("SELECT value FROM runtime WHERE key='telegram_wait_until'")
        job_counts={r['status']:r['n'] for r in db.all('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}
        imports=db.all('''SELECT i.id,i.created_at,COUNT(j.id) total,
                           SUM(j.status='queued') queued,SUM(j.status='running') running,
                           SUM(j.status='waiting') waiting,SUM(j.status='paused') paused,
                           SUM(j.status='done') done,SUM(j.status='failed') failed,SUM(j.status='cancelled') cancelled
                           FROM source_imports i JOIN source_import_jobs ij ON ij.import_id=i.id
                           JOIN jobs j ON j.id=ij.job_id GROUP BY i.id ORDER BY i.created_at DESC,i.rowid DESC LIMIT 5''')
        return {'account':db.public_account(),
                'sources':sources,'jobs':jobs,'events':db.all('SELECT * FROM events ORDER BY id DESC LIMIT 50'),
                'job_counts':job_counts,'job_total':sum(job_counts.values()),'source_imports':imports,
                'stats':{'people':counts['people'],'active':counts['active'],'views_24h':db.one('SELECT COUNT(*) n FROM views WHERE viewed_at>?',(now()-86400,))['n']},
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
        stage = 'archive_read'
        try:
            async with gateway.lock:
                check_telegram_cooldown()
                if storage:
                    await storage.flush()
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
                    stage = 'archive_extract'
                    folder=await asyncio.to_thread(extract_tdata,archive,extracted)
                    async with asyncio.timeout(90):
                        stage = 'gateway'
                        gateway.import_stage = 'desktop_read'
                        await gateway.import_tdata(folder,api_id,api_hash,passcode,password)
        except HTTPException:
            raise
        except ArchiveError as exc:
            return error_response(AccountAccessError('TDATA_ARCHIVE_INVALID',str(exc),
                                  'Загрузите ZIP с файлами авторизации вашего Telegram Desktop.'),import_stage=stage)
        except SnapshotError as exc:
            raise HTTPException(503,str(exc)) from None
        except Exception as exc:
            return error_response(exc,import_stage=getattr(gateway,'import_stage','unknown') if stage=='gateway' else stage)
        finally:
            await file.close()
        return {'ok':True}

    def check_telegram_cooldown():
        wait = db.one("SELECT value FROM runtime WHERE key='telegram_wait_until'")
        if wait and wait['value'] > now():
            raise AccountAccessError('RATE_LIMIT','Telegram попросил подождать.',
                                     'Повторите после завершения паузы Telegram.',retry_after=wait['value']-now())

    def error_response(exc, *, import_stage=''):
        error = account_error(exc)
        payload = error.payload()
        if import_stage:
            payload.update(import_diagnostic(exc,error.code,import_stage))
        if error.retry_after:
            db.execute("INSERT INTO runtime(key,value) VALUES('telegram_wait_until',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                       (now()+error.retry_after,))
        status = 429 if error.retry_after else 500 if error.code=='CLIENT_INTERNAL_ERROR' else 400
        return JSONResponse(payload,status_code=status,
                            headers={'Retry-After':str(error.retry_after)} if error.retry_after else {})

    async def profile_action(operation, *, mutation=False):
        need_account()
        if gateway.lock.locked():
            raise HTTPException(409,'Аккаунт занят другим действием. Дождитесь его окончания или приостановите задание.')
        try:
            async with gateway.lock:
                check_telegram_cooldown()
                if storage:
                    await storage.flush()
                async with asyncio.timeout(45):
                    profile = await operation()
                if mutation:
                    db.event('Профиль обновлён в Telegram.')
                # Profile reads update the durable cache too.
                if storage:
                    await storage.flush()
                return {'ok':True,'account':profile}
        except HTTPException:
            raise
        except SnapshotError as exc:
            raise HTTPException(503,str(exc)) from None
        except Exception as exc:
            return error_response(exc)

    @app.get('/api/account/profile')
    async def profile(login=Depends(authenticated)):
        return await profile_action(gateway.read_profile)

    @app.post('/api/account/profile/name')
    async def profile_name(body: NameInput,login=Depends(authenticated)):
        return await profile_action(lambda:gateway.change_name(body.first_name,body.last_name),mutation=True)

    @app.post('/api/account/profile/username')
    async def profile_username(body: UsernameInput,login=Depends(authenticated)):
        return await profile_action(lambda:gateway.change_username(body.username),mutation=True)

    @app.post('/api/account/profile/photo')
    async def profile_photo(file: UploadFile=File(...),login=Depends(authenticated)):
        need_account()
        try:
            data = await file.read(MAX_PHOTO_UPLOAD+1)
            photo,thumbnail = await asyncio.to_thread(prepare_avatar,data)
        except Exception as exc:
            return error_response(exc)
        finally:
            await file.close()
        return await profile_action(lambda:gateway.change_avatar(photo,thumbnail),mutation=True)

    @app.get('/api/account/profile/photo')
    def get_profile_photo(request: Request,login=Depends(authenticated)):
        row = db.one('SELECT photo,photo_revision FROM account_profile WHERE id=1')
        if not row or not row['photo']:
            raise HTTPException(404,'Аватарка не загружена.')
        etag = '"'+row['photo_revision']+'"'
        cache = {'ETag':etag,'Cache-Control':'private, max-age=300','Vary':'Cookie, X-Story-User'}
        if request.headers.get('if-none-match') == etag:
            return Response(status_code=304,headers=cache)
        return Response(row['photo'],media_type='image/jpeg',headers=cache)

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
                c.execute('DELETE FROM source_imports')
                c.execute('DELETE FROM people_imports')
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
        try:
            return {'id':store_source(db,ref,body.mode,body.max_users)}
        except SourceStoreError as exc:
            raise HTTPException(exc.status,str(exc)) from None

    @app.post('/api/sources/import')
    async def import_chats(file: UploadFile=File(...),mode: str=Form('members'),
                           max_users: int=Form(300),start_scans: bool=Form(True),request_id: str=Form(''),
                           login=Depends(authenticated)):
        try:
            if mode not in ('members','authors') or not 1<=max_users<=5000:
                raise HTTPException(400,'Выберите режим сбора и лимит от 1 до 5000 человек на каждый чат.')
            data=await file.read(MAX_CHAT_FILE+1)
            parsed=await asyncio.to_thread(parse_chat_file,data,file.filename or '')
            if storage:
                await storage.flush()
            return import_sources(db,parsed,data,mode,max_users,start_scans,request_id or uuid.uuid4().hex)
        except ChatFileError as exc:
            raise HTTPException(400,str(exc)) from None
        except SourceStoreError as exc:
            raise HTTPException(exc.status,str(exc)) from None
        except SnapshotError as exc:
            raise HTTPException(503,str(exc)) from None
        finally:
            await file.close()

    @app.post('/api/source-imports/{import_id}/control')
    def control_chat_import(import_id: str,body: ControlInput,login=Depends(authenticated)):
        try:
            return {'ok':True,'changed':control_import(db,import_id,body.action)}
        except SourceStoreError as exc:
            raise HTTPException(exc.status,str(exc)) from None

    @app.delete('/api/sources/{source_id}')
    def delete_source(source_id: str,login=Depends(authenticated)):
        if db.one("SELECT id FROM jobs WHERE source_id=? AND status IN ('queued','running','waiting','paused')",(source_id,)):
            raise HTTPException(409,'Сначала остановите задание этого чата.')
        db.execute('DELETE FROM sources WHERE id=?',(source_id,))
        return {'ok':True}

    @app.post('/api/sources/{source_id}/scan')
    def scan(source_id: str,login=Depends(authenticated)):
        try:
            return {'id':start_scan(db,source_id)}
        except SourceStoreError as exc:
            raise HTTPException(exc.status,str(exc)) from None

    @app.post('/api/people')
    def add_person(body: PersonInput,login=Depends(authenticated)):
        try:
            ref=normalize_person(body.username)
            return queue_people(db,PeopleList([ref],0,[],0),ref.encode(),body.request_id or uuid.uuid4().hex)
        except PeopleImportError as exc:
            raise HTTPException(exc.status,str(exc)) from None

    @app.post('/api/people/import')
    async def import_people(file: UploadFile=File(...),request_id: str=Form(''),login=Depends(authenticated)):
        try:
            data=await file.read(MAX_PEOPLE_FILE+1)
            parsed=await asyncio.to_thread(parse_people_file,data,file.filename or '')
            if storage:
                await storage.flush()
            return queue_people(db,parsed,data,request_id or uuid.uuid4().hex)
        except PeopleImportError as exc:
            raise HTTPException(exc.status,str(exc)) from None
        except SnapshotError as exc:
            raise HTTPException(503,str(exc)) from None
        finally:
            await file.close()

    def people_query(search='',source='',active=False):
        sql='SELECT DISTINCT p.* FROM people p'
        args=[]
        filters=[]
        if source=='manual':
            filters.append("EXISTS(SELECT 1 FROM job_people jp JOIN jobs j ON j.id=jp.job_id WHERE jp.user_id=p.user_id AND j.kind='people_import')")
        elif source:
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
        ids=list(dict.fromkeys(body.user_ids))
        if not all(re.fullmatch(r'[0-9]{1,20}',uid) for uid in ids):
            raise HTTPException(400,'Неверный список пользователей.')
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT id FROM account WHERE id=1').fetchone():
                raise HTTPException(409,'Сначала подключите аккаунт.')
            if c.execute("SELECT id FROM jobs WHERE kind='view' AND status IN ('queued','running','waiting','paused')").fetchone():
                raise HTTPException(409,'Сначала завершите или остановите текущее задание просмотра.')
            selected=[]
            for uid in ids:
                person=c.execute('SELECT * FROM people WHERE user_id=?',(uid,)).fetchone()
                if person and db.public_person(dict(person))['stories_count']:
                    selected.append(uid)
            if not selected:
                raise HTTPException(400,'У выбранных пользователей сейчас нет проверенных активных историй.')
            job_id=uuid.uuid4().hex
            c.execute("INSERT INTO jobs(id,kind,status,payload,created_at,updated_at) VALUES(?,'view','queued',?,?,?)",
                      (job_id,json.dumps({'user_ids':selected,'position':0,'delay':body.delay}),now(),now()))
            c.execute('INSERT INTO events(job_id,level,message,created_at) VALUES(?,?,?,?)',(job_id,'info','Задание добавлено в очередь.',now()))
            return {'id':job_id}

    @app.post('/api/jobs/{job_id}/control')
    def control(job_id: str,body: ControlInput,login=Depends(authenticated)):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            job=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not job:
                raise HTTPException(404,'Задание не найдено.')
            action=body.action
            if action=='pause' and job['status'] in ('queued','running','waiting'):
                c.execute("UPDATE jobs SET status='paused',message='Приостановлено пользователем.',updated_at=? WHERE id=?",(now(),job_id))
            elif action=='cancel' and job['status'] in ('queued','running','waiting','paused','failed'):
                c.execute("UPDATE jobs SET status='cancelled',message='Остановлено пользователем.',updated_at=? WHERE id=?",(now(),job_id))
            elif action=='resume' and job['status'] in ('paused','failed'):
                if not c.execute('SELECT id FROM account WHERE id=1').fetchone():
                    raise HTTPException(409,'Сначала подключите аккаунт.')
                if job['kind']=='scan' and c.execute("SELECT id FROM jobs WHERE source_id=? AND id<>? AND kind='scan' AND status IN ('queued','running','waiting','paused')",(job['source_id'],job_id)).fetchone():
                    raise HTTPException(409,'Для этого чата уже есть другой незавершённый сбор.')
                payload=json.loads(job['payload'])
                if payload.get('uncertain'):
                    payload['position']=payload.get('position',0)+1
                    payload.pop('uncertain')
                    c.execute('INSERT INTO events(job_id,level,message,created_at) VALUES(?,?,?,?)',
                              (job_id,'warning','Истории с неподтверждённым результатом пропущены при продолжении.',now()))
                c.execute('UPDATE jobs SET status=?,payload=?,message=?,retries=0,updated_at=? WHERE id=?',
                          ('waiting' if job['next_run']>now() else 'queued',json.dumps(payload),'Задание продолжено.',now(),job_id))
            else:
                raise HTTPException(409,'Состояние задания уже изменилось. Обновите страницу.')
        return {'ok':True}

    app.mount('/assets',StaticFiles(directory=STATIC),name='assets')

    @app.get('/')
    def index():
        return FileResponse(STATIC/'index.html')

    return app
