import asyncio
import json
import io
import re
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient, functions, types, errors
from telethon.sessions import StringSession

from .db import now
from .account_access import AccountAccessError, inspect_account


class TelegramError(ValueError):
    pass


class ViewNotConfirmedError(TelegramError):
    """Telegram explicitly returned a negative acknowledgement."""
    pass


class PersonLookupError(TelegramError):
    """A problem with one target, not the connected account or transport."""
    pass


def api_credentials(config,api_id=0,api_hash=''):
    """Resolve complete pairs atomically; never mix a custom ID with a public hash."""
    if api_id or api_hash:
        if not api_id or not api_hash:
            raise TelegramError('Укажите оба параметра: API ID и API Hash, либо оставьте оба поля пустыми.')
    elif config.telegram_api_id or config.telegram_api_hash:
        api_id,api_hash=config.telegram_api_id,config.telegram_api_hash
    elif config.public_api_defaults:
        # Published application parameters from the dependency, not account credentials.
        from opentele.api import API
        api_id,api_hash=API.TelegramDesktop.api_id,API.TelegramDesktop.api_hash
    if api_id<=0 or not re.fullmatch(r'[a-fA-F0-9]{32}',api_hash):
        raise TelegramError('Введите корректные API ID и API Hash своего приложения с my.telegram.org.')
    return api_id,api_hash


def create_cloud_client(session, api_id, api_hash, *, receive_updates,proxy=None):
    # opentele patches Telethon's constructor globally and adds an `api` argument
    # before api_id. Positional credentials silently turn api_hash into an int.
    return TelegramClient(session,api_id=api_id,api_hash=api_hash,
        device_model='ZAVOD 1.0',system_version='Cloud Linux',app_version='1.0',
        receive_updates=receive_updates,flood_sleep_threshold=0,request_retries=0,
        connection_retries=1,retry_delay=1,timeout=10,**({'proxy':proxy} if proxy else {}))


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
    if value.startswith(('t.me/','telegram.me/')):
        value='https://'+value
    if value.startswith(('http://','https://')):
        try:
            p = urlparse(value)
            valid=p.hostname in ('t.me','telegram.me') and not p.query and not p.fragment and not p.username and not p.password and p.port in (None,443,80)
        except ValueError:
            valid=False
        if not valid:
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
    return '@' + value.lower()


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, AccountAccessError):
        return str(exc)
    name = type(exc).__name__
    if name in {'ApiIdPublishedFloodError','ApiIdInvalidError'}:
        return 'Telegram отклонил параметры приложения. Укажите свои API ID и API Hash в дополнительных настройках подключения.'
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
        self.client_proxy = None
        self.entity_cache = {}

    async def close(self):
        client,self.client=self.client,None
        self.client_proxy=None
        self.entity_cache.clear()
        if client:
            with suppress(Exception):
                await asyncio.wait_for(client.disconnect(),timeout=5)

    async def client_for_account(self):
        settings=getattr(self,'proxy_provider',lambda:None)()
        signature=settings['signature'] if settings else None
        if self.client and self.client.is_connected() and self.client_proxy==signature:
            return self.client
        account = self.db.account()
        if not account:
            raise TelegramError('Сначала подключите аккаунт.')
        if self.client:await self.close()
        proxy=await self.proxy_parameters(settings)
        d = json.loads(self.cipher.decrypt(account['credential']))
        self.client = create_cloud_client(
            StringSession(d['session']), d['api_id'], d['api_hash'],
            receive_updates=False,proxy=proxy)
        try:
            await self.connect_transport(self.client,bool(proxy))
            await inspect_account(self.client)
        except BaseException:
            await self.close()
            raise
        self.client_proxy=signature
        return self.client

    async def proxy_parameters(self,settings):
        from .proxies import resolve_connection,ProxyStoreError
        try:return await resolve_connection(settings)
        except ProxyStoreError:
            raise AccountAccessError('PROXY_ADDRESS_INVALID','Не удалось подключить выбранный прокси.',
                'Проверьте его публичный адрес и порт. Прямое подключение автоматически не включается.') from None

    async def connect_transport(self,client,using_proxy):
        try:await client.connect()
        except Exception as exc:
            if using_proxy and (isinstance(exc,(OSError,ConnectionError,TimeoutError)) or type(exc).__module__.startswith('python_socks')):
                raise AccountAccessError('PROXY_CONNECT_FAILED','Не удалось установить соединение через выбранный прокси.',
                    'Проверьте адрес, порт, логин и пароль прокси. Выберите другой прокси в настройках аккаунта, если этот недоступен.') from None
            raise

    async def import_tdata(self, folder: Path, api_id: int, api_hash: str,
                           passcode: str = '', password: str = ''):
        # Import only in this path: opentele extends Telethon globally.
        from opentele.td import TDesktop
        from opentele.api import UseCurrentSession
        self.import_stage = 'desktop_read'
        desktop = await asyncio.to_thread(TDesktop, str(folder), passcode=passcode)
        if not desktop.isLoaded() or not desktop.mainAccount:
            raise AccountAccessError('TDATA_INVALID','В tdata не найден активный аккаунт.',
                                     'Создайте свежий архив файлов авторизации Telegram Desktop.')
        client = None
        desktop_client = None
        persisted = False
        try:
            self.import_stage='proxy_connect'
            proxy=await self.proxy_parameters(getattr(self,'proxy_provider',lambda:None)())
            # Use the Desktop authorization only to accept a fresh, separate cloud login.
            # Manage both clients explicitly: opentele's QR helper leaks clients on errors.
            self.import_stage = 'desktop_session'
            desktop_client = await desktop.ToTelethon(flag=UseCurrentSession,
                receive_updates=False, flood_sleep_threshold=0, request_retries=0,
                connection_retries=1, timeout=10,**({'proxy':proxy} if proxy else {}))
            self.import_stage = 'desktop_connect'
            await self.connect_transport(desktop_client,bool(proxy))
            self.import_stage = 'desktop_check'
            original = await inspect_account(desktop_client)
            existing=getattr(self,'account_lookup',lambda uid:None)(original.id)
            if existing:
                raise AccountAccessError('ACCOUNT_ALREADY_CONNECTED','Этот Telegram-аккаунт уже подключён.',
                                         'Выберите его в списке аккаунтов.')
            self.import_stage = 'cloud_session'
            client = create_cloud_client(StringSession(),api_id,api_hash,receive_updates=True,proxy=proxy)
            self.import_stage = 'cloud_connect'
            await self.connect_transport(client,bool(proxy))
            self.import_stage = 'cloud_login'
            await authorize_cloud(desktop_client,client,password)
            self.import_stage = 'cloud_check'
            me = await inspect_account(client)
            if me.id != original.id:
                raise AccountAccessError('ACCOUNT_MISMATCH','Telegram вернул другой аккаунт. Импорт отменён.')
            self.import_stage = 'save'
            credential = self.cipher.encrypt(json.dumps({
                'session':StringSession.save(client.session),'api_id':api_id,'api_hash':api_hash
            }).encode())
            await self.close()
            with self.db.connect() as c:
                c.execute('INSERT INTO account(id,user_id,name,username,credential,created_at) VALUES(1,?,?,?,?,?)',
                          (str(me.id), display_name(me), me.username, credential, now()))
                c.execute('INSERT INTO account_profile(id,first_name,last_name,updated_at) VALUES(1,?,?,?)',
                          (me.first_name or '',me.last_name or '',now()))
                c.execute('INSERT INTO events(level,message,created_at) VALUES(?,?,?)',
                          ('info','Аккаунт подключён. Создана отдельная облачная сессия.',now()))
            persisted = True
            self.import_stage = 'done'
        except BaseException:
            # Revoke a newly created cloud authorization if it could not be persisted.
            if client and not persisted:
                try:
                    await asyncio.wait_for(client.log_out(), timeout=5)
                except Exception:
                    pass
            raise
        finally:
            if client:
                with suppress(Exception):
                    await asyncio.wait_for(client.disconnect(),timeout=5)
            if desktop_client:
                with suppress(Exception):
                    await asyncio.wait_for(desktop_client.disconnect(),timeout=5)

    def cache_profile(self, me, photo=None, *, replace_photo=False, photo_id=None):
        if str(me.id) != self.db.account()['user_id']:
            raise AccountAccessError('ACCOUNT_MISMATCH','Сессия не соответствует подключённому аккаунту.')
        with self.db.connect() as c:
            c.execute('UPDATE account SET name=?,username=? WHERE id=1', (display_name(me),me.username))
            c.execute('''INSERT INTO account_profile(id,first_name,last_name,updated_at) VALUES(1,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET first_name=excluded.first_name,last_name=excluded.last_name,updated_at=excluded.updated_at''',
                      (me.first_name or '', me.last_name or '', now()))
            if replace_photo:
                c.execute('UPDATE account_profile SET photo=?,photo_id=?,photo_revision=? WHERE id=1',
                          (photo, photo_id or '', uuid.uuid4().hex))
        return self.db.public_account()

    async def cache_full_profile(self, client, me):
        full=await client(functions.users.GetFullUserRequest(types.InputUserSelf()))
        value=full.full_user
        channel_id=str(getattr(value,'personal_channel_id',None) or '')
        channel=next((c for c in getattr(full,'chats',[]) if str(c.id)==channel_id),None)
        self.db.execute('UPDATE account_profile SET about=?,is_premium=?,personal_channel_id=?,personal_channel_title=?,personal_channel_username=? WHERE id=1',
            (getattr(value,'about','') or '',int(bool(getattr(me,'premium',False))),channel_id,
             getattr(channel,'title','') or '',getattr(channel,'username','') or ''))
        return self.db.public_account()

    async def read_profile(self):
        client = await self.client_for_account()
        me = await inspect_account(client)
        self.cache_profile(me)
        await self.cache_full_profile(client,me)
        photo_id = str(getattr(getattr(me,'photo',None),'photo_id','') or '')
        saved = self.db.one('SELECT photo_id,photo IS NOT NULL AS has_photo FROM account_profile WHERE id=1')
        if not saved or saved['photo_id'] != photo_id or (photo_id and not saved['has_photo']):
            photo = await client.download_profile_photo(me, file=bytes, download_big=False) if photo_id else None
            if photo is not None and len(photo) > 5*1024*1024:
                raise AccountAccessError('PHOTO_SIZE','Telegram вернул слишком большую фотографию профиля.')
            self.cache_profile(me, photo, replace_photo=True, photo_id=photo_id)
        else:
            self.cache_profile(me)
        return await self.cache_full_profile(client,me)

    async def change_name(self, first_name, last_name):
        first_name, last_name = first_name.strip(), last_name.strip()
        if not first_name or max(len(first_name),len(last_name)) > 64 or any(c in first_name+last_name for c in '\r\n\x00'):
            raise TelegramError('Имя обязательно. Имя и фамилия — до 64 символов, без переноса строки.')
        client = await self.client_for_account()
        me = await inspect_account(client)
        if first_name != (me.first_name or '') or last_name != (me.last_name or ''):
            me = await client(functions.account.UpdateProfileRequest(first_name=first_name,last_name=last_name))
        return self.cache_profile(me)

    async def change_username(self, username):
        username = username.strip().removeprefix('@')
        if username and not re.fullmatch(r'[A-Za-z0-9_]{5,32}',username):
            raise TelegramError('Username: 5–32 латинские буквы, цифры или подчёркивания. Пустое поле удаляет username.')
        client = await self.client_for_account()
        me = await inspect_account(client)
        if username == (me.username or ''):
            return self.cache_profile(me)
        if username and username.casefold() != (me.username or '').casefold():
            if not await client(functions.account.CheckUsernameRequest(username)):
                raise AccountAccessError('USERNAME_OCCUPIED','Этот username недоступен.','Выберите другой username.')
        try:
            me = await client(functions.account.UpdateUsernameRequest(username))
        except errors.UsernameNotModifiedError:
            me = await inspect_account(client)
        return self.cache_profile(me)

    async def change_avatar(self, photo, thumbnail):
        client = await self.client_for_account()
        me = await inspect_account(client)
        upload = io.BytesIO(photo)
        upload.name = 'avatar.jpg'
        file = await client.upload_file(upload)
        result = await client(functions.photos.UploadProfilePhotoRequest(file=file))
        # The RPC's photo is authoritative; no second write or automatic retry.
        return self.cache_profile(me,thumbnail,replace_photo=True,photo_id=str(result.photo.id))

    async def change_about(self, about):
        if '\x00' in about:
            raise AccountAccessError('ABOUT_INVALID','Описание содержит недопустимый символ.')
        client=await self.client_for_account()
        me=await inspect_account(client)
        limit=140 if bool(getattr(me,'premium',False)) else 70
        if len(about)>limit:
            raise AccountAccessError('ABOUT_TOO_LONG',f'Описание этого аккаунта — до {limit} символов.')
        await client(functions.account.UpdateProfileRequest(about=about))
        return await self.read_profile()

    async def premium_for_person(self, person):
        if not person:
            return None
        client=await self.client_for_account()
        try:
            user=await client.get_entity(self.peer(person))
        except (ValueError,errors.PeerIdInvalidError,errors.UserIdInvalidError):
            return None
        if not isinstance(user,types.User) or getattr(user,'min',False) or user.deleted:
            return None
        premium=bool(getattr(user,'premium',False))
        self.db.execute('UPDATE people SET is_premium=?,premium_checked_at=? WHERE user_id=?',
                        (int(premium),now(),person['user_id']))
        return premium

    async def react_story(self, person, story_id):
        client=await self.client_for_account()
        await client(functions.stories.SendReactionRequest(peer=self.peer(person),story_id=story_id,
                     reaction=types.ReactionEmoji(emoticon='👍'),add_to_recent=False))

    def channel_peer(self, channel):
        data=json.loads(self.cipher.decrypt(channel['peer']))
        return types.InputChannel(int(data['id']),int(data['hash']))

    async def check_channel_username(self, username):
        username=username.strip().removeprefix('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username):
            raise AccountAccessError('CHANNEL_USERNAME_INVALID','Username канала: 5–32 латинские буквы, цифры и подчёркивания; первая — буква.')
        client=await self.client_for_account()
        if not await client(functions.channels.CheckUsernameRequest(channel=types.InputChannelEmpty(),username=username)):
            raise AccountAccessError('CHANNEL_USERNAME_OCCUPIED','Этот username канала уже занят.')
        return username.lower()

    async def create_channel(self, title, about):
        client=await self.client_for_account()
        result=await client(functions.channels.CreateChannelRequest(title=title,about=about,broadcast=True,megagroup=False))
        channel=next((c for c in getattr(result,'chats',[]) if isinstance(c,types.Channel) and getattr(c,'broadcast',False)),None)
        if not channel or not channel.access_hash:
            raise AccountAccessError('CHANNEL_CREATE_UNCERTAIN','Результат создания канала не подтверждён.','Проверьте список каналов в Telegram перед новой попыткой.')
        return {'id':str(channel.id),'title':channel.title,'username':channel.username,
                'peer':self.cipher.encrypt(json.dumps({'id':channel.id,'hash':channel.access_hash}).encode())}

    async def channel_username(self, channel_id, username):
        channel=self.db.one('SELECT * FROM channels WHERE id=?',(channel_id,))
        if not channel:
            raise AccountAccessError('CHANNEL_NOT_FOUND','Канал не найден среди созданных этим аккаунтом.')
        username=username.strip().removeprefix('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username):
            raise AccountAccessError('CHANNEL_USERNAME_INVALID','Введите username канала: 5–32 символа, первая — латинская буква.')
        client=await self.client_for_account()
        try:
            await client(functions.channels.UpdateUsernameRequest(channel=self.channel_peer(channel),username=username))
        except errors.UsernameNotModifiedError:
            pass
        self.db.execute('UPDATE channels SET username=? WHERE id=?',(username.lower(),channel_id))
        return self.db.public_account()

    async def pin_channel(self, ref):
        client=await self.client_for_account()
        if not ref.strip():
            peer=types.InputChannelEmpty()
        else:
            saved=self.db.one('SELECT * FROM channels WHERE id=?',(ref,))
            if saved:
                if not saved['username']:
                    raise AccountAccessError('CHANNEL_PUBLIC_REQUIRED','Сначала назначьте публичный username каналу.')
                peer=self.channel_peer(saved)
            else:
                normalized=normalize_chat(ref)
                if not normalized.startswith('@'):
                    raise AccountAccessError('CHANNEL_REF_INVALID','Укажите @username своего публичного канала.')
                entity=await client.get_entity(normalized)
                if not isinstance(entity,types.Channel) or not entity.broadcast or not entity.creator or not entity.username:
                    raise AccountAccessError('CHANNEL_OWNER_REQUIRED','Можно закрепить только свой публичный канал.')
                peer=types.InputChannel(entity.id,entity.access_hash)
        await client(functions.account.UpdatePersonalChannelRequest(channel=peer))
        return await self.read_profile()

    async def resolve(self, ref):
        client = await self.client_for_account()
        cached=self.entity_cache.get(ref)
        if cached and cached[0]>now()-300:
            return cached[1]
        if ref.startswith('+'):
            invite = await client(functions.messages.CheckChatInviteRequest(ref[1:]))
            if not isinstance(invite, types.ChatInviteAlready):
                raise TelegramError('Аккаунт ещё не состоит в этой группе. Вступите в неё вручную.')
            entity = invite.chat
        else:
            try:
                entity = await client.get_entity(int(ref) if ref.startswith('-') else ref)
            except (ValueError,errors.ChannelInvalidError,errors.PeerIdInvalidError):
                if not ref.startswith('-'):
                    raise
                # StringSession does not persist entity access hashes. Populate the
                # normal dialog cache only from chats already visible to this account.
                entity=None
                async for dialog in client.iter_dialogs(limit=1000):
                    if dialog.id==int(ref):
                        entity=dialog.entity
                        break
                if entity is None:
                    raise TelegramError('Чат по ID не найден среди доступных диалогов. Укажите его @username или ссылку t.me.')
        if not isinstance(entity,(types.Chat,types.Channel)) or (isinstance(entity,types.Channel) and not entity.megagroup):
            raise TelegramError('Нужна группа или супергруппа. Список подписчиков канала обычно недоступен.')
        self.entity_cache[ref]=(now(),entity)
        return entity

    def person(self, user):
        if not isinstance(user,types.User) or user.bot or user.deleted or user.is_self or not user.access_hash:
            return None
        return {'user_id':str(user.id),'name':display_name(user),'username':user.username,
                'peer':self.cipher.encrypt(json.dumps({'id':user.id,'hash':user.access_hash}).encode())}

    async def resolve_person(self, ref):
        client = await self.client_for_account()
        try:
            user = await client.get_entity(ref)
        except (errors.UsernameInvalidError, errors.UsernameNotOccupiedError,
                errors.PeerIdInvalidError, errors.UserIdInvalidError, ValueError):
            raise PersonLookupError('Пользователь с таким юзернеймом не найден.') from None
        if not isinstance(user, types.User):
            raise PersonLookupError('Этот юзернейм принадлежит чату или каналу, а не пользователю.')
        if user.bot:
            raise PersonLookupError('Это бот. Добавлять можно обычных пользователей.')
        if user.deleted:
            raise PersonLookupError('Этот аккаунт удалён.')
        if user.is_self:
            raise PersonLookupError('Это ваш подключённый аккаунт.')
        person = self.person(user)
        if not person:
            raise PersonLookupError('Telegram не предоставил доступ к этому пользователю.')
        return person

    async def gather(self, ref, mode, cursor, remaining):
        client = await self.client_for_account()
        entity = await self.resolve(ref)
        size = min(100, remaining)
        if mode == 'authors':
            # Only authors returned in readable messages; hidden membership isn't queried.
            page_limit=min(50,max(1,remaining))
            messages = await client.get_messages(entity,limit=page_limit,offset_id=cursor)
            people = []
            for message in messages:
                user = await message.get_sender()
                if person := self.person(user):
                    people.append(person)
            return Page(people, messages[-1].id if messages else cursor,
                        len(messages)<page_limit, entity.title)
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
            if type(exc).__name__ in {'UserPrivacyRestrictedError','UserIsBlockedError','UserIdInvalidError','PeerIdInvalidError','InputUserDeactivatedError','UserNotMutualContactError'}:
                return []
            raise
        stories = []
        for item in result.stories.stories:
            if not isinstance(item,types.StoryItem):
                continue
            expires = int(item.expire_date.timestamp())
            if expires > now():
                stories.append({'id':item.id,'expires':expires,'has_reaction':bool(getattr(item,'sent_reaction',None))})
        return stories

    async def register_views(self, person, story_ids):
        client = await self.client_for_account()
        # This RPC registers a view. Local persistence prevents deliberate repeat views.
        accepted = await client(functions.stories.IncrementStoryViewsRequest(peer=self.peer(person),id=story_ids))
        if not accepted:
            raise ViewNotConfirmedError('Telegram не подтвердил регистрацию просмотров. Они не включены в счётчик.')
        return True


def display_name(user):
    return ' '.join(p for p in (user.first_name,user.last_name) if p).strip() or user.username or str(user.id)
