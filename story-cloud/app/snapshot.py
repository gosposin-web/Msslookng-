"""Encrypted, conditional cloud checkpoints for an ephemeral free server."""
import asyncio
import os
import secrets
import sqlite3
import tempfile
import time
import zlib
from contextlib import suppress
from pathlib import Path

import httpx


class SnapshotError(RuntimeError):
    pass


class SnapshotStore:
    def __init__(self,db,config,transport=None):
        self.db=db
        self.config=config
        self.cipher=config.cipher()
        self.owner=secrets.token_hex(24)
        self.client=httpx.AsyncClient(base_url=config.snapshot_site_url,
            headers={'OAI-Sites-Authorization':'Bearer '+config.snapshot_site_token,
                     'X-Story-Cloud-Key':config.bridge_secret,'X-Story-Instance':self.owner},
            timeout=20,follow_redirects=False,transport=transport)
        self.revision=1
        self.saved=0
        self.etag=None
        self.ready=False
        self.error=''
        self.lock=asyncio.Lock()
        self.deadline=0
        self.db.on_change=self.changed

    def changed(self):
        self.revision+=1

    async def _request(self,method,path,**kwargs):
        try:
            response=await self.client.request(method,path,**kwargs)
        except Exception:
            raise SnapshotError('Облачное сохранение временно недоступно.') from None
        return response

    async def acquire(self):
        response=await self._request('POST','/internal/lease')
        if response.status_code==409:
            return False
        if response.status_code!=200:
            raise SnapshotError('Не удалось открыть облачное хранилище.')
        self.deadline=int(response.json()['expires'])
        return True

    def _snapshot(self):
        # SQLite backup captures the WAL consistently, then produces a self-contained file.
        with tempfile.TemporaryDirectory(prefix='story-checkpoint-') as folder:
            path=Path(folder)/'state.sqlite3'
            target=sqlite3.connect(path)
            try:
                with self.db.connect() as source:
                    source.backup(target)
                target.execute('PRAGMA journal_mode=DELETE')
            finally:
                target.close()
            data=path.read_bytes()
        if len(data)>32*1024*1024:
            raise SnapshotError('База превысила размер бесплатного режима.')
        return self.cipher.encrypt(zlib.compress(data,level=3))

    async def restore(self):
        response=await self._request('GET','/internal/snapshot')
        if response.status_code==404:
            self.etag=None
            return
        if response.status_code!=200 or len(response.content)>16*1024*1024:
            raise SnapshotError('Не удалось прочитать облачное сохранение.')
        try:
            encrypted=response.content
            compressed=self.cipher.decrypt(encrypted)
            decoder=zlib.decompressobj()
            data=decoder.decompress(compressed,32*1024*1024+1)
            if len(data)>32*1024*1024 or not decoder.eof or decoder.unused_data:
                raise ValueError('Invalid checkpoint size')
            if not data.startswith(b'SQLite format 3\x00'):
                raise ValueError('Invalid checkpoint')
            with tempfile.TemporaryDirectory(prefix='story-restore-') as folder:
                path=Path(folder)/'state.sqlite3'
                path.write_bytes(data)
                path.chmod(0o600)
                source=sqlite3.connect(path)
                try:
                    if source.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise ValueError('Checkpoint integrity failed')
                    with self.db.connect() as target:
                        source.backup(target)
                finally:
                    source.close()
            self.db.initialize_schema()
            self.etag=response.headers['etag']
        except Exception:
            raise SnapshotError('Сохранение не расшифровано. Проверьте постоянный ключ сервера.') from None

    async def flush(self):
        async with self.lock:
            if not self.ready or self.deadline<=time.time()+5:
                raise SnapshotError('Сервер восстанавливает облачное сохранение. Подождите.')
            if self.saved==self.revision:
                return
            revision=self.revision
            payload=self._snapshot()
            headers={'Content-Type':'application/octet-stream'}
            headers['If-Match' if self.etag else 'If-None-Match']=self.etag or '*'
            response=await self._request('PUT','/internal/snapshot',content=payload,headers=headers)
            if response.status_code in (409,412):
                self.ready=False
                raise SnapshotError('Сохранение занято другим экземпляром сервера. Задания приостановлены.')
            if response.status_code!=200:
                raise SnapshotError('Облачное сохранение не подтверждено. Новые просмотры приостановлены.')
            self.etag=response.headers['etag']
            self.saved=revision
            self.error=''

    async def run(self,on_lost=None):
        renewed=0
        while True:
            try:
                if not self.ready:
                    if not await self.acquire():
                        self.error='Предыдущий экземпляр сервера завершает работу.'
                        await asyncio.sleep(3)
                        continue
                    await self.restore()
                    self.ready=True
                    self.changed()
                    renewed=time.time()
                if time.time()-renewed>=20:
                    if not await self.acquire():
                        self.ready=False
                        if on_lost:
                            await on_lost()
                        continue
                    renewed=time.time()
                await self.flush()
            except asyncio.CancelledError:
                raise
            except SnapshotError as exc:
                self.error=str(exc)
                if self.deadline<=time.time()+10:
                    self.ready=False
                    if on_lost:
                        await on_lost()
            await asyncio.sleep(2)

    async def close(self):
        if self.ready:
            with suppress(SnapshotError):
                await self.flush()
            with suppress(SnapshotError):
                await self._request('DELETE','/internal/lease')
        await self.client.aclose()
