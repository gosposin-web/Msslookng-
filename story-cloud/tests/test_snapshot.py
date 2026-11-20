import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import Config
from app.db import DB, now
from app.snapshot import SnapshotError, SnapshotStore
from app.worker import Worker
from test_app import FakeGateway, account, source, tick


class Remote:
    def __init__(self):
        self.owner=None
        self.body=None
        self.etag=None
        self.version=0

    def __call__(self,request):
        owner=request.headers['X-Story-Instance']
        if request.url.path=='/internal/lease':
            if self.owner and self.owner!=owner:
                return httpx.Response(409)
            if request.method=='DELETE':
                self.owner=None
                return httpx.Response(200,json={'expires':0})
            self.owner=owner
            return httpx.Response(200,json={'expires':now()+90})
        if request.method=='GET':
            return httpx.Response(200,content=self.body,headers={'ETag':self.etag}) if self.body else httpx.Response(404)
        if request.method=='PUT':
            if self.owner!=owner:
                return httpx.Response(409)
            if self.etag and request.headers.get('if-match')!=self.etag:
                return httpx.Response(412)
            self.body=request.content
            self.version+=1
            self.etag='"'+str(self.version)+'"'
            return httpx.Response(200,json={'ok':True},headers={'ETag':self.etag})
        raise AssertionError('Unexpected request')


def config(path):
    c=Config(path,'test-password-long-enough','stable-test-encryption-key-over-32-characters',
             bridge_secret='test-bridge-secret',snapshot_site_url='https://private.test',snapshot_site_token='test-token')
    c.validate()
    return c


@pytest.mark.asyncio
async def test_cloud_checkpoint_survives_total_local_disk_loss(tmp_path):
    remote=Remote()
    transport=httpx.MockTransport(remote)
    c=config(tmp_path/'first')
    db=DB(c.data_dir/'state.sqlite3')
    store=SnapshotStore(db,c,transport=transport)
    assert await store.acquire()
    store.ready=True
    account(db,c)
    source(db)
    job=db.create_job('scan',{'phase':'check'},'source')
    db.execute('INSERT INTO view_attempts VALUES(?,?,?,?)',('90000','11111',7,now()))
    await store.flush()
    assert b'SQLite format' not in remote.body
    assert b'secret-session' not in remote.body
    await store.close()

    second=config(tmp_path/'after-sleep')
    db2=DB(second.data_dir/'state.sqlite3')
    restored=SnapshotStore(db2,second,transport=transport)
    assert await restored.acquire()
    await restored.restore()
    restored.ready=True
    assert db2.account()['user_id']=='90000'
    assert second.cipher().decrypt(db2.account()['credential'])==b'secret-session'
    assert db2.job(job)['payload']['phase']=='check'
    assert db2.viewed_ids('90000','11111')=={7}
    db2.event('After restore')
    await restored.flush()
    assert remote.version==2
    await restored.close()


@pytest.mark.asyncio
async def test_second_instance_cannot_overwrite_or_run(tmp_path):
    remote=Remote()
    transport=httpx.MockTransport(remote)
    c=config(tmp_path/'one')
    a=SnapshotStore(DB(c.data_dir/'state.sqlite3'),c,transport=transport)
    assert await a.acquire()
    a.ready=True
    await a.flush()
    c2=config(tmp_path/'two')
    b=SnapshotStore(DB(c2.data_dir/'state.sqlite3'),c2,transport=transport)
    assert not await b.acquire()
    b.ready=True
    b.deadline=now()+90
    with pytest.raises(SnapshotError):
        await b.flush()
    assert not b.ready
    await b.close()
    await a.close()


@pytest.mark.asyncio
async def test_wrong_encryption_key_does_not_silently_start_empty(tmp_path):
    remote=Remote()
    c=config(tmp_path/'one')
    transport=httpx.MockTransport(remote)
    db=DB(c.data_dir/'state.sqlite3')
    a=SnapshotStore(db,c,transport=transport)
    assert await a.acquire()
    a.ready=True
    account(db,c)
    await a.flush()
    await a.close()
    c2=config(tmp_path/'two')
    c2.encryption_secret='a-different-encryption-key-of-enough-length'
    db2=DB(c2.data_dir/'state.sqlite3')
    b=SnapshotStore(db2,c2,transport=transport)
    assert await b.acquire()
    with pytest.raises(SnapshotError):
        await b.restore()
    assert db2.account() is None
    await b.client.aclose()


@pytest.mark.asyncio
async def test_view_never_runs_until_intent_is_saved(tmp_path):
    c=config(tmp_path/'app')
    db=DB(c.data_dir/'state.sqlite3')
    gateway=FakeGateway(db,c.cipher())
    account(db,c)
    source(db)
    scan=db.create_job('scan',{})
    db.save_person(gateway.person(11111),'source',scan)
    db.update_job(scan,status='done')
    class FailingStorage:
        calls=0
        async def flush(self):
            self.calls+=1
            if self.calls>1:
                raise SnapshotError('Storage offline')
    worker=Worker(db,gateway,FailingStorage())
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    await tick(db,worker)
    assert gateway.calls==[]
    assert db.job(jid)['status']=='paused'
