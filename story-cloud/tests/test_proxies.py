import asyncio
import json

import pytest

from app.account_access import AccountAccessError
from app.proxies import ProxyStoreError,resolve_connection
from app.telegram import TelegramGateway
from test_app import setup,login


def proxy_body(**extra):
    return dict(label='My proxy',protocol='socks5',host='8.8.8.8',port=1080,username='synthetic-user',password='synthetic-secret')|extra


def add(client,**extra):
    response=client.post('/api/proxies',json=proxy_body(**extra))
    assert response.status_code==200,response.text
    return response.json()['proxy']


def test_encrypted_credentials_and_explicit_assignments_before_import(setup):
    client,db,_,_,_=setup;login(client)
    row=add(client)
    assert row['has_auth'] and row['assigned_slots']==[]
    assert 'synthetic-' not in json.dumps(row)
    raw=db.one('SELECT credential FROM network_proxies')['credential']
    assert b'synthetic-' not in raw
    assert client.post('/api/account/proxy',json={'proxy_id':row['id']}).status_code==200
    client.headers['X-Story-Account']='15'
    assert client.post('/api/account/proxy',json={'proxy_id':row['id']}).status_code==409
    assert client.patch('/api/proxies/'+row['id'],json={'allow_shared':True}).status_code==200
    assert client.post('/api/account/proxy',json={'proxy_id':row['id']}).status_code==200
    state=client.get('/api/state').json()
    assert state['account'] is None and state['proxy']['assigned_slots']==[1,15]
    assert client.patch('/api/proxies/'+row['id'],json={'allow_shared':False}).status_code==409
    assert client.delete('/api/proxies/'+row['id']).status_code==409
    assert client.post('/api/account/proxy',json={'proxy_id':None}).status_code==200
    assert client.patch('/api/proxies/'+row['id'],json={'allow_shared':False}).status_code==200
    assert 'synthetic-' not in client.get('/api/proxies').text


def test_edits_preserve_credentials_until_explicitly_cleared(setup):
    client,_,_,_,_=setup;login(client);row=add(client)
    store=client.app.state.hub.proxy_store;store.assign(1,row['id'])
    first=store.connection(1)
    assert client.patch('/api/proxies/'+row['id'],json={'label':'Renamed','port':3128}).status_code==200
    second=store.connection(1)
    assert second['password']==first['password'] and second['signature']!=first['signature']
    assert client.patch('/api/proxies/'+row['id'],json={'clear_auth':True}).status_code==200
    assert store.connection(1)['username'] is None and store.connection(1)['password'] is None
    assert not store.selected(1)['has_auth']


def test_registry_has_pagination_without_a_total_proxy_cap(setup):
    client,_,_,_,_=setup;login(client)
    store=client.app.state.hub.proxy_store
    for i in range(107):store.save(proxy_body(label='Proxy '+str(i)))
    first=client.get('/api/proxies?limit=100').json()
    rest=client.get('/api/proxies?offset=100&limit=100').json()
    assert first['total']==107 and len(first['proxies'])==100 and len(rest['proxies'])==7
    assert len({r['id'] for r in first['proxies']+rest['proxies']})==107


@pytest.mark.parametrize('host',['127.0.0.1','10.0.0.1','169.254.169.254','::1','localhost','x.local','https://proxy.test','user:secret@proxy.test'])
def test_private_or_embedded_credentials_are_rejected_without_echo(setup,host):
    client,_,_,_,_=setup;login(client)
    response=client.post('/api/proxies',json=proxy_body(host=host))
    assert response.status_code==400 and 'synthetic-secret' not in response.text


@pytest.mark.asyncio
async def test_dns_result_is_public_and_pinned(monkeypatch):
    loop=asyncio.get_running_loop()
    async def dns(*args,**kwargs):return [(None,None,None,None,('8.8.8.8',1080))]
    monkeypatch.setattr(loop,'getaddrinfo',dns)
    result=await resolve_connection(dict(addr='proxy.example',port=1080,signature='x',proxy_type='socks5'))
    assert result['addr']=='8.8.8.8' and 'signature' not in result
    async def private(*args,**kwargs):return [(None,None,None,None,('169.254.169.254',1080))]
    monkeypatch.setattr(loop,'getaddrinfo',private)
    with pytest.raises(ProxyStoreError):await resolve_connection(dict(addr='proxy.example',port=1080))


@pytest.mark.asyncio
async def test_proxy_failure_has_no_secret_or_direct_connection_fallback():
    class Client:
        calls=0
        async def connect(self):
            self.calls+=1
            raise ConnectionError('synthetic-secret at proxy.example')
    client=Client()
    with pytest.raises(AccountAccessError) as exc:
        await TelegramGateway.connect_transport(None,client,True)
    assert exc.value.code=='PROXY_CONNECT_FAILED'
    assert 'synthetic-secret' not in str(exc.value) and client.calls==1


def test_busy_account_prevents_proxy_reassignment_and_edit(setup):
    client,_,gateway,_,_=setup;login(client);row=add(client)
    store=client.app.state.hub.proxy_store;store.assign(1,row['id'])
    async def hold():await gateway.lock.acquire()
    asyncio.run(hold())
    try:
        assert client.post('/api/account/proxy',json={'proxy_id':None}).status_code==409
        assert client.patch('/api/proxies/'+row['id'],json={'label':'Should not save'}).status_code==409
        assert store.public(row['id'])['label']=='My proxy'
    finally:gateway.lock.release()
