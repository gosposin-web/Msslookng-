"""Exercise the actual Desktop parser offline with synthetic authorization bytes."""
import zipfile

import pytest
from opentele.api import API, UseCurrentSession
from opentele.td import TDesktop
from opentele.tl import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

from app.archive import extract_tdata


@pytest.mark.parametrize('passcode',['','synthetic-local-passcode'])
@pytest.mark.asyncio
async def test_desktop_archive_roundtrip_including_account_maps(tmp_path,passcode):
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
