"""Bounded username lists and durable, idempotent audience imports."""
import csv
import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

from .db import now

MAX_PEOPLE_FILE = 512 * 1024
MAX_IMPORT_PEOPLE = 200
HEADERS = {'username', 'usernames', 'user', 'users', 'telegram', 'url', 'link',
           'юзернейм', 'юзернеймы', 'пользователь', 'пользователи', 'ссылка'}


class PeopleImportError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass
class PeopleList:
    refs: list[str]
    duplicates: int
    invalid: list[dict]
    invalid_count: int


def normalize_person(value):
    value = value.strip()
    if value.startswith(('t.me/', 'telegram.me/')):
        value = 'https://' + value
    if value.startswith(('http://', 'https://')):
        try:
            p = urlparse(value)
            valid = (p.hostname in ('t.me', 'telegram.me') and not p.query and not p.fragment
                     and not p.username and not p.password and p.port in (None, 80, 443))
        except ValueError:
            valid = False
        if not valid:
            raise PeopleImportError('Укажите @username пользователя или ссылку t.me/username.')
        value = p.path.strip('/')
    if value.startswith('@'):
        value = value[1:]
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise PeopleImportError('Нужен @username пользователя. Номер телефона, ID и приглашение в чат не подходят.')
    return '@' + value.lower()


def parse_people_file(data, filename):
    suffix = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if suffix not in ('txt', 'csv'):
        raise PeopleImportError('Нужен файл TXT или CSV со списком юзернеймов.')
    if not data or len(data) > MAX_PEOPLE_FILE:
        raise PeopleImportError('Выберите непустой файл размером до 512 КБ.')
    try:
        if data.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = data.decode('utf-16')
        else:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = data.decode('cp1251')
    except UnicodeError:
        raise PeopleImportError('Не удалось прочитать файл. Сохраните его в UTF-8.') from None
    if '\x00' in text or any(ord(c) < 32 and c not in '\t\n\r' for c in text):
        raise PeopleImportError('Нужен обычный текст TXT или CSV, без двоичных данных.')
    lines = text.splitlines()
    if len(lines) > 2000:
        raise PeopleImportError('Оставьте до 200 уникальных пользователей и не более 2000 строк.')
    reader = None
    if suffix == 'csv':
        sample = '\n'.join(s for s in lines if s.strip() and not s.lstrip().startswith('#'))[:8192]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=',;\t').delimiter
        except csv.Error:
            first = sample.splitlines()[0] if sample else ''
            delimiter = next((d for d in (';', '\t', ',') if d in first), ',')
        reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    refs = []; seen = set(); invalid = []; invalid_count = 0; duplicates = 0
    columns = None; first_row = True
    try:
        for number, row in enumerate(reader if reader is not None else ([s] for s in lines), 1):
            line = reader.line_num if reader is not None else number
            if not row or not any(c.strip() for c in row) or row[0].lstrip().startswith('#'):
                continue
            if first_row:
                first_row = False
                found = [i for i, c in enumerate(row) if re.sub(r'[\s_-]', '', c.strip().casefold()) in HEADERS]
                if found and not any(c.strip().startswith(('@', 'https://', 'http://', 't.me/', 'telegram.me/')) for c in row):
                    columns = found
                    continue
            cells = ([row[i].strip() for i in columns if i < len(row) and row[i].strip()]
                     if columns is not None else [c.strip() for c in row if c.strip()])
            # With no header, retain explicit references rather than unrelated CSV notes.
            explicit = [c for c in cells if c.startswith(('@', 'https://', 'http://', 't.me/', 'telegram.me/'))]
            if columns is None and len(cells) > 1 and explicit:
                cells = explicit
            for cell in cells or ['']:
                try:
                    ref = normalize_person(cell)
                except PeopleImportError as exc:
                    invalid_count += 1
                    if len(invalid) < 20:
                        invalid.append({'line': line, 'value': cell[:120], 'message': str(exc)})
                    continue
                if ref in seen:
                    duplicates += 1
                    continue
                seen.add(ref); refs.append(ref)
                if len(refs) > MAX_IMPORT_PEOPLE:
                    raise PeopleImportError('В одном списке допускается до 200 уникальных пользователей. Разделите файл.')
    except csv.Error:
        raise PeopleImportError('Проверьте кавычки в CSV или загрузите TXT: один username на строку.') from None
    if not refs:
        raise PeopleImportError('В файле нет корректных юзернеймов. Используйте @username или t.me/username.')
    return PeopleList(refs, duplicates, invalid, invalid_count)


def queue_people(db, parsed, data, request_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
        raise PeopleImportError('Обновите форму и повторите добавление.')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        account = c.execute('SELECT user_id FROM account WHERE id=1').fetchone()
        if not account:
            raise PeopleImportError('Сначала подключите Telegram-аккаунт во вкладке «Аккаунт».', 409)
        fingerprint = hashlib.sha256(data + b'\x00' + account['user_id'].encode()).hexdigest()
        previous = c.execute('SELECT fingerprint,result FROM people_imports WHERE id=?', (request_id,)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise PeopleImportError('Этот запрос уже использован для другого списка. Откройте новую форму.', 409)
            return json.loads(previous['result']) | {'replayed': True}
        pending = c.execute("SELECT COUNT(*) n FROM jobs WHERE kind='people_import' AND status IN ('queued','running','waiting','paused')").fetchone()['n']
        if pending >= 25:
            raise PeopleImportError('Уже есть 25 незавершённых списков пользователей. Завершите или остановите их.', 409)
        stamp = now(); job_id = uuid.uuid4().hex
        payload = {'refs': parsed.refs, 'position': 0, 'phase': 'resolve', 'rejected': 0,
                   'added': 0, 'duplicate_users': 0, 'errors': []}
        c.execute("INSERT INTO jobs(id,kind,status,payload,created_at,updated_at) VALUES(?,'people_import','queued',?,?,?)",
                  (job_id, json.dumps(payload), stamp, stamp))
        result = {'ok': True, 'import_id': request_id, 'job_id': job_id, 'valid': len(parsed.refs),
                  'duplicates': parsed.duplicates, 'invalid_count': parsed.invalid_count,
                  'invalid_rows': parsed.invalid, 'replayed': False}
        c.execute('INSERT INTO people_imports VALUES(?,?,?,?,?)',
                  (request_id, fingerprint, json.dumps(result, ensure_ascii=False), job_id, stamp))
        c.execute('INSERT INTO events(job_id,level,message,created_at) VALUES(?,?,?,?)',
                  (job_id, 'info', f'Проверка списка пользователей: {len(parsed.refs)} юзернеймов в очереди.', stamp))
        return result
