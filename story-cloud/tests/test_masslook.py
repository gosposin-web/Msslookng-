import json
from contextlib import contextmanager

import pytest
from telethon.errors import FloodWaitError

from app.db import now
from app.telegram import Page
from test_app import setup, account, source, tick, login


def start(client,db,config):
    account(db,config);source(db);login(client)
    response=client.post('/api/sources/source/masslook',json={'delay':5})
    assert response.status_code==200,response.text
    return response.json()['id']


@pytest.mark.asyncio
async def test_stream_views_five_before_next_page_and_drains_last_batch(setup):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config)
    trace=[];positive={str(i) for i in [10001,10003,10007,10012,10020,10030,10031]}
    async def gather(ref,mode,cursor,limit):
        trace.append(('page',cursor));assert limit<=25
        end=min(10038,10000+cursor+limit)
        return Page([gateway.person(uid) for uid in range(10000+cursor,end)],end-10000,end==10038,'Test group')
    async def stories(person):
        return [{'id':7,'expires':now()+3600}] if person['user_id'] in positive else []
    original=gateway.register_views
    async def register(person,ids):
        trace.append(('view',person['user_id']))
        return await original(person,ids)
    gateway.gather=gather;gateway.stories=stories;gateway.register_views=register
    highwater=0
    for _ in range(180):
        await tick(db,worker)
        highwater=max(highwater,db.one('SELECT COUNT(*) n FROM people')['n'])
        if db.job(jid)['status']=='done':break
    job=db.job(jid)
    assert job['status']=='done'
    assert (job['collected'],job['checked'],job['found'],job['viewed'],job['viewed_users'],job['reacted'])==(38,38,7,7,7,7)
    second=trace.index(('page',25))
    assert len([r for r in trace[:second] if r[0]=='view'])==5
    assert {uid for uid,_ in gateway.calls}==positive
    assert highwater<=29
    assert db.one('SELECT COUNT(*) n FROM people')['n']==0
    assert db.one('SELECT COUNT(*) n FROM masslook_seen')['n']==0
    assert db.one('SELECT COUNT(*) n FROM job_people')['n']==0
    assert db.one('SELECT COUNT(*) n FROM views')['n']==7


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted',[False,None])
async def test_negative_acknowledgement_does_not_create_view_or_reaction(setup,accepted):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config)
    async def register(person,ids):return accepted
    gateway.register_views=register
    for _ in range(12):
        await tick(db,worker)
        if db.job(jid)['status']=='paused':break
    job=db.job(jid)
    assert job['status']=='paused' and job['viewed']==0 and job['viewed_users']==0
    assert job['unconfirmed']==1
    assert db.one('SELECT COUNT(*) n FROM views')['n']==0
    assert db.one('SELECT COUNT(*) n FROM view_attempts')['n']==0
    assert gateway.reactions==[]


@pytest.mark.asyncio
async def test_uncertain_result_is_visible_but_never_counted_or_retried(setup):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config)
    async def register(person,ids):
        gateway.calls.append((person['user_id'],ids))
        raise ConnectionError('Synthetic missing response')
    gateway.register_views=register
    for _ in range(12):
        await tick(db,worker)
        if db.job(jid)['status']=='paused':break
    row=client.get('/api/people').json()['people'][0]
    assert row['viewed_count']==0 and row['unconfirmed_count']==1
    assert db.job(jid)['viewed']==0
    assert client.post('/api/jobs/'+jid+'/control',json={'action':'resume'}).status_code==200
    for _ in range(12):await tick(db,worker)
    assert len(gateway.calls)==1
    assert db.job(jid)['status']=='done' and db.job(jid)['viewed']==0
    assert db.one('SELECT COUNT(*) n FROM people')['n']==0
    assert db.one('SELECT COUNT(*) n FROM view_attempts')['n']==1


@pytest.mark.asyncio
async def test_flood_wait_preserves_batch_and_uses_account_deadline(setup):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config);original=gateway.register_views;attempts=0
    async def register(person,ids):
        nonlocal attempts
        attempts+=1
        if attempts==1:raise FloodWaitError(None,77)
        return await original(person,ids)
    gateway.register_views=register
    for _ in range(12):
        await tick(db,worker)
        if attempts:break
    assert db.job(jid)['status']=='waiting'
    assert db.one("SELECT value FROM runtime WHERE key='telegram_wait_until'")['value']>=now()+76
    assert db.claim() is None
    assert not db.job(jid)['payload'].get('uncertain')
    db.execute('DELETE FROM runtime')
    for _ in range(12):await tick(db,worker)
    assert db.job(jid)['viewed']==1 and attempts==2


def test_only_one_masslook_chat_per_account_runs_while_batch_waits(setup):
    client,db,_,_,config=setup
    first=start(client,db,config)
    db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('second','@another_group','Second','members',300,now()))
    second=client.post('/api/sources/second/masslook',json={'delay':5}).json()['id']
    db.update_job(first,next_run=now()+200,status='waiting')
    assert db.claim() is None
    db.update_job(first,status='paused',next_run=0)
    assert db.claim() is None
    db.update_job(first,status='done')
    assert db.claim()==second


@pytest.mark.asyncio
async def test_cancel_cleans_transient_people_but_keeps_manual_selection(setup):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config)
    for _ in range(2):await tick(db,worker)
    queued=client.post('/api/jobs/view',json={'user_ids':['11111'],'delay':5})
    assert queued.status_code==200
    assert client.post('/api/jobs/'+jid+'/control',json={'action':'cancel'}).status_code==200
    assert db.one('SELECT is_transient FROM people WHERE user_id=?',('11111',))['is_transient']==0
    assert db.one('SELECT COUNT(*) n FROM people')['n']==1
    for _ in range(8):await tick(db,worker)
    assert db.job(queued.json()['id'])['viewed']==1


@pytest.mark.asyncio
async def test_legacy_scan_is_still_collection_only(setup):
    client,db,gateway,worker,config=setup
    account(db,config);source(db)
    jid=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(8):await tick(db,worker)
    assert db.job(jid)['found']==1 and db.job(jid)['viewed']==0
    assert gateway.calls==[]


@pytest.mark.asyncio
async def test_reported_4600_people_and_55_authors_are_counted_without_a_large_audience(setup,monkeypatch):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config)
    db.execute('UPDATE sources SET max_users=5000')
    # This stress case measures stream counters and bounded audience size. The
    # separate crash/intent tests cover durability; avoid thousands of fsyncs on
    # this disposable test database. Production connection settings stay intact.
    original=db.connect
    @contextmanager
    def fast_connection():
        with original() as c:
            c.execute('PRAGMA synchronous=OFF')
            yield c
    monkeypatch.setattr(db,'connect',fast_connection)
    positive={str(10000+i*83) for i in range(55)}
    async def gather(ref,mode,cursor,limit):
        end=min(4600,cursor+limit)
        return Page([gateway.person(10000+i) for i in range(cursor,end)],end,end==4600,'Synthetic 4600')
    async def stories(person):
        return [{'id':7,'expires':now()+3600}] if person['user_id'] in positive else []
    gateway.gather=gather;gateway.stories=stories
    with original() as keeper:
        keeper.execute('SELECT COUNT(*) FROM jobs').fetchone()
        highwater=0
        for _ in range(5700):
            await tick(db,worker)
            highwater=max(highwater,db.one('SELECT COUNT(*) n FROM people')['n'])
            if db.job(jid)['status']=='done':break
        job=db.job(jid)
        assert (job['status'],job['collected'],job['checked'],job['found'],job['viewed'],job['viewed_users'])==('done',4600,4600,55,55,55)
        assert len(gateway.calls)==55 and highwater<=29
        assert db.one('SELECT COUNT(*) n FROM people')['n']==0

@pytest.mark.asyncio
async def test_negative_reply_during_cancellation_does_not_resurrect_job(setup):
    client,db,gateway,worker,config=setup
    jid=start(client,db,config)
    async def register(person,ids):
        db.update_job(jid,status='cancelled',message='Остановлено пользователем.')
        db.cleanup_masslook(jid,final=True)
        return False
    gateway.register_views=register
    for _ in range(12):await tick(db,worker)
    assert db.job(jid)['status']=='cancelled' and db.job(jid)['viewed']==0
    assert db.one('SELECT COUNT(*) n FROM people')['n']==0
