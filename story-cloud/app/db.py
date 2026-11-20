import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


def now():
    return int(time.time())


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.on_change = None
        self.initialize_schema()
        self.path.chmod(0o600)

    def initialize_schema(self):
        # Also run after restoring a checkpoint written by an older version.
        with self.connect() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id=1), user_id TEXT NOT NULL,
                    name TEXT NOT NULL, username TEXT, credential BLOB NOT NULL,
                    created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS account_profile (
                    id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE CHECK(id=1),
                    first_name TEXT NOT NULL DEFAULT '', last_name TEXT NOT NULL DEFAULT '',
                    photo BLOB, photo_id TEXT NOT NULL DEFAULT '', photo_revision TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, ref TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                    mode TEXT NOT NULL, max_users INTEGER NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS people (
                    user_id TEXT PRIMARY KEY, name TEXT NOT NULL, username TEXT,
                    peer BLOB NOT NULL, stories TEXT NOT NULL DEFAULT '[]',
                    checked_at INTEGER, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS source_people (
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES people(user_id) ON DELETE CASCADE,
                    PRIMARY KEY(source_id,user_id));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT,
                    status TEXT NOT NULL, payload TEXT NOT NULL,
                    collected INTEGER NOT NULL DEFAULT 0, checked INTEGER NOT NULL DEFAULT 0,
                    found INTEGER NOT NULL DEFAULT 0, viewed INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    next_run INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
                    retries INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS job_people (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES people(user_id) ON DELETE CASCADE,
                    checked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id,user_id));
                CREATE TABLE IF NOT EXISTS views (
                    account_id TEXT NOT NULL, user_id TEXT NOT NULL, story_id INTEGER NOT NULL,
                    viewed_at INTEGER NOT NULL, PRIMARY KEY(account_id,user_id,story_id));
                CREATE TABLE IF NOT EXISTS view_attempts (
                    account_id TEXT NOT NULL, user_id TEXT NOT NULL, story_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, PRIMARY KEY(account_id,user_id,story_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, level TEXT NOT NULL,
                    message TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS logins (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS login_failures (
                    ip TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            changes=c.total_changes
            yield c
            c.commit()
            if c.total_changes!=changes and self.on_change:
                self.on_change()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def all(self, sql, params=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.connect() as c:
            return c.execute(sql, params).rowcount

    def account(self):
        return self.one('SELECT * FROM account WHERE id=1')

    def public_account(self):
        account = self.account()
        if not account:
            return None
        profile = self.one('SELECT first_name,last_name,photo_revision,updated_at,photo IS NOT NULL AS has_avatar FROM account_profile WHERE id=1')
        return {k:account[k] for k in ('user_id','name','username','created_at')} | {
            'first_name':profile['first_name'] if profile else None,
            'last_name':profile['last_name'] if profile else None,
            'has_avatar':bool(profile and profile['has_avatar']),
            'photo_revision':profile['photo_revision'] if profile else '',
            'profile_loaded':bool(profile),
        }

    def event(self, message, job_id=None, level='info'):
        with self.connect() as c:
            c.execute('INSERT INTO events(job_id,level,message,created_at) VALUES(?,?,?,?)',
                      (job_id, level, message, now()))
            c.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)')

    def create_job(self, kind, payload, source_id=None):
        job_id = uuid.uuid4().hex
        self.execute('''INSERT INTO jobs(id,kind,source_id,status,payload,created_at,updated_at)
                        VALUES(?,?,?,'queued',?,?,?)''',
                     (job_id, kind, source_id, json.dumps(payload), now(), now()))
        self.event('Задание добавлено в очередь.', job_id)
        return job_id

    def job(self, job_id):
        row = self.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        if row:
            row['payload'] = json.loads(row['payload'])
        return row

    def update_job(self, job_id, **fields):
        allowed = {'payload','status','collected','checked','found','viewed','message','next_run','retries'}
        if not set(fields) <= allowed:
            raise ValueError('Unknown job fields')
        if 'payload' in fields:
            fields['payload'] = json.dumps(fields['payload'])
        fields['updated_at'] = now()
        with self.connect() as c:
            c.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?',
                      [*fields.values(),job_id])

    def claim(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            wait = c.execute("SELECT value FROM runtime WHERE key='telegram_wait_until'").fetchone()
            if wait and wait['value']>now():
                return None
            row = c.execute("SELECT id FROM jobs WHERE status IN ('queued','waiting') AND next_run<=? ORDER BY created_at,id LIMIT 1", (now(),)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=?", (now(),row['id']))
            return row['id']

    def running(self, job_id):
        return bool(self.one("SELECT id FROM jobs WHERE id=? AND status='running'", (job_id,)))

    def requeue(self, job_id, delay=0):
        self.execute("UPDATE jobs SET status='queued',next_run=?,updated_at=? WHERE id=? AND status='running'", (now()+delay,now(),job_id))

    def recover(self):
        # A view RPC might have succeeded before a crash. Do not silently repeat it.
        self.execute("UPDATE jobs SET status='paused',message='Сервер перезапущен. Проверьте результат и продолжите вручную.',updated_at=? WHERE status='running' AND kind='view'", (now(),))
        self.execute("UPDATE jobs SET status='queued',updated_at=? WHERE status='running' AND kind='scan'", (now(),))

    def public_job(self, row):
        return {k: v for k,v in row.items() if k != 'payload'}

    def public_person(self, row):
        stories = [s for s in json.loads(row['stories']) if s['expires'] > now()]
        return {k:v for k,v in row.items() if k not in ('peer','stories')} | {
            'stories_count':len(stories), 'expires_at':max((s['expires'] for s in stories),default=None)}

    def save_person(self, person, source_id, job_id):
        with self.connect() as c:
            c.execute('''INSERT INTO people(user_id,name,username,peer,created_at) VALUES(?,?,?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,username=excluded.username,peer=excluded.peer''',
                      (person['user_id'],person['name'],person.get('username'),person['peer'],now()))
            c.execute('INSERT OR IGNORE INTO source_people VALUES(?,?)', (source_id,person['user_id']))
            c.execute('INSERT OR IGNORE INTO job_people(job_id,user_id) VALUES(?,?)', (job_id,person['user_id']))

    def viewed_ids(self, account_id, user_id):
        return {r['story_id'] for r in self.all('''SELECT story_id FROM views WHERE account_id=? AND user_id=?
                                                 UNION SELECT story_id FROM view_attempts WHERE account_id=? AND user_id=?''',
                                               (account_id,user_id,account_id,user_id))}
