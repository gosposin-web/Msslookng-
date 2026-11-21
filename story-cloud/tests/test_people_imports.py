import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from telethon import errors, types

from app.db import now
from app.people_imports import (MAX_PEOPLE_FILE, PeopleImportError, PeopleList,
                               normalize_person, parse_people_file, queue_people)
from app.telegram import PersonLookupError, TelegramGateway
from test_app import setup, login, account, source, tick


def upload(client, text, filename='people.txt', request_id=None):
    return client.post('/api/people/import', files={'file':(filename, text if isinstance(text,bytes) else text.encode())},
                       data={'request_id':request_id or uuid.uuid4().hex})


def fake_resolver(gateway, targets):
    async def resolve(ref):
        gateway.calls.append(ref)
        target = targets[ref]
        if isinstance(target, Exception):
            raise target
        return gateway.person(target)
    gateway.resolve_person = resolve


@pytest.mark.parametrize('value,expected', [('@User_one','@user_one'),('user_one','@user_one'),
    ('t.me/User_one','@user_one'),('https://telegram.me/User_one/','@user_one')])
def test_usernames_and_profile_links(value, expected):
    assert normalize_person(value)==expected


@pytest.mark.parametrize('value', ['-10012345','+79991234567','12345','https://t.me/+AbCdEf12345',
    'https://evil.example/name','https://t.me/user_one/42','https://t.me/user_one?start=1',
    '@@user_one','<img src=x onerror=alert(1)>','https://pass:secret@t.me/user_one'])
def test_non_profile_references_rejected(value):
    with pytest.raises(PeopleImportError):
        normalize_person(value)


@pytest.mark.parametrize('filename,text,encoding,expected',[
    ('people.txt','# users\n@User_one\nuser_two\nt.me/User_three\n@USER_ONE','utf-8-sig',['@user_one','@user_two','@user_three']),
    ('people.csv','Имя;username;Notes\nИмя;@User_one;private-note','cp1251',['@user_one']),
    ('people.csv','username\nuser_one\nuser_two','utf-16',['@user_one','@user_two']),
    ('people.csv','@user_one,@user_two','utf-8',['@user_one','@user_two']),
    ('people.csv','user_one,user_two','utf-8',['@user_one','@user_two']),
])
def test_phone_file_formats(filename,text,encoding,expected):
    result=parse_people_file(text.encode(encoding),filename)
    assert result.refs==expected
    assert 'private-note' not in str(result)


@pytest.mark.parametrize('data,filename', [(b'', 'empty.txt'),(b'username\n"unterminated','bad.csv'),
    (b'@user_one\x00','binary.txt'),(b'@user_one','list.zip'),(b'a'*(MAX_PEOPLE_FILE+1),'large.txt'),
    ('\n'.join(f'@user_{i:03}' for i in range(201)).encode(),'many.txt')])
def test_invalid_files_have_no_partial_acceptance(data,filename):
    with pytest.raises(PeopleImportError):
        parse_people_file(data,filename)


def test_partial_parse_deduplicates_and_bounds_errors():
    parsed=parse_people_file(('@user_one\nhttps://t.me/USER_ONE\n'+'https://evil.example/invalid\n'*30).encode(),'users.txt')
    assert parsed.refs==['@user_one'] and parsed.duplicates==1
    assert parsed.invalid_count==30 and len(parsed.invalid)==20


def test_endpoints_require_auth_account_csrf_and_idempotency(setup):
    client,db,_,_,config=setup
    assert upload(client,'@user_one').status_code==401
    login(client)
    assert client.post('/api/people',json={'username':'@user_one'}).status_code==409
    assert upload(client,'@user_one').status_code==409
    account(db,config)
    csrf=client.headers.pop('X-CSRF-Token')
    assert upload(client,'@user_one').status_code==403
    assert client.post('/api/people',json={'username':'@user_one'}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    rid=uuid.uuid4().hex
    first=upload(client,'@user_one\n@USER_ONE\nnot a username',request_id=rid)
    assert first.status_code==200
    assert first.json()['valid']==1 and first.json()['duplicates']==1 and first.json()['invalid_count']==1
    second=upload(client,'@user_one\n@USER_ONE\nnot a username',request_id=rid)
    assert second.json()['job_id']==first.json()['job_id'] and second.json()['replayed']
    assert upload(client,'@user_two',request_id=rid).status_code==409
    assert len(db.all("SELECT * FROM jobs"))==1
    assert not db.all('SELECT * FROM sources')
    assert upload(client,'@user_one',filename='list.zip').status_code==400
    assert upload(client,b'a'*(MAX_PEOPLE_FILE+1)).status_code==400


def test_single_and_parallel_requests_do_not_duplicate_jobs(setup):
    client,db,_,_,config=setup
    login(client);account(db,config)
    response=client.post('/api/people',json={'username':'t.me/User_one','request_id':'single-user-request'})
    assert response.status_code==200
    parsed=PeopleList(['@user_two'],0,[],0)
    def add(_):
        return queue_people(db,parsed,b'@user_two','parallel-user-request')
    with ThreadPoolExecutor(max_workers=4) as pool:
        result=list(pool.map(add,range(4)))
    assert len({r['job_id'] for r in result})==1
    assert len(db.all('SELECT * FROM jobs'))==2


@pytest.mark.asyncio
async def test_import_checks_stories_and_merges_with_chat_people(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config);source(db)
    scan=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(5):
        await tick(db,worker)
    fake_resolver(gateway,{'@user_one':11111,'@alias_user':11111,'@user_two':33333,
                           '@missing_user':PersonLookupError('Пользователь не найден.')})
    result=upload(client,'@user_one\n@alias_user\n@user_two\n@missing_user').json()
    for _ in range(10):
        await tick(db,worker)
    job=db.job(result['job_id'])
    assert job['status']=='done' and job['collected']==2 and job['checked']==2 and job['found']==1
    assert job['payload']['added']==1 and job['payload']['duplicate_users']==1 and job['payload']['rejected']==1
    assert len(db.all('SELECT * FROM people'))==3
    assert len(db.all('SELECT * FROM sources'))==1
    assert not [c for c in gateway.calls if isinstance(c,tuple)], 'Import must never register story views'
    manual=client.get('/api/people',params={'source':'manual','active':False}).json()
    assert {p['user_id'] for p in manual['people']}=={'11111','33333'}
    active=client.get('/api/people',params={'source':'manual','active':True}).json()
    assert active['total']==1
    state=client.get('/api/state').json()
    public=next(j for j in state['jobs'] if j['id']==job['id'])
    assert public['processed']==4 and public['total']==4 and public['rejected']==1
    assert public['import_errors'][0]['ref']=='@missing_user'
    assert 'payload' not in public and 'peer' not in json.dumps(state)


@pytest.mark.asyncio
async def test_200_user_list_is_resumable_without_duplicate_resolve(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    fake_resolver(gateway,{f'@user_{i:03}':10000+i for i in range(200)})
    result=upload(client,'\n'.join(f'@user_{i:03}' for i in range(200))).json()
    jid=result['job_id']
    await tick(db,worker)
    assert db.job(jid)['payload']['phase']=='check'
    # Simulate a process crash after persisting a resolved target.
    db.update_job(jid,status='running');db.recover()
    await tick(db,worker)
    assert gateway.calls.count('@user_000')==1
    client.post(f'/api/jobs/{jid}/control',json={'action':'pause'})
    assert db.claim() is None
    client.post(f'/api/jobs/{jid}/control',json={'action':'resume'})
    for _ in range(401):
        await tick(db,worker)
    job=db.job(jid)
    assert job['status']=='done' and job['checked']==200 and job['collected']==200
    assert len(db.all('SELECT * FROM people'))==200


@pytest.mark.asyncio
async def test_flood_wait_does_not_advance_cursor_or_allow_early_resume(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    fake_resolver(gateway,{'@user_one':errors.FloodWaitError(None,capture=90)})
    jid=upload(client,'@user_one').json()['job_id']
    await tick(db,worker)
    job=db.job(jid)
    assert job['status']=='waiting' and job['payload']['position']==0 and job['next_run']>=now()+89
    client.post(f'/api/jobs/{jid}/control',json={'action':'pause'})
    client.post(f'/api/jobs/{jid}/control',json={'action':'resume'})
    assert db.claim() is None and db.job(jid)['status']=='waiting'


@pytest.mark.asyncio
async def test_account_revocation_and_restore_generation_pause_import(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    fake_resolver(gateway,{'@user_one':errors.SessionRevokedError(None)})
    jid=upload(client,'@user_one').json()['job_id']
    await tick(db,worker)
    assert db.job(jid)['status']=='paused' and not db.all('SELECT * FROM people')
    class Storage:
        ready=True
        restore_generation=1
        async def flush(self): pass
    worker.storage=storage=Storage()
    async def resolve(ref):
        storage.restore_generation+=1
        return gateway.person(11111)
    gateway.resolve_person=resolve
    client.post(f'/api/jobs/{jid}/control',json={'action':'resume'})
    await tick(db,worker)
    assert db.job(jid)['status']=='paused' and not db.all('SELECT * FROM people')


@pytest.mark.asyncio
async def test_gateway_accepts_only_regular_accessible_users(setup):
    _,db,_,_,config=setup
    gateway=TelegramGateway(db,config.cipher())
    current=None
    class Client:
        async def get_entity(self,ref):
            if isinstance(current,Exception): raise current
            return current
    async def client(): return Client()
    gateway.client_for_account=client
    for current in [types.ChatEmpty(1),types.User(1,bot=True),types.User(1,deleted=True),
                    types.User(1,is_self=True),types.User(1),errors.UsernameNotOccupiedError(None)]:
        with pytest.raises(PersonLookupError):
            await gateway.resolve_person('@user_one')
    current=types.User(11111,access_hash=42,first_name='User',username='user_one')
    person=await gateway.resolve_person('@user_one')
    assert person['user_id']=='11111' and person['username']=='user_one'
    for current in [errors.FloodWaitError(None,capture=60),errors.SessionRevokedError(None),ConnectionError()]:
        with pytest.raises(type(current)):
            await gateway.resolve_person('@user_one')
