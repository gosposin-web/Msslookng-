"""Atomic source imports and their independent scan queues."""
import hashlib
import json
import re
import uuid

from .chat_files import ref_key
from .db import now

MAX_SOURCES=500
ACTIVE_SCANS=('queued','running','waiting','paused')


class SourceStoreError(ValueError):
    def __init__(self,message,status=400):
        super().__init__(message)
        self.status=status


def add_source(db,ref,mode,max_users):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        refs={ref_key(r['ref']) for r in c.execute('SELECT ref FROM sources')}
        if ref_key(ref) in refs:
            raise SourceStoreError('Этот чат уже добавлен.',409)
        if len(refs)>=MAX_SOURCES:
            raise SourceStoreError(f'В панели допускается до {MAX_SOURCES} чатов.')
        source_id=uuid.uuid4().hex
        c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',(source_id,ref,ref,mode,max_users,now()))
    return source_id


def import_sources(db,parsed,data,mode,max_users,start_scans,request_id,*,auto_view=False,delay=15):
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id):
        raise SourceStoreError('Обновите форму импорта и повторите загрузку.')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        account=c.execute('SELECT user_id FROM account WHERE id=1').fetchone()
        identity=account['user_id'] if account and start_scans else ''
        settings=[mode,max_users,start_scans,identity]
        if auto_view:settings+=['masslook-v1',delay]
        options=json.dumps(settings,ensure_ascii=True)
        fingerprint=hashlib.sha256(data+b'\x00'+options.encode()).hexdigest()
        previous=c.execute('SELECT fingerprint,result FROM source_imports WHERE id=?',(request_id,)).fetchone()
        if previous:
            if previous['fingerprint']!=fingerprint:
                raise SourceStoreError('Этот запрос уже использован для другого файла или настроек. Откройте новую форму.',409)
            return json.loads(previous['result'])|{'replayed':True}
        if start_scans and not account:
            raise SourceStoreError('Сначала подключите аккаунт или отключите запуск сбора после импорта.',409)
        existing={ref_key(r['ref']):dict(r) for r in c.execute('SELECT * FROM sources')}
        added=sum(ref_key(ref) not in existing for ref in parsed.refs)
        if len(existing)+added>MAX_SOURCES:
            raise SourceStoreError(f'В панели допускается до {MAX_SOURCES} чатов. Сейчас {len(existing)}, новых в файле {added}.')
        stamp=now();queued=0;busy=0
        c.execute('INSERT INTO source_imports VALUES(?,?,?,?)',(request_id,fingerprint,'{}',stamp))
        for ref in parsed.refs:
            source=existing.get(ref_key(ref))
            source_id=source['id'] if source else uuid.uuid4().hex
            if not source:
                c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',(source_id,ref,ref,mode,max_users,stamp))
            if not start_scans:
                continue
            running=c.execute("SELECT id FROM jobs WHERE source_id=? AND kind IN ('scan','masslook') AND status IN ('queued','running','waiting','paused')",(source_id,)).fetchone()
            if running:
                busy+=1
                continue
            # File settings apply only when this chat has no unfinished scan.
            c.execute('UPDATE sources SET mode=?,max_users=? WHERE id=?',(mode,max_users,source_id))
            job_id=uuid.uuid4().hex
            kind='masslook' if auto_view else 'scan'
            payload={'phase':'gather','cursor':0}
            if auto_view:payload.update(user_ids=[],position=0,delay=delay,auto_react=True)
            c.execute("INSERT INTO jobs(id,kind,source_id,status,payload,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?)",
                      (job_id,kind,source_id,json.dumps(payload),stamp,stamp))
            c.execute('INSERT INTO source_import_jobs VALUES(?,?)',(request_id,job_id))
            queued+=1
        result={'ok':True,'import_id':request_id,'valid':len(parsed.refs),'added':added,
                'existing':len(parsed.refs)-added,'queued':queued,'busy':busy,
                'duplicates':parsed.duplicates,'invalid_count':parsed.invalid_count,'invalid_rows':parsed.invalid,
                'replayed':False}
        c.execute('UPDATE source_imports SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),request_id))
        c.execute('INSERT INTO events(level,message,created_at) VALUES(?,?,?)',
                  ('info',f'Импорт чатов: добавлено {added}, поставлено в очередь {queued}, уже есть сбор {busy}.',stamp))
        c.execute('DELETE FROM source_imports WHERE created_at<?',(stamp-7*86400,))
        c.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)')
        return result


def start_scan(db,source_id,*,auto_view=False,delay=15):
    if type(delay) is not int or not 5<=delay<=300:
        raise SourceStoreError('Интервал просмотра — от 5 до 300 секунд.')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT id FROM account WHERE id=1').fetchone():
            raise SourceStoreError('Сначала подключите аккаунт.',409)
        if not c.execute('SELECT id FROM sources WHERE id=?',(source_id,)).fetchone():
            raise SourceStoreError('Чат не найден.',404)
        if c.execute("SELECT id FROM jobs WHERE source_id=? AND status IN ('queued','running','waiting','paused')",(source_id,)).fetchone():
            raise SourceStoreError('Для этого чата уже есть незавершённое задание.',409)
        job_id=uuid.uuid4().hex;stamp=now()
        kind='masslook' if auto_view else 'scan'
        payload={'phase':'gather','cursor':0}
        if auto_view:payload.update(user_ids=[],position=0,delay=delay,auto_react=True)
        c.execute("INSERT INTO jobs(id,kind,source_id,status,payload,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?)",
                  (job_id,kind,source_id,json.dumps(payload),stamp,stamp))
        c.execute('INSERT INTO events(job_id,level,message,created_at) VALUES(?,?,?,?)',(job_id,'info','Задание добавлено в очередь.',stamp))
        return job_id


def control_import(db,import_id,action):
    if action not in ('pause','resume','cancel'):
        raise SourceStoreError('Выберите паузу, продолжение или остановку импорта.')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT id FROM source_imports WHERE id=?',(import_id,)).fetchone():
            raise SourceStoreError('Импорт не найден.',404)
        if action=='resume' and not c.execute('SELECT id FROM account WHERE id=1').fetchone():
            raise SourceStoreError('Сначала подключите аккаунт.',409)
        members='id IN (SELECT job_id FROM source_import_jobs WHERE import_id=?)'
        if action=='pause':
            changed=c.execute(f"UPDATE jobs SET status='paused',message='Импорт приостановлен пользователем.',updated_at=? WHERE {members} AND status IN ('queued','running','waiting')",(now(),import_id)).rowcount
        elif action=='cancel':
            changed=c.execute(f"UPDATE jobs SET status='cancelled',message='Импорт остановлен пользователем.',updated_at=? WHERE {members} AND status IN ('queued','running','waiting','paused','failed')",(now(),import_id)).rowcount
        else:
            wait=c.execute("SELECT value FROM runtime WHERE key='telegram_wait_until'").fetchone()
            deadline=wait['value'] if wait else 0
            changed=c.execute(f"UPDATE jobs SET status=CASE WHEN MAX(next_run,?)>? THEN 'waiting' ELSE 'queued' END,next_run=MAX(next_run,?),retries=0,message='Импорт продолжен.',updated_at=? WHERE {members} AND status IN ('paused','failed') AND NOT EXISTS(SELECT 1 FROM jobs other WHERE other.source_id=jobs.source_id AND other.id<>jobs.id AND other.kind IN ('scan','masslook') AND other.status IN ('queued','running','waiting','paused'))",
                              (deadline,now(),deadline,now(),import_id)).rowcount
        if not changed:
            raise SourceStoreError('Состояние заданий уже изменилось. Обновите страницу.',409)
        return changed
