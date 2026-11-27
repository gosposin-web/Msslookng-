import asyncio
import inspect
import json
import re
import sqlite3

import pytest

from app.accounts import AccountDB, AccountHub, SCOPED_TABLES, account_slot, scoped_sql
from app.db import DB, now


def add_account(db, user_id):
    db.execute('INSERT INTO account VALUES(1,?,?,?,?,?)',
               (str(user_id), 'Synthetic account', 'test_account',
                b'synthetic-encrypted-credential', now()))


def test_legacy_account_and_audience_stay_in_slot_one(tmp_path):
    path = tmp_path / 'state.sqlite3'
    legacy = DB(path)
    add_account(legacy, 90001)
    legacy.execute('INSERT INTO people(user_id,name,username,peer,created_at) VALUES(?,?,?,?,?)',
                   ('12345', 'Synthetic person', 'test_person', b'synthetic-peer', now()))
    legacy.create_job('view', {'user_ids': ['12345'], 'position': 0, 'delay': 15})
    root = AccountDB(path)
    assert root.account()['user_id'] == '90001'
    assert root.one('SELECT COUNT(*) n FROM people')['n'] == 1
    assert root.one('SELECT COUNT(*) n FROM jobs')['n'] == 1
    for slot in range(2, 16):
        other = AccountDB(path, slot)
        assert other.account() is None
        assert other.one('SELECT COUNT(*) n FROM people')['n'] == 0
        assert other.one('SELECT COUNT(*) n FROM jobs')['n'] == 0


def test_fifteen_workspaces_allow_identical_target_and_source_ids(tmp_path):
    path = tmp_path / 'state.sqlite3'
    for slot in range(1, 16):
        db = AccountDB(path, slot)
        add_account(db, 90000 + slot)
        db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',
                   ('same-source-id', '@test_group', 'Group ' + str(slot), 'members', slot, now()))
        db.execute('INSERT INTO people(user_id,name,username,peer,created_at) VALUES(?,?,?,?,?)',
                   ('12345', 'Person ' + str(slot), 'test_person', b'synthetic-peer', now()))
        db.execute('INSERT INTO source_people VALUES(?,?)', ('same-source-id', '12345'))
    for slot in range(1, 16):
        db = AccountDB(path, slot)
        assert db.account()['user_id'] == str(90000 + slot)
        assert db.one('SELECT * FROM sources')['max_users'] == slot
        assert db.one('SELECT * FROM people')['name'] == 'Person ' + str(slot)
    AccountDB(path, 2).execute('DELETE FROM people')
    assert AccountDB(path, 2).one('SELECT COUNT(*) n FROM people')['n'] == 0
    assert AccountDB(path, 1).one('SELECT COUNT(*) n FROM people')['n'] == 1


def test_jobs_and_telegram_wait_are_account_local(tmp_path):
    path = tmp_path / 'state.sqlite3'
    first, second = AccountDB(path, 1), AccountDB(path, 2)
    one = first.create_job('scan', {'phase': 'gather'})
    two = second.create_job('scan', {'phase': 'gather'})
    first.execute("INSERT INTO runtime VALUES('telegram_wait_until',?)", (now() + 100,))
    assert first.claim() is None
    assert second.claim() == two
    assert second.job(one) is None
    assert first.job(two) is None


def test_panel_login_is_shared_but_account_data_is_not(tmp_path):
    path = tmp_path / 'state.sqlite3'
    first, fifth = AccountDB(path, 1), AccountDB(path, 5)
    first.execute('INSERT INTO logins VALUES(?,?,?)', ('synthetic-token-hash', 'synthetic-csrf', now() + 100))
    assert fifth.one('SELECT * FROM logins')['csrf'] == 'synthetic-csrf'
    add_account(first, 90001)
    assert fifth.account() is None


def test_identifiers_are_scoped_without_changing_literals_or_parameters(tmp_path):
    db = AccountDB(tmp_path / 'state.sqlite3', 4)
    add_account(db, 90004)
    with db.connect() as c:
        row = c.execute("SELECT 'account' AS literal,user_id FROM account").fetchone()
        assert row['literal'] == 'account' and row['user_id'] == '90004'
        assert c.cursor().execute('SELECT user_id FROM "account"').fetchone()['user_id'] == '90004'
        assert c.cursor().execute('SELECT user_id FROM [account]').fetchone()['user_id'] == '90004'
        c.execute('UPDATE account SET name=?', ('people',))
    assert db.account()['name'] == 'people'
    assert scoped_sql("SELECT 'people and jobs', people.user_id FROM people", 2) == (
        "SELECT 'people and jobs', account_2_people.user_id FROM account_2_people")


def test_backup_preserves_all_fifteen_accounts(tmp_path):
    path, backup = tmp_path / 'state.sqlite3', tmp_path / 'backup.sqlite3'
    root = AccountDB(path)
    for slot in range(1, 16):
        add_account(AccountDB(path, slot), 90000 + slot)
    target = sqlite3.connect(backup)
    try:
        with root.connect() as source:
            source.backup(target)
    finally:
        target.close()
    restored = AccountDB(backup)
    assert restored.account()['user_id'] == '90001'
    assert AccountDB(backup, 15).account()['user_id'] == '90015'


def test_legacy_restore_recreates_other_namespaces(tmp_path):
    path, old_path = tmp_path / 'state.sqlite3', tmp_path / 'older.sqlite3'
    root = AccountDB(path)
    add_account(DB(old_path), 80001)
    with sqlite3.connect(old_path) as source, sqlite3.connect(path) as target:
        source.backup(target)
    root.initialize_schema()
    assert root.account()['user_id'] == '80001'
    assert AccountDB(path, 5).account() is None
    assert AccountDB(path, 5).one('SELECT COUNT(*) n FROM people')['n'] == 0


def test_isolation_inventory_covers_the_existing_schema():
    tables = set(re.findall(r'CREATE TABLE IF NOT EXISTS (\w+)', inspect.getsource(DB.initialize_schema)))
    assert tables - {'logins', 'login_failures'} <= SCOPED_TABLES


@pytest.mark.parametrize('value', [0, 16, -1, True, '2', 1.5, None])
def test_invalid_slots_are_rejected(value):
    with pytest.raises(ValueError):
        account_slot(value)


class Gateway:
    def __init__(self, db, cipher):
        self.db = db
        self.import_stage = ''
        self.lock = asyncio.Lock()
        self.closed = False

    async def close(self):
        self.closed = True


class Worker:
    def __init__(self, db, gateway, storage):
        self.db, self.gateway, self.storage = db, gateway, storage


class Storage:
    def __init__(self, db):
        self.db = db
        self.revision = 0

    def changed(self):
        self.revision += 1


def test_context_switch_keeps_workers_and_mutable_fields_isolated(tmp_path):
    hub = AccountHub(tmp_path / 'state.sqlite3', None, Gateway, Worker, Storage)
    db, gateway = hub.proxy('databases'), hub.proxy('gateways')
    add_account(db, 90001)
    token = hub.bind(2)
    try:
        add_account(db, 90002)
        gateway.import_stage = 'second-account-import'
        assert db.account()['user_id'] == '90002'
        assert hub.workers[1].db.slot == 1
        assert hub.workers[2].db.slot == 2
    finally:
        hub.reset(token)
    assert db.account()['user_id'] == '90001'
    assert gateway.import_stage == ''
    assert hub.gateways[2].import_stage == 'second-account-import'
    assert hub.storage.revision > 0
    public = hub.public_accounts()
    assert {account['slot'] for account in public} == {1, 2}
    assert 'credential' not in json.dumps(public)
    assert hub.find_user(90002)['slot'] == 2


@pytest.mark.asyncio
async def test_concurrent_contexts_remain_separate(tmp_path):
    hub = AccountHub(tmp_path / 'state.sqlite3', None, Gateway, Worker)
    proxy = hub.proxy('databases')
    async def request(slot):
        token = hub.bind(slot)
        try:
            await asyncio.sleep(0)
            assert proxy.slot == slot
            return proxy.slot
        finally:
            hub.reset(token)
    assert await asyncio.gather(*(request(slot) for slot in range(1, 16))) == list(range(1,16))
    assert proxy.slot == 1
    await hub.close()
    assert all(gateway.closed for gateway in hub.gateways.values())
