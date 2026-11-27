import asyncio
import sqlite3
from contextlib import contextmanager

import pytest
from telethon import errors

from app.account_access import AccountAccessError
from app.db import now
from test_app import setup,account,source,tick


@pytest.mark.asyncio
@pytest.mark.parametrize('exc',[errors.SessionRevokedError(None),errors.UserDeactivatedBanError(None),
                              AccountAccessError('ACCOUNT_FROZEN','Аккаунт заморожен.')])
async def test_account_wide_failure_pauses_all_queued_work(setup,exc):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    first=db.create_job('scan',{'phase':'gather'},'source')
    second=db.create_job('scan',{'phase':'gather'},'source')
    gateway.fail=exc
    await tick(db,worker)
    assert db.job(first)['status']=='paused' and db.job(second)['status']=='paused'
    assert db.claim() is None


@pytest.mark.asyncio
async def test_network_failure_has_bounded_retries(setup):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    jid=db.create_job('scan',{'phase':'gather'},'source')
    gateway.fail=ConnectionError('synthetic network failure')
    for _ in range(4):
        await tick(db,worker)
    assert db.job(jid)['status']=='failed'
    assert db.job(jid)['retries']==4


@pytest.mark.asyncio
async def test_worker_survives_a_temporary_claim_database_error(setup,monkeypatch):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    db.create_job('scan',{'phase':'gather'},'source')
    original_claim=db.claim;calls=0;processed=asyncio.Event()
    def flaky_claim():
        nonlocal calls
        calls+=1
        if calls==1:
            raise sqlite3.OperationalError('database is locked')
        return original_claim()
    original_sleep=asyncio.sleep
    async def fast_sleep(seconds):
        await original_sleep(0)
    original_process=worker.process
    async def process(jid):
        await original_process(jid)
        processed.set()
    monkeypatch.setattr(db,'claim',flaky_claim)
    monkeypatch.setattr(worker,'process',process)
    monkeypatch.setattr('app.worker.asyncio.sleep',fast_sleep)
    task=asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(processed.wait(),timeout=2)
        assert not task.done() and calls>=2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_restored_snapshot_cannot_receive_results_from_an_old_scan(setup):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    jid=db.create_job('scan',{'phase':'gather'},'source')
    class Storage:
        ready=True
        restore_generation=1
        async def flush(self):
            pass
    storage=Storage();worker.storage=storage
    original=gateway.gather
    async def gather(*args):
        result=await original(*args)
        storage.restore_generation=2
        return result
    gateway.gather=gather
    await tick(db,worker)
    assert db.job(jid)['status']=='paused'
    assert db.one('SELECT COUNT(*) n FROM people')['n']==0


@pytest.mark.asyncio
async def test_snapshot_loss_after_view_keeps_uncertain_intent_and_never_repeats(setup):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    scan=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(5):
        await tick(db,worker)
    class Storage:
        ready=True
        restore_generation=1
        async def flush(self):
            pass
    storage=Storage();worker.storage=storage
    async def register(person,ids):
        gateway.calls.append((person['user_id'],ids))
        storage.restore_generation+=1
        return True
    gateway.register_views=register
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert db.job(jid)['status']=='paused'
    assert db.job(jid)['payload']['uncertain']
    assert db.one('SELECT COUNT(*) n FROM view_attempts')['n']==1
    other=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert len(gateway.calls)==1


@pytest.mark.asyncio
async def test_database_error_during_scan_is_retried(setup):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    jid=db.create_job('scan',{'phase':'gather'},'source')
    original=gateway.gather;attempts=0
    async def flaky(*args):
        nonlocal attempts
        attempts+=1
        if attempts==1:
            raise sqlite3.OperationalError('database is locked')
        return await original(*args)
    gateway.gather=flaky
    await tick(db,worker)
    assert db.job(jid)['status']=='waiting'
    for _ in range(5):
        await tick(db,worker)
    assert db.job(jid)['status']=='done'


@pytest.mark.asyncio
async def test_database_failure_after_view_keeps_intent_and_pauses(setup,monkeypatch):
    _,db,gateway,worker,config=setup
    account(db,config);source(db)
    scan=db.create_job('scan',{'phase':'gather'},'source')
    for _ in range(5):
        await tick(db,worker)
    original=db.connect;failed=False
    class Connection:
        def __init__(self,real):
            self.real=real
        def execute(self,sql,params=()):
            nonlocal failed
            if 'INSERT OR IGNORE INTO views ' in sql and not failed:
                failed=True
                raise sqlite3.OperationalError('synthetic write failure')
            return self.real.execute(sql,params)
    @contextmanager
    def connection():
        with original() as real:
            yield Connection(real)
    monkeypatch.setattr(db,'connect',connection)
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert db.job(jid)['status']=='paused'
    assert db.job(jid)['payload']['uncertain']
    assert db.one('SELECT COUNT(*) n FROM view_attempts')['n']==1
    other=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert len(gateway.calls)==1
