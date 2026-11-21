import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from telethon import errors

from app.chat_files import ChatFileError, MAX_CHAT_FILE, parse_chat_file
from app.db import now
from app.source_store import MAX_SOURCES, import_sources, start_scan, SourceStoreError
from test_app import setup, login, account, source, tick


def upload(client,text,*,start=True,request_id=None,filename='chats.txt',**options):
    data=text if isinstance(text,bytes) else text.encode()
    fields={'start_scans':str(start).lower(),'request_id':request_id or uuid.uuid4().hex,
            'mode':'members','max_users':'1'}|options
    return client.post('/api/sources/import',files={'file':(filename,data)},data=fields)


@pytest.mark.parametrize('filename,data,expected',[
    ('chats.txt',b'\xef\xbb\xbf# chats\r\nhttps://t.me/Group_one\r\n@Group_two\r\n-10012345678\r\n',
     ['@group_one','@group_two','-10012345678']),
    ('chats.txt',b't.me/group_one\ntelegram.me/group_two\njoinchat/AbCdEf123456\n',
     ['@group_one','@group_two','+AbCdEf123456']),
    ('chats.csv','Название;Ссылка\n"Первый; чат";https://t.me/group_one\nВторой;@group_two'.encode(),['@group_one','@group_two']),
    ('chats.csv','Название,username\nЗакрытый,@group_one'.encode('cp1251'),['@group_one']),
    ('chats.csv','chat_id\n-1001234567890'.encode('utf-16'),['-1001234567890']),
    ('chats.csv',b'Human title,https://t.me/group_one\nSecond title,@group_two', ['@group_one','@group_two']),
    ('chats.csv',b'chat,https://t.me/group_one', ['@group_one']),
    ('chats.csv',b'@group_one\n@group_two', ['@group_one','@group_two']),
])
def test_phone_and_spreadsheet_text_formats(filename,data,expected):
    result=parse_chat_file(data,filename)
    assert result.refs==expected
    assert result.invalid_count==0


def test_deduplication_does_not_fold_private_invite_tokens():
    result=parse_chat_file(b'@Group_one\nhttps://t.me/group_one\n+AbCdEf123456\n+abcdef123456','chats.txt')
    assert result.refs==['@group_one','+AbCdEf123456','+abcdef123456']
    assert result.duplicates==1


def test_invalid_rows_are_bounded_and_extra_csv_columns_are_not_retained():
    text='Title,Ссылка,Notes\nSecret title,@group_one,private-metadata\n'
    text+='\n'.join('Ignored,https://evil.example/abc,private-metadata' for _ in range(30))
    result=parse_chat_file(text.encode(),'chats.csv')
    assert result.refs==['@group_one']
    assert result.invalid_count==30 and len(result.invalid)==20
    assert 'private-metadata' not in str(result)
    assert 'Secret title' not in str(result)


@pytest.mark.parametrize('data,filename',[
    (b'', 'empty.txt'),
    (b'abc\x00def','binary.txt'),
    (b'hello','chats.xlsx'),
    (b'url\n"unterminated','bad.csv'),
    (b'https://evil.example/chat\nhttps://t.me/chat/123','bad.txt'),
    (b'x'*(MAX_CHAT_FILE+1),'large.txt'),
    ('\n'.join(f'@group_{i}' for i in range(201)).encode(),'many.txt'),
])
def test_bad_file_is_rejected_before_any_write(data,filename):
    with pytest.raises(ChatFileError):
        parse_chat_file(data,filename)


def test_upload_auth_csrf_limits_and_preconfiguration(setup):
    client,db,*_=setup
    assert upload(client,'@group_one').status_code==401
    login(client)
    assert upload(client,'@group_one').status_code==409
    assert db.all('SELECT * FROM sources')==[]
    csrf=client.headers.pop('X-CSRF-Token')
    assert upload(client,'@group_one',start=False).status_code==403
    client.headers['X-CSRF-Token']=csrf
    result=upload(client,'@group_one\n@group_two',start=False)
    assert result.status_code==200 and result.json()['added']==2
    assert result.json()['queued']==0
    assert not db.all('SELECT * FROM jobs')
    assert upload(client,b'x'*(MAX_CHAT_FILE+1),start=False).status_code==400
    assert upload(client,'@group_three',start=False,filename='wrong.zip').status_code==400


@pytest.mark.asyncio
async def test_200_chat_file_creates_independent_resumable_scans_and_deduplicates_people(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    text='\n'.join(f'@group_{i:03}' for i in range(200))
    request_id=uuid.uuid4().hex
    result=upload(client,text,request_id=request_id)
    assert result.status_code==200,result.text
    assert result.json()['added']==200 and result.json()['queued']==200
    again=upload(client,text,request_id=request_id)
    assert again.status_code==200 and again.json()['replayed']
    assert db.one('SELECT COUNT(*) n FROM jobs')['n']==200
    state=client.get('/api/state').json()
    assert len(state['sources'])==200 and len(state['jobs'])==200
    assert state['job_counts']['queued']==200
    assert state['source_imports'][0]['total']==200
    assert all(s['scan_status']=='queued' for s in state['sources'])
    for _ in range(601):
        if await tick(db,worker) is None:
            break
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE status='done'")['n']==200
    assert db.one('SELECT COUNT(*) n FROM people')['n']==1
    assert db.one('SELECT COUNT(*) n FROM source_people')['n']==200
    assert db.one('SELECT COUNT(*) n FROM job_people WHERE checked=1')['n']==200
    state=client.get('/api/state').json()
    assert state['source_imports'][0]['done']==200
    assert state['stats']['active']==1
    # Replaying a successful request after jobs finish still must not start scans again.
    assert upload(client,text,request_id=request_id).json()['replayed']
    assert db.one('SELECT COUNT(*) n FROM jobs')['n']==200


def test_existing_and_busy_chats_keep_one_unfinished_scan(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config);source(db)
    old=db.create_job('scan',{'phase':'gather'},'source')
    db.update_job(old,status='paused')
    result=upload(client,'@TEST_GROUP\n@new_group\n@NEW_GROUP\nhttps://evil.example',max_users='17',mode='authors')
    assert result.status_code==200
    data=result.json()
    assert (data['added'],data['existing'],data['queued'],data['busy'],data['duplicates'],data['invalid_count'])==(1,1,1,1,1,1)
    assert db.one('SELECT max_users FROM sources WHERE id=?',('source',))['max_users']==300
    assert db.one('SELECT mode,max_users FROM sources WHERE ref=?',('@new_group',))=={'mode':'authors','max_users':17}
    assert client.post(f"/api/source-imports/{data['import_id']}/control",json={'action':'cancel'}).status_code==200
    assert db.job(old)['status']=='paused'


def test_fingerprint_conflicts_and_rejected_files_are_atomic(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    key=uuid.uuid4().hex
    assert upload(client,'@group_one',request_id=key).status_code==200
    assert upload(client,'@group_two',request_id=key).status_code==409
    assert upload(client,'@group_one',request_id=key,max_users='20').status_code==409
    assert db.one('SELECT COUNT(*) n FROM sources')['n']==1
    assert upload(client,'\n'.join(f'@large_group_{i}' for i in range(201))).status_code==400
    assert db.one('SELECT COUNT(*) n FROM sources')['n']==1


def test_simultaneous_retries_are_idempotent(setup):
    client,db,gateway,worker,config=setup
    account(db,config)
    data=b'@group_one\n@group_two'
    parsed=parse_chat_file(data,'chats.txt');key=uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(import_sources,db,parsed,data,'members',10,True,key) for _ in range(2)]
        results=[future.result(timeout=10) for future in futures]
    assert sorted(r['replayed'] for r in results)==[False,True]
    assert db.one('SELECT COUNT(*) n FROM jobs')['n']==2


def test_two_manual_scan_requests_cannot_create_duplicate_jobs(setup):
    _,db,_,_,config=setup
    account(db,config);source(db)
    def start():
        try:
            return start_scan(db,'source')
        except SourceStoreError as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:start(),range(2)))
    assert sum(result==409 for result in results)==1
    assert db.one('SELECT COUNT(*) n FROM jobs')['n']==1


def test_total_source_limit_rolls_back_whole_import(setup):
    client,db,*_=setup
    login(client)
    with db.connect() as c:
        c.executemany('INSERT INTO sources VALUES(?,?,?,?,?,?)',
                      [(str(i),f'@existing_{i}','Existing','members',1,now()) for i in range(MAX_SOURCES)])
    result=upload(client,'@new_group',start=False)
    assert result.status_code==400
    assert db.one('SELECT COUNT(*) n FROM sources')['n']==MAX_SOURCES
    assert not db.all('SELECT * FROM source_imports')


def test_batch_controls_preserve_flood_wait_and_other_jobs(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config);source(db)
    outside=db.create_job('scan',{'phase':'gather'},'source')
    result=upload(client,'@group_one\n@group_two').json();batch=result['import_id']
    assert client.post(f'/api/source-imports/{batch}/control',json={'action':'pause'}).json()['changed']==2
    assert db.job(outside)['status']=='queued'
    deadline=now()+120
    db.execute("INSERT INTO runtime VALUES('telegram_wait_until',?)",(deadline,))
    assert client.post(f'/api/source-imports/{batch}/control',json={'action':'resume'}).status_code==200
    rows=db.all('SELECT j.* FROM jobs j JOIN source_import_jobs ij ON ij.job_id=j.id WHERE ij.import_id=?',(batch,))
    assert all(j['status']=='waiting' and j['next_run']>=deadline for j in rows)
    assert db.claim() is None
    assert client.post(f'/api/source-imports/{batch}/control',json={'action':'cancel'}).json()['changed']==2
    assert db.job(outside)['status']=='queued'


def test_resuming_old_failed_import_cannot_duplicate_a_new_scan(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    old=upload(client,'@group_one').json()['import_id']
    job=db.one('SELECT job_id FROM source_import_jobs WHERE import_id=?',(old,))['job_id']
    db.update_job(job,status='failed')
    assert upload(client,'@group_one').json()['queued']==1
    assert client.post(f'/api/source-imports/{old}/control',json={'action':'resume'}).status_code==409
    assert client.post(f'/api/jobs/{job}/control',json={'action':'resume'}).status_code==409
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE status='queued'")['n']==1


@pytest.mark.asyncio
async def test_one_inaccessible_chat_does_not_stop_the_file(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    original=gateway.gather
    async def gather(ref,*args):
        if ref=='@bad_group':
            raise errors.ChannelPrivateError(None)
        return await original(ref,*args)
    gateway.gather=gather
    assert upload(client,'@bad_group\n@good_group').status_code==200
    for _ in range(6):
        await tick(db,worker)
    statuses={r['ref']:r['status'] for r in db.all('SELECT s.ref,j.status FROM sources s JOIN jobs j ON j.source_id=s.id')}
    assert statuses=={'@bad_group':'failed','@good_group':'done'}
