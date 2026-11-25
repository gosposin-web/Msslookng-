import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from telethon import errors, functions, types

from app.account_access import AccountAccessError, account_error, inspect_account
from app.avatar import MAX_PHOTO_UPLOAD, prepare_avatar
from app.config import Config
from app.main import create_app
from app.telegram import TelegramGateway
from test_app import account, login, make_zip


def image_bytes(fmt='PNG', size=(320,240)):
    out = io.BytesIO()
    Image.new('RGB',size,'blue').save(out,fmt)
    return out.getvalue()


class ProtocolClient:
    def __init__(self):
        self.me = types.User(90000,is_self=True,first_name='Старое',last_name='Имя',username='old_name')
        self.config = types.JsonObject([])
        self.calls = []
        self.about = ''
        self.fail = None
        self.username_available = True
        self.logged_out = False
        self.disconnected = False

    def is_connected(self):
        return True

    async def connect(self):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def log_out(self):
        self.logged_out = True

    async def upload_file(self, file):
        self.upload = file.read()
        return 'uploaded-photo'

    async def __call__(self, request):
        self.calls.append(request)
        if self.fail:
            raise self.fail
        if isinstance(request,functions.users.GetUsersRequest):
            return [self.me]
        if isinstance(request,functions.help.GetAppConfigRequest):
            return types.help.AppConfig(hash=1,config=self.config)
        if isinstance(request,functions.users.GetFullUserRequest):
            return SimpleNamespace(full_user=SimpleNamespace(about=self.about,personal_channel_id=None),chats=[])
        if isinstance(request,functions.account.UpdateProfileRequest):
            if request.first_name is not None: self.me.first_name=request.first_name
            if request.last_name is not None: self.me.last_name=request.last_name
            if request.about is not None: self.about=request.about
            return self.me
        if isinstance(request,functions.account.CheckUsernameRequest):
            return self.username_available
        if isinstance(request,functions.account.UpdateUsernameRequest):
            self.me.username = request.username or None
            return self.me
        if isinstance(request,functions.photos.UploadProfilePhotoRequest):
            return SimpleNamespace(photo=types.PhotoEmpty(777))
        raise AssertionError(type(request).__name__)


@pytest.fixture
def panel(tmp_path):
    config = Config(tmp_path,'a-long-test-password','a-test-encryption-secret-that-is-long-enough',cookie_secure=False)
    app = create_app(config,run_worker=False)
    with TestClient(app) as client:
        account(app.state.db,config)
        protocol = ProtocolClient()
        app.state.gateway.client = protocol
        login(client)
        yield client,app,protocol


def test_profile_edits_use_telegram_and_persist_canonical_fields(panel):
    client,app,protocol = panel
    initial = client.get('/api/account/profile')
    assert initial.status_code == 200
    assert initial.json()['account']['first_name'] == 'Старое'
    changed = client.post('/api/account/profile/name',json={'first_name':' Новое ','last_name':''})
    assert changed.status_code == 200
    assert protocol.me.first_name == 'Новое'
    assert changed.json()['account']['last_name'] == ''
    changed = client.post('/api/account/profile/username',json={'username':'@new_username'})
    assert changed.status_code == 200
    assert protocol.me.username == 'new_username'
    assert any(isinstance(r,functions.account.CheckUsernameRequest) for r in protocol.calls)
    assert client.post('/api/account/profile/username',json={'username':''}).status_code == 200
    assert protocol.me.username is None
    public = client.get('/api/state').json()['account']
    assert public['name'] == 'Новое'
    assert 'credential' not in public and 'photo' not in public
    assert app.state.db.public_account() == public


def test_unavailable_username_validation_and_csrf_never_mutate(panel):
    client,app,protocol = panel
    protocol.username_available = False
    result = client.post('/api/account/profile/username',json={'username':'unavailable'})
    assert result.status_code == 400 and result.json()['code'] == 'USERNAME_OCCUPIED'
    assert not any(isinstance(r,functions.account.UpdateUsernameRequest) for r in protocol.calls)
    protocol.calls.clear()
    assert client.post('/api/account/profile/name',json={'first_name':' '}).status_code == 400
    assert client.post('/api/account/profile/username',json={'username':'wrong!'}).status_code == 400
    assert protocol.calls == []
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post('/api/account/profile/name',json={'first_name':'Несохранённое'}).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert protocol.me.first_name == 'Старое'


def test_avatar_processed_uploaded_cached_and_deleted_with_account(panel):
    client,app,protocol = panel
    result = client.post('/api/account/profile/photo',files={'file':('phone.png',image_bytes(),'image/png')})
    assert result.status_code == 200
    a = result.json()['account']
    assert a['has_avatar'] and a['photo_revision']
    with Image.open(io.BytesIO(protocol.upload)) as photo:
        assert photo.format == 'JPEG' and photo.width == photo.height
        assert not photo.getexif()
    response = client.get('/api/account/profile/photo')
    assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'
    assert response.headers['cache-control'].startswith('private')
    assert client.get('/api/account/profile/photo',headers={'If-None-Match':response.headers['etag']}).status_code == 304
    bad = client.post('/api/account/profile/photo',files={'file':('bad.jpg',b'not an image','image/jpeg')})
    assert bad.status_code == 400 and bad.json()['code'] == 'PHOTO_INVALID'
    assert client.post('/api/account/profile/photo',files={'file':('large.jpg',b'x'*(MAX_PHOTO_UPLOAD+1))}).status_code in (400,413)
    assert client.delete('/api/account').status_code == 200
    assert app.state.db.one('SELECT * FROM account_profile') is None
    assert client.get('/api/account/profile/photo').status_code == 404


@pytest.mark.parametrize('fmt',['JPEG','PNG','WEBP'])
def test_phone_avatar_formats(fmt):
    photo, thumbnail = prepare_avatar(image_bytes(fmt))
    with Image.open(io.BytesIO(photo)) as image:
        assert image.format == 'JPEG' and image.size == (240,240)
    assert thumbnail


def test_avatar_rejects_small_and_animated_images():
    with pytest.raises(AccountAccessError,match='160'):
        prepare_avatar(image_bytes(size=(100,100)))
    out = io.BytesIO()
    Image.new('RGB',(200,200),'red').save(out,'WEBP',save_all=True,append_images=[Image.new('RGB',(200,200),'blue')],duration=100,loop=0)
    with pytest.raises(AccountAccessError,match='прочитать'):
        prepare_avatar(out.getvalue())


def frozen_config(appeal='https://telegram.org/appeal'):
    return types.JsonObject([
        types.JsonObjectValue('freeze_since_date',types.JsonNumber(100)),
        types.JsonObjectValue('freeze_until_date',types.JsonNumber(1900000000)),
        types.JsonObjectValue('freeze_appeal_url',types.JsonString(appeal)),
    ])


@pytest.mark.asyncio
async def test_frozen_check_reads_actual_tl_json_and_does_not_infer_from_restricted():
    protocol = ProtocolClient()
    protocol.me.restricted = True
    assert await inspect_account(protocol) is protocol.me
    protocol.config = frozen_config('https://evil.example/appeal')
    with pytest.raises(AccountAccessError) as caught:
        await inspect_account(protocol)
    assert caught.value.code == 'ACCOUNT_FROZEN'
    assert caught.value.freeze_until == 1900000000
    assert not caught.value.appeal_url


@pytest.mark.parametrize('exc,code',[
    (errors.SessionRevokedError(None),'SESSION_REVOKED'),
    (errors.AuthKeyUnregisteredError(None),'SESSION_INVALID'),
    (errors.AuthKeyDuplicatedError(None),'SESSION_DUPLICATED'),
    (errors.UserDeactivatedBanError(None),'ACCOUNT_BANNED'),
    (errors.UserDeactivatedError(None),'ACCOUNT_DELETED'),
    (errors.PasswordHashInvalidError(None),'TWO_FACTOR_INVALID'),
    (TimeoutError('secret session data'),'CONNECTION_FAILED'),
    (RuntimeError('secret session data'),'CLIENT_INTERNAL_ERROR'),
])
def test_diagnostics_distinguish_account_states_and_hide_payloads(exc,code):
    error = account_error(exc)
    assert error.code == code
    assert 'secret session data' not in str(error.payload())


def test_internal_import_error_has_phase_and_private_diagnostic_id(panel,monkeypatch,caplog):
    import json
    import re
    client,app,protocol=panel
    app.state.db.execute('DELETE FROM account')
    async def fail(folder,api_id,api_hash,passcode,password):
        app.state.gateway.import_stage='cloud_login'
        raise TypeError('private-session-value '+passcode+' '+password+' '+api_hash)
    monkeypatch.setattr(app.state.gateway,'import_tdata',fail)
    result=client.post('/api/account/tdata',data={'api_id':'123456','api_hash':'a'*32,
                        'passcode':'private-desktop-password','password':'private-cloud-password'},
                        files={'file':('private-archive-name.zip',make_zip({'tdata/key_datas':b'key','tdata/D877F783D5D3EF8Cs':b'account'}))})
    assert result.status_code==500
    data=result.json()
    assert data['code']=='CLIENT_INTERNAL_ERROR'
    assert data['stage']=='cloud_login' and data['stage_label']=='Подтверждение нового входа'
    assert re.fullmatch(r'[a-f0-9]{12}',data['diagnostic_id'])
    assert 'diagnostic='+data['diagnostic_id'] in caplog.text
    assert 'exception=TypeError' in caplog.text
    for secret in ('private-session-value','private-desktop-password','private-cloud-password','private-archive-name','a'*32):
        assert secret not in caplog.text
        assert secret not in json.dumps(data)
    assert app.state.db.account() is None


def test_unknown_rpc_error_stays_separate_from_internal_application_errors():
    error=account_error(errors.RPCError(None,'UNRECOGNIZED_ERROR',400))
    assert error.code=='UNRECOGNIZED_ERROR'
    assert 'Telegram' in str(error)
    assert account_error(TypeError('private session')).code=='CLIENT_INTERNAL_ERROR'


@pytest.mark.parametrize('condition',['frozen','revoked','banned'])
def test_import_rejects_invalid_account_before_creating_cloud_session(tmp_path,monkeypatch,condition):
    import opentele.td
    import app.telegram as telegram
    desktop_client = ProtocolClient()
    if condition == 'frozen':
        desktop_client.config = frozen_config()
    elif condition == 'revoked':
        desktop_client.fail = errors.SessionRevokedError(None)
    else:
        desktop_client.fail = errors.UserDeactivatedBanError(None)
    class Desktop:
        mainAccount = True
        def isLoaded(self):
            return True
        async def ToTelethon(self,**kwargs):
            return desktop_client
    monkeypatch.setattr(opentele.td,'TDesktop',lambda *args,**kwargs:Desktop())
    cloud_clients = []
    def cloud_factory(*args,**kwargs):
        cloud_clients.append(ProtocolClient())
        return cloud_clients[-1]
    monkeypatch.setattr(telegram,'TelegramClient',cloud_factory)
    config = Config(tmp_path,'a-long-test-password','a-test-encryption-secret-that-is-long-enough',cookie_secure=False)
    app = create_app(config,run_worker=False)
    with TestClient(app) as client:
        login(client)
        result = client.post('/api/account/tdata',files={'file':('account.zip',make_zip({'tdata/key_datas':b'key','tdata/D877F783D5D3EF8Cs':b'account'}))})
        assert result.status_code == 400
        assert result.json()['code'] == {'frozen':'ACCOUNT_FROZEN','revoked':'SESSION_REVOKED','banned':'ACCOUNT_BANNED'}[condition]
        if condition == 'frozen':
            assert result.json()['appeal_url'].startswith('https://telegram.org/')
        assert app.state.db.account() is None
    assert cloud_clients == []
    assert desktop_client.disconnected and not desktop_client.logged_out


def test_telegram_flood_wait_applies_to_profiles_and_background_jobs(panel):
    client,app,protocol = panel
    protocol.fail = errors.FloodWaitError(None,31)
    result = client.post('/api/account/profile/name',json={'first_name':'Новое'})
    assert result.status_code == 429 and result.json()['retry_after'] == 31
    assert result.headers['retry-after'] == '31'
    protocol.fail = None
    protocol.calls.clear()
    assert client.get('/api/account/profile').status_code == 429
    assert protocol.calls == []
    jid = app.state.db.create_job('scan',{})
    assert app.state.db.claim() is None
    assert app.state.db.job(jid)['status'] == 'queued'


@pytest.mark.asyncio
async def test_failed_new_cloud_validation_revokes_only_the_new_session(tmp_path,monkeypatch):
    import opentele.td
    import app.telegram as telegram
    from app.db import DB
    desktop_client,cloud_client = ProtocolClient(),ProtocolClient()
    cloud_client.config = frozen_config()
    class Desktop:
        mainAccount = True
        def isLoaded(self):
            return True
        async def ToTelethon(self,**kwargs):
            return desktop_client
    monkeypatch.setattr(opentele.td,'TDesktop',lambda *args,**kwargs:Desktop())
    monkeypatch.setattr(telegram,'TelegramClient',lambda *args,**kwargs:cloud_client)
    authorized = []
    async def authorize(original,new,password):
        authorized.append((original,new))
    monkeypatch.setattr(telegram,'authorize_cloud',authorize)
    config = Config(tmp_path,'a-long-test-password','a-test-encryption-secret-that-is-long-enough')
    db = DB(tmp_path/'test.sqlite3')
    gateway = TelegramGateway(db,config.cipher())
    with pytest.raises(AccountAccessError) as caught:
        await gateway.import_tdata(tmp_path,1,'a'*32)
    assert caught.value.code == 'ACCOUNT_FROZEN'
    assert authorized == [(desktop_client,cloud_client)]
    assert cloud_client.logged_out and cloud_client.disconnected
    assert desktop_client.disconnected and not desktop_client.logged_out
    assert db.account() is None


@pytest.mark.asyncio
async def test_numeric_chat_id_uses_only_visible_dialogs_and_caches_result(panel):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    client,app,protocol=panel
    entity=types.Channel(id=12345,title='Synthetic group',photo=types.ChatPhotoEmpty(),
                         date=datetime.now(timezone.utc),megagroup=True,access_hash=42)
    calls=[]
    async def get_entity(ref):
        calls.append('entity')
        raise ValueError('No cached access hash')
    async def dialogs(limit):
        assert limit==1000
        calls.append('dialogs')
        yield SimpleNamespace(id=-1000000012345,entity=entity)
    protocol.get_entity=get_entity;protocol.iter_dialogs=dialogs
    assert await app.state.gateway.resolve('-1000000012345') is entity
    assert await app.state.gateway.resolve('-1000000012345') is entity
    assert calls==['entity','dialogs']
    await app.state.gateway.close()
    assert app.state.gateway.entity_cache=={}


@pytest.mark.asyncio
async def test_failed_connection_is_cleaned_even_if_disconnect_fails(panel,monkeypatch):
    import json
    import app.telegram as telegram
    client,app,protocol=panel
    gateway=app.state.gateway
    await gateway.close()
    credential=gateway.cipher.encrypt(json.dumps({'session':'','api_id':123456,'api_hash':'a'*32}).encode())
    app.state.db.execute('UPDATE account SET credential=?',(credential,))
    class Broken(ProtocolClient):
        async def connect(self):
            raise ConnectionError('synthetic connection failure')
        async def disconnect(self):
            self.disconnected=True
            raise RuntimeError('synthetic close failure')
    broken=Broken()
    monkeypatch.setattr(telegram,'TelegramClient',lambda *a,**kw:broken)
    with pytest.raises(ConnectionError):
        await gateway.client_for_account()
    assert broken.disconnected and gateway.client is None


@pytest.mark.asyncio
@pytest.mark.parametrize('exc',[errors.InputUserDeactivatedError(None),errors.UserNotMutualContactError(None)])
async def test_unavailable_target_user_does_not_fail_the_whole_chat(panel,exc):
    import json
    _,app,protocol=panel
    protocol.fail=exc
    person={'peer':app.state.gateway.cipher.encrypt(json.dumps({'id':123456,'hash':42}).encode())}
    assert await app.state.gateway.stories(person)==[]
