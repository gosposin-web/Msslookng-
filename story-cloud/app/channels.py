"""Idempotent creation and explicit partial results for owned channels."""
import asyncio
import hashlib
import json
import re

from telethon.errors import FloodWaitError
from .account_access import AccountAccessError, account_error
from .db import now
from .snapshot import SnapshotError


def public_channel(row):
    return {key:row[key] for key in ('id','title','username','created_at')}


async def create_channel(db,gateway,storage,title,about,username,pin,request_id):
    title=title.strip()
    username=username.strip().removeprefix('@')
    if not title or len(title)>128 or '\x00' in title+about:
        raise AccountAccessError('CHANNEL_TITLE_INVALID','Название обязательно, до 128 символов.')
    if len(about)>255:
        raise AccountAccessError('CHANNEL_ABOUT_TOO_LONG','Описание канала — до 255 символов.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id):
        raise AccountAccessError('REQUEST_INVALID','Обновите форму создания канала.')
    account=db.account()
    if not account:
        raise AccountAccessError('ACCOUNT_REQUIRED','Сначала подключите аккаунт.')
    fingerprint=hashlib.sha256(json.dumps([account['user_id'],title,about,username,pin],ensure_ascii=False).encode()).hexdigest()
    previous=db.one('SELECT * FROM channel_requests WHERE id=?',(request_id,))
    channel=None
    if previous:
        if previous['fingerprint']!=fingerprint:
            raise AccountAccessError('REQUEST_CONFLICT','Этот запрос уже относится к другим настройкам канала.')
        if previous['status']=='done':
            return json.loads(previous['result'])|{'replayed':True}
        if previous['status'] in ('creating','uncertain'):
            raise AccountAccessError('CHANNEL_CREATE_UNCERTAIN','Канал мог быть создан, но подтверждение не получено.',
                                     'Проверьте каналы в Telegram. Повтор этого запроса не создаст второй канал.')
        if previous['status']=='created':
            channel=db.one('SELECT * FROM channels WHERE id=?',(json.loads(previous['result'])['channel_id'],))
            if not channel:
                raise AccountAccessError('CHANNEL_RESULT_MISSING','Результат создания недоступен. Проверьте Telegram.')
    if channel is None:
        await gateway.check_channel_username(username)
        db.execute("INSERT INTO channel_requests VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status='creating'",
                   (request_id,fingerprint,'{}','creating',now()))
        if storage:
            await storage.flush()
        generation=getattr(storage,'restore_generation',0)
        try:
            channel=await gateway.create_channel(title,about)
            if storage and (not storage.ready or generation!=storage.restore_generation):
                raise SnapshotError('Канал мог быть создан. Проверьте Telegram перед повтором.')
        except FloodWaitError:
            db.execute("UPDATE channel_requests SET status='retryable' WHERE id=?",(request_id,))
            raise
        except BaseException as exc:
            if generation==getattr(storage,'restore_generation',0):
                db.execute("UPDATE channel_requests SET status='uncertain' WHERE id=?",(request_id,))
            if isinstance(exc,(asyncio.CancelledError,SnapshotError)):
                raise
            raise AccountAccessError('CHANNEL_CREATE_UNCERTAIN','Результат создания канала не подтверждён.',
                                     'Проверьте каналы в Telegram. Повторная отправка создания исключена.') from None
        channel=channel|{'created_at':now()}
        with db.connect() as c:
            c.execute('INSERT INTO channels(id,title,username,peer,created_at) VALUES(?,?,?,?,?)',
                      (channel['id'],channel['title'],channel.get('username'),channel['peer'],channel['created_at']))
            c.execute("UPDATE channel_requests SET status='created',result=? WHERE id=?",
                      (json.dumps({'channel_id':channel['id']}),request_id))
        if storage:
            await storage.flush()
    warning=''
    pinned=False
    try:
        await gateway.channel_username(channel['id'],username)
        if pin:
            await gateway.pin_channel(channel['id'])
            pinned=True
    except Exception as exc:
        error=account_error(exc)
        if error.retry_after:
            db.execute("INSERT INTO runtime VALUES('telegram_wait_until',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                       (now()+error.retry_after,))
        warning='Канал создан. '+str(error)+' Username и закрепление можно настроить отдельно.'
    row=db.one('SELECT * FROM channels WHERE id=?',(channel['id'],))
    result={'ok':True,'channel':public_channel(row),'account':db.public_account(),'pinned':pinned,
            'warning':warning,'replayed':False}
    db.execute("UPDATE channel_requests SET status='done',result=? WHERE id=?",(json.dumps(result,ensure_ascii=False),request_id))
    db.event('Канал создан.'+(' Закреплён в профиле.' if pinned else ''))
    return result
