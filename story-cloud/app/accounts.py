"""Five account-local workspaces inside the existing encrypted checkpoint."""
import asyncio
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .db import DB

MAX_ACCOUNTS = 5
SCOPED_TABLES = frozenset({
    'account', 'account_profile', 'sources', 'people', 'source_people',
    'jobs', 'job_people', 'views', 'view_attempts', 'events', 'runtime',
    'source_imports', 'source_import_jobs', 'people_imports',
    'reaction_tasks', 'channel_requests', 'channels',
})
SCOPED_INDEXES = frozenset({
    'jobs_queue', 'jobs_source_created', 'job_people_unchecked',
    'views_recent', 'view_attempts_recent', 'reaction_tasks_pending',
})
_NAMES = SCOPED_TABLES | SCOPED_INDEXES
_IDENTIFIER = re.compile(r'\b(?:' + '|'.join(sorted(_NAMES, key=len, reverse=True)) + r')\b', re.I)
_QUOTED = re.compile(r"""('(?:''|[^'])*'|"[^"]*"|`[^`]*`|\[[^\]]*\])""")


def account_slot(value):
    if type(value) is not int or not 1 <= value <= MAX_ACCOUNTS:
        raise ValueError('Аккаунт: выберите номер от 1 до 5.')
    return value


def scoped_sql(sql, slot):
    account_slot(slot)
    if slot == 1:
        return sql
    prefix = f'account_{slot}_'
    parts = _QUOTED.split(sql)
    for i, part in enumerate(parts):
        if i % 2:
            if not part or part.startswith("'"):
                continue
            inner = part[1:-1]
            if inner.casefold() in _NAMES:
                parts[i] = part[0] + prefix + inner.casefold() + part[-1]
        else:
            parts[i] = _IDENTIFIER.sub(lambda match: prefix + match.group(0).casefold(), part)
    return ''.join(parts)


class ScopedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        return super().execute(scoped_sql(sql, self.connection.slot), parameters)

    def executemany(self, sql, parameters, /):
        return super().executemany(scoped_sql(sql, self.connection.slot), parameters)

    def executescript(self, sql, /):
        return super().executescript(scoped_sql(sql, self.connection.slot))


class ScopedConnection(sqlite3.Connection):
    slot = 1

    def cursor(self, factory=ScopedCursor):
        return super().cursor(factory=factory)

    def execute(self, sql, parameters=(), /):
        return super().execute(scoped_sql(sql, self.slot), parameters)

    def executemany(self, sql, parameters, /):
        return super().executemany(scoped_sql(sql, self.slot), parameters)

    def executescript(self, sql, /):
        return super().executescript(scoped_sql(sql, self.slot))


class AccountDB(DB):
    def __init__(self, path: Path, slot=1):
        self.slot = account_slot(slot)
        super().__init__(path)

    def initialize_schema(self):
        super().initialize_schema()
        if self.slot == 1:
            # Also runs after restoring an older single-account checkpoint.
            for slot in range(2, MAX_ACCOUNTS + 1):
                AccountDB(self.path, slot)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10, factory=ScopedConnection)
        c.slot = self.slot
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            changes = c.total_changes
            yield c
            c.commit()
            if c.total_changes != changes and self.on_change:
                self.on_change()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()


class AccountHub:
    def __init__(self, path, cipher, gateway_factory, worker_factory, storage_factory=None):
        root = AccountDB(Path(path), 1)
        self.databases = {1: root}
        self.databases.update({slot: AccountDB(Path(path), slot) for slot in range(2, MAX_ACCOUNTS + 1)})
        self.storage = storage_factory(root) if storage_factory else None
        if self.storage:
            for db in self.databases.values():
                db.on_change = self.storage.changed
        self.gateways = {slot: gateway_factory(db, cipher) for slot, db in self.databases.items()}
        self.workers = {
            slot: worker_factory(db, self.gateways[slot], self.storage)
            for slot, db in self.databases.items()
        }
        self.import_lock = asyncio.Lock()
        from contextvars import ContextVar
        self._active = ContextVar('zavod_active_account', default=1)

    def bind(self, slot):
        return self._active.set(account_slot(slot))

    def reset(self, token):
        self._active.reset(token)

    def public_accounts(self):
        accounts = []
        for slot, db in self.databases.items():
            account = db.public_account()
            if account:
                accounts.append(account | {'slot': slot})
        return accounts

    def find_user(self, user_id):
        return next((account for account in self.public_accounts() if account['user_id'] == str(user_id)), None)

    async def close(self):
        await asyncio.gather(*(gateway.close() for gateway in self.gateways.values()))

    def proxy(self, collection):
        if collection not in ('databases', 'gateways', 'workers'):
            raise ValueError('Unknown account collection')
        return AccountProxy(self, collection)


class AccountProxy:
    __slots__ = ('_hub', '_collection')

    def __init__(self, hub, collection):
        object.__setattr__(self, '_hub', hub)
        object.__setattr__(self, '_collection', collection)

    def _target(self):
        return getattr(self._hub, self._collection)[self._hub._active.get()]

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)
