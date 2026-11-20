import asyncio
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession

from .db import now


class TelegramError(ValueError):
    pass


async def authorize_cloud(desktop_client, cloud_client, password):
    qr = await cloud_client.qr_login()
    # QRLogin.wait must be running before the Desktop session accepts the token.
    # It also requires the new client to receive UpdateLoginToken updates.
    waiting = asyncio.create_task(qr.wait(timeout=45))
    try:
        await asyncio.sleep(0)
        await desktop_client(functions.auth.AcceptLoginTokenRequest(qr.token))
        try:
            await waiting
        except errors.SessionPasswordNeededError:
            if not password:
                raise
            await cloud_client.sign_in(password=password)
    finally:
        if not waiting.done():
            waiting.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await waiting


def normalize_chat(value: str) -> str:
    value = value.strip()
    if value.startswith(('http://','https://')):
        p = urlparse(value)
        if p.hostname not in ('t.me','telegram.me') or p.query or p.fragment:
            raise TelegramError('Укажите ссылку t.me на группу или её @username.')
        value = p.path.strip('/')
    if value.startswith('+') and re.fullmatch(r'\+[A-Za-z0-9_-]{8,80}', value):
        return value
    if value.startswith('joinchat/'):
        return normalize_chat('+' + value.split('/',1)[1])
    if re.fullmatch(r'-[0-9]{5,20}', value):
        return value
    value = value.lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise TelegramError('Укажите @username группы, ссылку t.me или ID доступного чата.')
    return '@' + value


def friendly_error(exc: Exception) -> str:
    name = type(exc).__name__
    if name in {'ChatAdminRequiredError','ChatAdminRequired'}:
        return 'Telegram не открыл список участников. Нужны права администратора; можно выбрать сбор авторов сообщений.'
    if name in {'ChannelPrivateError','UserNotParticipantError','InviteHashExpiredError','InviteHashInvalidError'}:
        return 'Чат недоступен этому аккаунту. Проверьте ссылку и самостоятельно вступите в группу.'
    if name in {'UsernameInvalidError','UsernameNotOccupiedError'}:
        return 'Группа по этому имени не найдена.'
    if name in {'SessionPasswordNeededError','PasswordHashInvalidError','PasswordIncorrect','NoPasswordProvided'}:
        return 'Для создания облачной сессии нужен правильный пароль двухэтапной проверки Telegram.'
    if name in {'TDataBadDecryptKey','TDataInvalidMagic','TDataWrongPasscode'}:
        return 'Не удалось расшифровать tdata. Проверьте код-пароль Telegram Desktop.'
    if name in {'AuthKeyUnregisteredError','SessionRevokedError','AuthKeyDuplicatedError','UserDeactivatedError','AuthKeyNotFound'}:
        return 'Сессия аккаунта недействительна. Подключите аккаунт заново.'
    if name in {'FreshTokenRequiredError','SessionTooFreshError','PasswordTooFreshError','FreshResetAuthorisationForbiddenError'}:
        return 'Telegram пока запрещает новую авторизацию с этой сессии. Попробуйте позже.'
    if isinstance(exc, TelegramError):
        return str(exc)
    if isinstance(exc, (TimeoutError,ConnectionError,OSError)):
        return 'Telegram временно не отвечает. Можно повторить задание позже.'
    return 'Telegram не выполнил действие (' + name + '). Проверьте доступ аккаунта и повторите.'


@dataclass
class Page:
    people: list
    cursor: int
    done: bool
    title: str


class TelegramGateway:
    def __init__(self, db, cipher):
        self.db = db
        self.cipher = cipher
        self.lock = asyncio.Lock()
        self.client = None

    async def close(self):
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def client_for_account(self):
        if self.client and self.client.is_connected():
            return self.client
        account = self.db.account()
        if not account:
            raise TelegramError('Сначала подключите аккаунт.')
        d = json.loads(self.cipher.decrypt(account['credential']))
        self.client = TelegramClient(
            StringSession(d['session']), d['api_id'], d['api_hash'],
            device_model='Story Cloud', system_version='Cloud Linux', app_version='1.0',
            receive_updates=False, flood_sleep_threshold=0, request_retries=0,
            connection_retries=1, retry_delay=1, timeout=10)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.close()
            raise TelegramError('Сессия истекла. Подключите аккаунт заново.')
        return self.client

    async def import_tdata(self, folder: Path, api_id: int, api_hash: str,
                           passcode: str = '', password: str = ''):
        # Import only in this path: opentele extends Telethon globally.
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
        desktop = await asyncio.to_thread(TDesktop, str(folder), passcode=passcode)
        if not desktop.isLoaded() or not desktop.mainAccount:
            raise TelegramError('В tdata не найден активный аккаунт.')
        client = None
        desktop_client = None
        try:
            # Use the Desktop authorization only to accept a fresh, separate cloud login.
            # Manage both clients explicitly: opentele's QR helper leaks clients on errors.
            desktop_client = await desktop.ToTelethon(flag=UseCurrentSession,
                receive_updates=False, flood_sleep_threshold=0, request_retries=0,
                connection_retries=1, timeout=10)
            await desktop_client.connect()
            if not await desktop_client.is_user_authorized():
                raise TelegramError('Сессия в tdata уже завершена или отозвана.')
            client = TelegramClient(StringSession(),api_id,api_hash,
                device_model='Story Cloud',system_version='Cloud Linux',app_version='1.0',
                receive_updates=True,flood_sleep_threshold=0,request_retries=0,
                connection_retries=1,timeout=10)
            await client.connect()
            await authorize_cloud(desktop_client,client,password)
            if not await client.is_user_authorized():
                raise TelegramError('Не удалось авторизовать облачную сессию.')
            me = await client.get_me()
            credential = self.cipher.encrypt(json.dumps({
                'session':StringSession.save(client.session),'api_id':api_id,'api_hash':api_hash
            }).encode())
            await self.close()
            self.db.execute('INSERT INTO account(id,user_id,name,username,credential,created_at) VALUES(1,?,?,?,?,?)',
                            (str(me.id), display_name(me), me.username, credential, now()))
            self.db.event('Аккаунт подключён. Создана отдельная облачная сессия.')
        except BaseException:
            # Revoke a newly created cloud authorization if it could not be persisted.
            if client:
                try:
                    await asyncio.wait_for(client.log_out(), timeout=5)
                except Exception:
                    pass
            raise
        finally:
            if client:
                await client.disconnect()
            if desktop_client:
                await desktop_client.disconnect()

    async def resolve(self, ref):
        client = await self.client_for_account()
        if ref.startswith('+'):
            invite = await client(functions.messages.CheckChatInviteRequest(ref[1:]))
            if not isinstance(invite, types.ChatInviteAlready):
                raise TelegramError('Аккаунт ещё не состоит в этой группе. Вступите в неё вручную.')
            entity = invite.chat
        else:
            entity = await client.get_entity(int(ref) if ref.startswith('-') else ref)
        if not isinstance(entity,(types.Chat,types.Channel)) or (isinstance(entity,types.Channel) and not entity.megagroup):
            raise TelegramError('Нужна группа или супергруппа. Список подписчиков канала обычно недоступен.')
        return entity

    def person(self, user):
        if not isinstance(user,types.User) or user.bot or user.deleted or user.is_self or not user.access_hash:
            return None
        return {'user_id':str(user.id),'name':display_name(user),'username':user.username,
                'peer':self.cipher.encrypt(json.dumps({'id':user.id,'hash':user.access_hash}).encode())}

    async def gather(self, ref, mode, cursor, remaining):
        client = await self.client_for_account()
        entity = await self.resolve(ref)
        size = min(100, remaining)
        if mode == 'authors':
            # Only authors returned in readable messages; hidden membership isn't queried.
            messages = await client.get_messages(entity,limit=50,offset_id=cursor)
            people = []
            for message in messages:
                user = await message.get_sender()
                if person := self.person(user):
                    people.append(person)
            return Page(people, messages[-1].id if messages else cursor,
                        len(messages)<50, entity.title)
        if isinstance(entity,types.Chat):
            users = await client.get_participants(entity)
            page = users[cursor:cursor+size]
            return Page([p for u in page if (p:=self.person(u))],cursor+len(page),
                        cursor+len(page)>=len(users),entity.title)
        result = await client(functions.channels.GetParticipantsRequest(
            channel=entity,filter=types.ChannelParticipantsSearch(''),offset=cursor,limit=size,hash=0))
        users = getattr(result,'users',[])
        participants = getattr(result,'participants',[])
        return Page([p for u in users if (p:=self.person(u))],cursor+len(participants),
                    len(participants)<size,entity.title)

    def peer(self, person):
        data = json.loads(self.cipher.decrypt(person['peer']))
        return types.InputPeerUser(int(data['id']),int(data['hash']))

    async def stories(self, person):
        client = await self.client_for_account()
        try:
            result = await client(functions.stories.GetPeerStoriesRequest(self.peer(person)))
        except Exception as exc:
            if type(exc).__name__ in {'UserPrivacyRestrictedError','UserIsBlockedError','UserIdInvalidError','PeerIdInvalidError'}:
                return []
            raise
        stories = []
        for item in result.stories.stories:
            if not isinstance(item,types.StoryItem):
                continue
            expires = int(item.expire_date.timestamp())
            if expires > now():
                stories.append({'id':item.id,'expires':expires})
        return stories

    async def register_views(self, person, story_ids):
        client = await self.client_for_account()
        # This RPC registers a view. Local persistence prevents deliberate repeat views.
        accepted = await client(functions.stories.IncrementStoryViewsRequest(peer=self.peer(person),id=story_ids))
        if not accepted:
            raise TelegramError('Telegram не подтвердил регистрацию просмотров.')


def display_name(user):
    return ' '.join(p for p in (user.first_name,user.last_name) if p).strip() or user.username or str(user.id)
