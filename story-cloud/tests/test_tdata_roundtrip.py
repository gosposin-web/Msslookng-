"""Exercise the actual Desktop parser offline with synthetic authorization bytes."""
import json
import zipfile

import pytest
from opentele.api import API, UseCurrentSession
from opentele.td import TDesktop
from opentele.tl import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession
from telethon import functions, types

from app.archive import extract_tdata
from app.config import Config
from app.db import DB
from app.telegram import TelegramGateway, create_cloud_client


@pytest.mark.parametrize('passcode',['','synthetic-local-passcode'])
@pytest.mark.asyncio
async def test_desktop_archive_roundtrip_including_account_maps(tmp_path,passcode,monkeypatch):
    memory=MemorySession()
    memory.set_dc(2,'149.154.167.51',443)
    memory.auth_key=AuthKey(bytes(range(256)))
    client=TelegramClient(memory,api=API.TelegramDesktop)
    client.UserId=123456789
    original=tmp_path/'original'
    original.mkdir()
    # UseCurrentSession here only serializes a synthetic in-memory key. No network calls.
    desktop=await TDesktop.FromTelethon(client,flag=UseCurrentSession,api=API.TelegramDesktop)
    desktop.SaveTData(str(original),passcode=passcode)
    archive=tmp_path/'desktop.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for path in original.rglob('*'):
            if path.is_file():
                z.write(path,'tdata/'+path.relative_to(original).as_posix())
        z.writestr('tdata/cache/private-media',b'not needed')
    extracted=tmp_path/'extracted'
    extracted.mkdir()
    extract_tdata(archive,extracted)
    parsed=TDesktop(str(extracted),passcode=passcode)
    assert parsed.isLoaded()
    assert parsed.mainAccount.UserId==123456789
    assert parsed.mainAccount.authKey.key==bytes(range(256))
    assert list(extracted.glob('*/maps'))
    assert not (extracted/'cache').exists()
    assert not client.is_connected()

    # Exercise the real SDK constructors after opentele patched Telethon. Only the
    # network boundary is replaced; synthetic keys never leave this process.
    connections=[]
    logouts=[]
    async def connect(client):
        connections.append(client)
    async def logout(client):
        logouts.append(client)
    async def no_network(*args,**kwargs):
        raise AssertionError('Offline test attempted to open a network connection')
    async def inspect(client):
        return types.User(123456789,is_self=True,first_name='Synthetic')
    async def authorize(original,cloud,password):
        assert cloud.api_id==123456 and cloud.api_hash=='a'*32
        assert bytes(functions.auth.ExportLoginTokenRequest(cloud.api_id,cloud.api_hash,[]))
        cloud.session.set_dc(2,'149.154.167.51',443)
        cloud.session.auth_key=AuthKey(bytes(range(256)))
    monkeypatch.setattr(TelegramClient,'connect',connect)
    monkeypatch.setattr(TelegramClient,'log_out',logout)
    monkeypatch.setattr('app.telegram.TelegramClient.connect',connect)
    monkeypatch.setattr('app.telegram.TelegramClient.log_out',logout)
    monkeypatch.setattr('telethon.network.mtprotosender.MTProtoSender.connect',no_network)
    monkeypatch.setattr('app.telegram.inspect_account',inspect)
    monkeypatch.setattr('app.telegram.authorize_cloud',authorize)
    config=Config(tmp_path,'synthetic-password','a-synthetic-test-encryption-secret-over-32')
    db=DB(tmp_path/'state.sqlite3')
    gateway=TelegramGateway(db,config.cipher())
    await gateway.import_tdata(extracted,123456,'a'*32,passcode,'')
    assert len(connections)==2
    saved=json.loads(config.cipher().decrypt(db.account()['credential']))
    assert saved['api_id']==123456 and saved['api_hash']=='a'*32
    assert saved['session']
    restored=await gateway.client_for_account()
    assert restored.api_id==123456 and restored.api_hash=='a'*32
    assert bytes(functions.auth.ExportLoginTokenRequest(restored.api_id,restored.api_hash,[]))
    await gateway.close()
    assert len(connections)==3 and logouts==[]


@pytest.mark.parametrize('receive_updates',[False,True])
@pytest.mark.asyncio
async def test_client_credentials_survive_opentele_constructor_patch(receive_updates):
    # Positional arguments would silently replace this hash with an integer in
    # opentele 1.15.1. Verify actual binary request creation, not just construction.
    client=create_cloud_client(MemorySession(),123456,'a'*32,receive_updates=receive_updates)
    try:
        assert client.api_id==123456
        assert client.api_hash=='a'*32
        assert bytes(functions.auth.ExportLoginTokenRequest(client.api_id,client.api_hash,[]))
    finally:
        await client.disconnect()
