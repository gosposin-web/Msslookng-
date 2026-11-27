import asyncio
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from app.archive import ArchiveError, extract_tdata
from app.config import Config
from app.db import DB, now
from app.main import create_app
from app.telegram import Page, normalize_chat, authorize_cloud
from app.worker import Worker


class FakeGateway:
    def __init__(self,db,cipher):
        self.db,self.cipher=db,cipher
        self.lock=asyncio.Lock()
        self.calls=[]
        self.reactions=[]
        self.fail=None
        self.import_path=None
        self.after_stories=None
        self.reactions=[]
        self.premium=False

    async def close(self):
        pass

    def person(self,uid):
        return {'user_id':str(uid),'name':'Человек '+str(uid),'username':'person_'+str(uid),
                'peer':self.cipher.encrypt(json.dumps({'id':uid,'hash':42}).encode())}

    async def gather(self,*args):
        if self.fail:
            raise self.fail
        return Page([self.person(11111),self.person(11111),self.person(22222)],3,True,'Тестовая группа')

    async def stories(self,person):
        if self.fail:
            raise self.fail
        if self.after_stories:
            self.after_stories()
        return [{'id':7,'expires':now()+3600}] if person['user_id']=='11111' else []

    async def premium_for_person(self,person):
        return self.premium

    async def react_story(self,person,story_id):
        self.reactions.append((person['user_id'],story_id))
        if self.fail:
            raise self.fail

    async def register_views(self,person,ids):
        self.calls.append((person['user_id'],ids))
        if self.fail:
            raise self.fail
        return True

    async def import_tdata(self,folder,api_id,api_hash,passcode,password):
        assert (folder/'key_datas').exists()
        self.import_api=(api_id,api_hash)
        self.import_path=folder
        self.db.execute('INSERT INTO account VALUES(1,?,?,?,?,?)',
                         ('90000','Аккаунт','test_account',self.cipher.encrypt(b'secret-session'),now()))


@pytest.fixture
def setup(tmp_path):
    config=Config(tmp_path,'a-long-test-password','a-test-encryption-secret-that-is-long-enough',cookie_secure=False)
    app=create_app(config,gateway_factory=FakeGateway,run_worker=False)
    with TestClient(app) as client:
        yield client,app.state.db,app.state.gateway,app.state.worker,config


def login(client):
    result=client.post('/api/auth/login',json={'password':'a-long-test-password'})
    assert result.status_code==200
    client.headers['X-CSRF-Token']=result.json()['csrf']


def account(db,config):
    db.execute('INSERT INTO account VALUES(1,?,?,?,?,?)',('90000','Мой аккаунт','my_account',config.cipher().encrypt(b'secret-session'),now()))


def source(db):
    db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('source','@test_group','Тестовая группа','members',300,now()))


async def tick(db,worker):
    db.execute("UPDATE jobs SET next_run=0 WHERE status IN ('queued','waiting')")
    job_id=db.claim()
    if job_id:
        await worker.process(job_id)
    return job_id


def test_auth_and_csrf(setup):
    client,db,*_=setup
    assert client.get('/api/state').status_code==401
    assert client.post('/api/auth/login',json={'password':'wrong'}).status_code==401
    login(client)
    assert client.get('/api/state').status_code==200
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/sources',json={'ref':'@test_group'}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.post('/api/sources',json={'ref':'@test_group'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/sources',json={'ref':'@test_group'}).status_code==200
    assert db.one('SELECT COUNT(*) n FROM sources')['n']==1
    assert client.post('/api/auth/logout').status_code==200
    assert client.get('/api/state').status_code==401


def test_login_rate_limit(setup):
    client,*_=setup
    for _ in range(5):
        assert client.post('/api/auth/login',json={'password':'wrong'}).status_code==401
    assert client.post('/api/auth/login',json={'password':'a-long-test-password'}).status_code==429


@pytest.mark.parametrize('value,expected',[
    ('https://t.me/test_group','@test_group'),('@test_group','@test_group'),
    ('https://t.me/+abcdef123456','+abcdef123456'),('-100123456789','-100123456789')])
def test_chat_normalization(value,expected):
    assert normalize_chat(value)==expected


@pytest.mark.parametrize('value',['https://evil.example/test_group','https://t.me/test_group/123','../../etc/passwd','https://t.me/test?next=oops'])
def test_reject_invalid_chat(value):
    with pytest.raises(ValueError):
        normalize_chat(value)


def make_zip(entries):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        for name,content in entries.items():
            z.writestr(name,content)
    return out.getvalue()


def test_archive_only_auth_files_and_traversal(tmp_path):
    archive=tmp_path/'upload.zip'
    dest=tmp_path/'out'
    dest.mkdir()
    archive.write_bytes(make_zip({'tdata/key_datas':b'key','tdata/D877F783D5D3EF8Cs':b'account','tdata/cache/secret':b'cache'}))
    extract_tdata(archive,dest)
    assert sorted(p.name for p in dest.iterdir())==['D877F783D5D3EF8Cs','key_datas']
    archive.write_bytes(make_zip({'../outside':b'oops','tdata/key_datas':b'key'}))
    with pytest.raises(ArchiveError):
        extract_tdata(archive,dest)
    assert not (tmp_path/'outside').exists()


def test_archive_symlink_and_multiple_accounts_roots(tmp_path):
    archive=tmp_path/'upload.zip'
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        item=zipfile.ZipInfo('tdata/link')
        item.external_attr=(0o120777<<16)
        z.writestr(item,'/etc/passwd')
        z.writestr('tdata/key_datas','fake')
    archive.write_bytes(out.getvalue())
    with pytest.raises(ArchiveError):
        extract_tdata(archive,tmp_path)
    archive.write_bytes(make_zip({'one/key_datas':b'a','two/key_datas':b'b'}))
    with pytest.raises(ArchiveError):
        extract_tdata(archive,tmp_path)


def test_archive_accepts_windows_path_separators(tmp_path):
    archive=tmp_path/'windows.zip'
    archive.write_bytes(make_zip({'tdata\\key_datas':b'key','tdata\\D877F783D5D3EF8Cs':b'account'}))
    target=tmp_path/'out';target.mkdir()
    extract_tdata(archive,target)
    assert (target/'key_datas').read_bytes()==b'key'


def test_import_erases_archive_and_hides_credentials(setup):
    client,db,gateway,_,config=setup
    login(client)
    data=make_zip({'tdata/key_datas':b'key','tdata/D877F783D5D3EF8Cs':b'account'})
    response=client.post('/api/account/tdata',files={'file':('tdata.zip',data,'application/zip')},
                         data={'api_id':'123456','api_hash':'a'*32,'passcode':'local-secret','password':'twofa-secret'})
    assert response.status_code==200,response.text
    assert not gateway.import_path.exists()
    raw=db.account()['credential']
    assert b'secret-session' not in raw
    assert config.cipher().decrypt(raw)==b'secret-session'
    public=client.get('/api/state').text
    assert all(value not in public for value in ['secret-session','credential','twofa-secret','local-secret','peer'])
    assert client.post('/api/account/tdata',files={'file':('x.zip',data)},data={'api_id':'123','api_hash':'a'*32}).status_code==409


def test_large_request_rejected(setup):
    client,*_=setup
    login(client)
    response=client.post('/api/sources',content=b'x'*(129*1024),headers={'Content-Type':'application/json'})
    assert response.status_code==413


@pytest.mark.asyncio
async def test_scan_deduplicates_and_finds_active_stories(setup):
    _,db,gateway,worker,config=setup
    account(db,config)
    source(db)
    jid=db.create_job('scan',{'phase':'gather','cursor':0},'source')
    for _ in range(5):
        await tick(db,worker)
    job=db.job(jid)
    assert job['status']=='done'
    assert (job['collected'],job['checked'],job['found'])==(2,2,1)
    assert db.one('SELECT COUNT(*) n FROM people')['n']==2
    people=db.all('SELECT * FROM people')
    assert sum(db.public_person(p)['stories_count'] for p in people)==1


@pytest.mark.asyncio
async def test_view_rechecks_and_never_repeats_confirmed_story(setup):
    _,db,gateway,worker,config=setup
    account(db,config)
    source(db)
    sj=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(5):
        await tick(db,worker)
    for _ in range(2):
        jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
        await tick(db,worker)
        await tick(db,worker)
        assert db.job(jid)['status']=='done'
    assert gateway.calls==[('11111',[7])]
    assert db.one('SELECT COUNT(*) n FROM views')['n']==1


@pytest.mark.asyncio
async def test_flood_wait_blocks_other_jobs_and_survives_pause(setup):
    client,db,gateway,worker,config=setup
    login(client)
    account(db,config)
    source(db)
    jid=db.create_job('scan',{'phase':'gather'},'source')
    gateway.fail=FloodWaitError(request=None,capture=120)
    await tick(db,worker)
    job=db.job(jid)
    assert job['status']=='waiting'
    assert job['next_run']>=now()+119
    db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('other-source','@other_group','Другая группа','members',300,now()))
    other=db.create_job('scan',{'phase':'gather'},'other-source')
    assert db.claim() is None
    assert client.post(f'/api/jobs/{jid}/control',json={'action':'pause'}).status_code==200
    assert client.post(f'/api/jobs/{jid}/control',json={'action':'resume'}).status_code==200
    assert db.job(jid)['status']=='waiting'
    assert db.claim() is None


@pytest.mark.asyncio
async def test_pause_during_story_check_does_not_consume_view_target(setup):
    _,db,gateway,worker,config=setup
    account(db,config)
    source(db)
    sj=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(5):
        await tick(db,worker)
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    gateway.after_stories=lambda:db.update_job(jid,status='paused')
    await tick(db,worker)
    assert db.job(jid)['status']=='paused'
    assert db.job(jid)['payload']['position']==0
    assert not gateway.calls


@pytest.mark.asyncio
async def test_uncertain_view_is_skipped_even_in_a_new_job(setup):
    client,db,gateway,worker,config=setup
    login(client)
    account(db,config)
    source(db)
    scan_id=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(5):
        await tick(db,worker)
    async def uncertain(person,ids):
        gateway.calls.append((person['user_id'],ids))
        raise TimeoutError('A server answer was lost')
    gateway.register_views=uncertain
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert db.job(jid)['status']=='paused'
    assert db.job(jid)['payload']['uncertain']
    assert db.one('SELECT COUNT(*) n FROM views')['n']==0
    assert client.post(f'/api/jobs/{jid}/control',json={'action':'cancel'}).status_code==200
    jid2=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert len(gateway.calls)==1


def test_restart_recovery_is_conservative(setup):
    _,db,*_=setup
    a=db.create_job('scan',{'phase':'gather'})
    b=db.create_job('view',{'user_ids':[],'position':0})
    db.update_job(a,status='running')
    db.update_job(b,status='running')
    db.recover()
    assert db.job(a)['status']=='queued'
    assert db.job(b)['status']=='paused'


def test_export_sanitizes_formulas_and_omits_peer(setup):
    client,db,gateway,_,config=setup
    login(client)
    account(db,config)
    source(db)
    jid=db.create_job('scan',{})
    person=gateway.person(11111)|{'name':'=HYPERLINK("bad")'}
    db.save_person(person,'source',jid)
    result=client.get('/api/people/export?active=false')
    assert result.status_code==200
    assert "'=HYPERLINK" in result.text
    assert 'peer' not in result.text
    assert 'hash' not in result.text


def test_static_serves_and_session_cookie_is_http_only(setup):
    client,*_=setup
    assert client.get('/').status_code==200
    assert client.get('/assets/app.js').status_code==200
    response=client.post('/api/auth/login',json={'password':'a-long-test-password'})
    cookie=response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie


@pytest.mark.asyncio
@pytest.mark.parametrize('twofa',[False,True])
async def test_cloud_login_waits_before_accepting_token(twofa):
    event=asyncio.Event()
    started=False
    signed_in=[]

    class QR:
        token=b'fake-token'
        async def wait(self,timeout):
            nonlocal started
            started=True
            await event.wait()
            if twofa:
                raise SessionPasswordNeededError(request=None)

    class Cloud:
        async def qr_login(self):
            return QR()
        async def sign_in(self,password):
            signed_in.append(password)

    async def desktop(request):
        assert started,'Token accepted before the login listener was registered'
        event.set()

    await authorize_cloud(desktop,Cloud(),'test-twofa-password')
    assert signed_in==(['test-twofa-password'] if twofa else [])


def test_chunked_request_is_limited(setup):
    client,*_=setup
    login(client)
    def chunks():
        yield b'x'*65536
        yield b'x'*65536
        yield b'x'*1024
    result=client.post('/api/sources',content=chunks(),headers={'Content-Type':'application/json'})
    assert result.status_code==413


def test_private_site_bridge_requires_secret_identity_and_csrf(setup):
    client,db,_,_,config=setup
    config.bridge_secret='a-private-server-bridge-key'
    assert client.get('/api/state',headers={'X-Story-User':'owner'}).status_code==401
    client.headers['X-Story-Bridge-Secret']=config.bridge_secret
    client.headers['X-Story-User']='site-owner-id'
    response=client.get('/api/auth/me')
    assert response.status_code==200
    assert client.get('/api/state').json()['auth_mode']=='chatgpt'
    assert client.post('/api/sources',json={'ref':'@test_group'}).status_code==403
    client.headers['X-CSRF-Token']=response.json()['csrf']
    assert client.post('/api/sources',json={'ref':'@test_group'}).status_code==200


def test_tdata_import_uses_published_defaults_without_api_fields(setup):
    from opentele.api import API
    client,db,gateway,_,config=setup
    login(client)
    assert client.get('/health').json()['api_mode']=='public'
    state=client.get('/api/state').json()
    assert state['telegram_api_mode']=='public'
    assert state['telegram_api_configured'] is True
    data=make_zip({'tdata/key_datas':b'key','tdata/D877F783D5D3EF8Cs':b'account'})
    response=client.post('/api/account/tdata',files={'file':('tdata.zip',data,'application/zip')})
    assert response.status_code==200,response.text
    assert gateway.import_api==(API.TelegramDesktop.api_id,API.TelegramDesktop.api_hash)
    assert API.TelegramDesktop.api_hash not in client.get('/api/state').text


@pytest.mark.parametrize('partial',[{'api_id':'12345'},{'api_hash':'a'*32}])
def test_partial_api_override_does_not_mix_with_published_defaults(setup,partial):
    client,db,gateway,_,config=setup
    login(client)
    data=make_zip({'tdata/key_datas':b'key'})
    result=client.post('/api/account/tdata',files={'file':('tdata.zip',data)},data=partial)
    assert result.status_code==400
    assert db.account() is None


def test_own_api_pair_takes_priority_and_public_mode_can_be_disabled(setup):
    from app.telegram import api_credentials, TelegramError
    *_,config=setup
    assert api_credentials(config,12345,'a'*32)==(12345,'a'*32)
    config.telegram_api_id=45678
    config.telegram_api_hash='b'*32
    assert api_credentials(config)==(45678,'b'*32)
    config.telegram_api_id=0
    config.telegram_api_hash=''
    config.public_api_defaults=False
    with pytest.raises(TelegramError):
        api_credentials(config)
