import hashlib
import random
import struct

import httpx
import pytest

from app.checkpoint_codec import (BUNDLE_MAGIC, MAX_PATCHES, PATCH_HEADER,
                                  apply_patch, encode_patch, unpack_frames)
from app.db import DB
from app.snapshot import SnapshotError, SnapshotStore
from test_snapshot import Remote, config
from test_app import setup, login


class PageRemote(Remote):
    def __init__(self):
        super().__init__()
        self.uploads = []
        self.reject_patch = None

    def __call__(self, request):
        if request.url.path == '/internal/snapshot' and request.method in ('PUT', 'PATCH'):
            self.uploads.append((request.method, request.content))
        if request.url.path == '/internal/snapshot' and request.method == 'PATCH':
            assert request.headers['X-Story-Snapshot-Codec'] == 'sqlite-pages-v1'
            if self.reject_patch:
                response = httpx.Response(self.reject_patch)
            elif self.owner != request.headers['X-Story-Instance']:
                response = httpx.Response(409)
            elif self.etag != request.headers.get('if-match'):
                response = httpx.Response(412)
            else:
                frames = unpack_frames(self.body) + [request.content]
                self.body = BUNDLE_MAGIC + b''.join(struct.pack('>I', len(frame)) + frame for frame in frames)
                self.version += 1
                self.etag = '"' + str(self.version) + '"'
                response = httpx.Response(200, json={'ok': True}, headers={'ETag': self.etag})
        else:
            response = super().__call__(request)
        if request.url.path == '/internal/snapshot':
            response.headers['X-Story-Snapshot-Codec'] = 'sqlite-pages-v1'
        return response


def seed(db):
    rng = random.Random(31)
    with db.connect() as connection:
        connection.execute('CREATE TABLE checkpoint_fixture(id INTEGER PRIMARY KEY,payload BLOB)')
        connection.executemany('INSERT INTO checkpoint_fixture VALUES(?,?)',
                               [(i, rng.randbytes(512)) for i in range(400)])


async def start(tmp_path, remote):
    cfg = config(tmp_path)
    db = DB(cfg.data_dir / 'state.sqlite3')
    seed(db)
    store = SnapshotStore(db, cfg, transport=httpx.MockTransport(remote))
    assert await store.acquire()
    await store.restore()
    store.ready = True
    await store.flush()
    return db, store


@pytest.mark.asyncio
async def test_small_updates_transfer_pages_and_restore_after_complete_disk_loss(tmp_path):
    remote = PageRemote()
    db, store = await start(tmp_path / 'first', remote)
    full_size = len(remote.body)
    for i in range(12):
        db.execute('UPDATE checkpoint_fixture SET payload=? WHERE id=?', (b'change-' + bytes([i]), i))
        await store.flush()
    assert [method for method, _ in remote.uploads] == ['PUT'] + ['PATCH'] * 12
    patch_bytes = sum(len(payload) for method, payload in remote.uploads if method == 'PATCH')
    assert patch_bytes < full_size * 12 * 0.05
    assert len(unpack_frames(remote.body)) == 13
    assert b'change-' not in remote.body
    await store.close()

    cfg = config(tmp_path / 'fresh-disk')
    recovered = DB(cfg.data_dir / 'state.sqlite3')
    restored = SnapshotStore(recovered, cfg, transport=httpx.MockTransport(remote))
    assert await restored.acquire()
    await restored.restore()
    assert recovered.all('SELECT * FROM checkpoint_fixture') == db.all('SELECT * FROM checkpoint_fixture')
    assert restored.patch_count == 12
    await restored.client.aclose()


@pytest.mark.asyncio
async def test_failed_patch_does_not_advance_acknowledged_data_or_revision(tmp_path):
    remote = PageRemote()
    db, store = await start(tmp_path, remote)
    saved, previous, etag = store.saved, store.last_snapshot, store.etag
    db.execute('UPDATE checkpoint_fixture SET payload=? WHERE id=0', (b'pending-change',))
    remote.reject_patch = 412
    with pytest.raises(SnapshotError):
        await store.flush()
    assert not store.ready
    assert (store.saved, store.last_snapshot, store.etag) == (saved, previous, etag)
    await store.client.aclose()


@pytest.mark.asyncio
async def test_rejected_append_compacts_using_the_same_conditional_version(tmp_path):
    remote = PageRemote()
    db, store = await start(tmp_path, remote)
    remote.reject_patch = 413
    db.execute('UPDATE checkpoint_fixture SET payload=? WHERE id=0', (b'new-value',))
    await store.flush()
    assert [method for method, _ in remote.uploads] == ['PUT', 'PATCH', 'PUT']
    assert len(unpack_frames(remote.body)) == 1
    assert store.patch_count == 0
    assert store.saved == store.revision
    await store.client.aclose()


@pytest.mark.asyncio
async def test_chain_count_limit_forces_full_compaction(tmp_path):
    remote = PageRemote()
    db, store = await start(tmp_path, remote)
    store.patch_count = MAX_PATCHES
    db.execute('UPDATE checkpoint_fixture SET payload=? WHERE id=1', (b'compact',))
    await store.flush()
    assert [method for method, _ in remote.uploads] == ['PUT', 'PUT']
    assert store.patch_count == 0
    await store.client.aclose()


@pytest.mark.asyncio
async def test_legacy_gateway_keeps_full_checkpoint_protocol(tmp_path):
    remote = Remote()
    db, store = await start(tmp_path, remote)
    assert not store.supports_patches
    db.execute('UPDATE checkpoint_fixture SET payload=? WHERE id=0', (b'legacy',))
    await store.flush()
    assert not remote.body.startswith(BUNDLE_MAGIC)
    await store.close()


@pytest.mark.asyncio
async def test_corrupted_authenticated_patch_does_not_replace_existing_local_database(tmp_path):
    remote = PageRemote()
    db, store = await start(tmp_path / 'first', remote)
    db.execute('UPDATE checkpoint_fixture SET payload=? WHERE id=0', (b'change',))
    await store.flush()
    await store.close()
    base, patch = unpack_frames(remote.body)
    broken = bytearray(patch)
    broken[30] ^= 1
    remote.body = BUNDLE_MAGIC + struct.pack('>I', len(base)) + base + struct.pack('>I', len(broken)) + broken
    cfg = config(tmp_path / 'existing-local')
    local = DB(cfg.data_dir / 'state.sqlite3')
    local.event('Existing local data remains')
    restored = SnapshotStore(local, cfg, transport=httpx.MockTransport(remote))
    assert await restored.acquire()
    with pytest.raises(SnapshotError):
        await restored.restore()
    assert local.one('SELECT message FROM events')['message'] == 'Existing local data remains'
    assert restored.last_snapshot is None
    await restored.client.aclose()


def test_page_patch_handles_growth_and_shrink_and_rejects_wrong_base(tmp_path):
    cfg = config(tmp_path)
    db = DB(cfg.data_dir / 'state.sqlite3')
    store = SnapshotStore(db, cfg, transport=httpx.MockTransport(Remote()))
    before = store._snapshot_data()
    seed(db)
    grown = store._snapshot_data()
    assert apply_patch(before, encode_patch(before, grown)) == grown
    db.execute('DROP TABLE checkpoint_fixture')
    db.execute('VACUUM')
    smaller = store._snapshot_data()
    patch = encode_patch(grown, smaller)
    assert len(smaller) < len(grown)
    assert apply_patch(grown, patch) == smaller
    with pytest.raises(ValueError, match='Wrong patch base'):
        apply_patch(before, patch)


def test_patch_bounds_integrity_and_frame_lengths_are_validated(tmp_path):
    cfg = config(tmp_path)
    db = DB(cfg.data_dir / 'state.sqlite3')
    store = SnapshotStore(db, cfg, transport=httpx.MockTransport(Remote()))
    previous = store._snapshot_data()
    db.event('New event')
    current = store._snapshot_data()
    patch = encode_patch(previous, current)
    with pytest.raises(ValueError):
        apply_patch(previous, patch[:-1])
    corrupted = bytearray(patch)
    corrupted[-1] ^= 1
    with pytest.raises(ValueError):
        apply_patch(previous, corrupted)
    with pytest.raises(ValueError):
        apply_patch(previous, PATCH_HEADER.pack(b'ZSP1', 2**32 - 1, 4096, 0,
                                                hashlib.sha256(previous).digest(), b'0' * 32))
    for broken in [BUNDLE_MAGIC, BUNDLE_MAGIC + b'\0', BUNDLE_MAGIC + struct.pack('>I', 20) + b'short']:
        with pytest.raises(ValueError):
            unpack_frames(broken)


def test_large_private_state_is_compressed_on_the_wire(setup):
    client, db, *_ = setup
    for _ in range(80):
        db.create_job('scan', {'phase': 'check'})
    login(client)
    plain = client.get('/api/state', headers={'Accept-Encoding': 'identity'})
    compressed = client.get('/api/state', headers={'Accept-Encoding': 'gzip'})
    assert compressed.status_code == 200
    assert compressed.json() == plain.json()
    assert compressed.headers['Content-Encoding'] == 'gzip'
    assert int(compressed.headers['Content-Length']) < int(plain.headers['Content-Length']) / 5
