"""Encrypted proxy registry shared by account slots; assignments are explicit."""
import asyncio
import ipaddress
import json
import re
import socket
import uuid

from .db import now


class ProxyStoreError(ValueError):
    def __init__(self,message,status=400):
        super().__init__(message)
        self.status=status


def create_proxy_schema(db):
    with db.connect() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS network_proxies(
                id TEXT PRIMARY KEY,label TEXT NOT NULL,protocol TEXT NOT NULL,
                host TEXT NOT NULL,port INTEGER NOT NULL,credential BLOB NOT NULL,
                has_auth INTEGER NOT NULL,allow_shared INTEGER NOT NULL DEFAULT 0,
                revision TEXT NOT NULL,created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS proxy_assignments(
                slot INTEGER PRIMARY KEY,proxy_id TEXT NOT NULL REFERENCES network_proxies(id));
        ''')


def validate_address(protocol,host,port):
    if protocol not in ('socks5','http'):
        raise ProxyStoreError('Поддерживаются SOCKS5 и HTTP CONNECT.')
    host=host.strip().strip('[]').rstrip('.').lower() if isinstance(host,str) else ''
    if not host or len(host)>253 or any(ch in host for ch in '/@?#\\\r\n\x00'):
        raise ProxyStoreError('Укажите только IP или имя сервера прокси, без ссылки и пароля.')
    try:
        ip=ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',host) or '..' in host:
            raise ProxyStoreError('Некорректный адрес прокси.')
        if host=='localhost' or host.endswith(('.localhost','.local','.internal')):
            raise ProxyStoreError('Для облачного подключения нужен публичный адрес прокси.')
    else:
        if not ip.is_global or ip.is_multicast:
            raise ProxyStoreError('Для облачного подключения нужен публичный адрес прокси.')
    if type(port) is not int or not 1<=port<=65535:
        raise ProxyStoreError('Порт прокси должен быть от 1 до 65535.')
    return host


class ProxyStore:
    def __init__(self,db,cipher):
        self.db,self.cipher=db,cipher
        create_proxy_schema(db)

    def assigned_slots(self,proxy_id):
        return [r['slot'] for r in self.db.all('SELECT slot FROM proxy_assignments WHERE proxy_id=? ORDER BY slot',(proxy_id,))]

    def public(self,proxy_id):
        row=self.db.one('SELECT id,label,protocol,host,port,has_auth,allow_shared,created_at FROM network_proxies WHERE id=?',(proxy_id,))
        if row:
            row['has_auth']=bool(row['has_auth']);row['allow_shared']=bool(row['allow_shared'])
            row['assigned_slots']=self.assigned_slots(proxy_id)
        return row

    def selected(self,slot):
        row=self.db.one('SELECT proxy_id FROM proxy_assignments WHERE slot=?',(slot,))
        return self.public(row['proxy_id']) if row else None

    def list(self,offset=0,limit=50):
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=100:
            raise ProxyStoreError('Недопустимая страница списка прокси.')
        rows=self.db.all('SELECT id FROM network_proxies ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?',(limit,offset))
        return {'proxies':[self.public(r['id']) for r in rows],
                'total':self.db.one('SELECT COUNT(*) n FROM network_proxies')['n'],'offset':offset}

    def save(self,body,proxy_id=None):
        current=self.db.one('SELECT * FROM network_proxies WHERE id=?',(proxy_id,)) if proxy_id else None
        if proxy_id and not current:raise ProxyStoreError('Прокси не найден.',404)
        protocol=body.get('protocol',current['protocol'] if current else 'socks5')
        host=validate_address(protocol,body.get('host',current['host'] if current else ''),body.get('port',current['port'] if current else None))
        port=body.get('port',current['port'] if current else None)
        label=body.get('label',current['label'] if current else '')
        if not isinstance(label,str) or len(label)>80 or any(ch in label for ch in '\r\n\x00'):
            raise ProxyStoreError('Название прокси — до 80 символов без переноса строки.')
        label=label.strip() or f'{protocol.upper()} {host}:{port}'
        shared=body.get('allow_shared',bool(current and current['allow_shared']))
        if type(shared) is not bool:raise ProxyStoreError('Укажите режим совместного использования прокси.')
        if type(body.get('clear_auth',False)) is not bool:raise ProxyStoreError('Укажите, нужно ли удалить авторизацию прокси.')
        auth=json.loads(self.cipher.decrypt(current['credential'])) if current else {'username':'','password':''}
        if body.get('clear_auth'):auth={'username':'','password':''}
        for key in ('username','password'):
            value=body.get(key)
            if value is not None:
                if not isinstance(value,str) or len(value)>512 or any(ch in value for ch in '\r\n\x00'):
                    raise ProxyStoreError('Логин и пароль прокси — до 512 символов без переноса строки.')
                auth[key]=value
        if auth['password'] and not auth['username']:raise ProxyStoreError('Укажите логин для пароля прокси.')
        pid=proxy_id or uuid.uuid4().hex
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if current:
                fresh=c.execute('SELECT revision FROM network_proxies WHERE id=?',(pid,)).fetchone()
                if not fresh or fresh['revision']!=current['revision']:
                    raise ProxyStoreError('Настройки прокси изменились в другой вкладке. Обновите список.',409)
            if not shared and c.execute('SELECT COUNT(*) n FROM proxy_assignments WHERE proxy_id=?',(pid,)).fetchone()['n']>1:
                raise ProxyStoreError('Сначала оставьте этот прокси только у одного аккаунта.',409)
            c.execute('''INSERT INTO network_proxies VALUES(?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET label=excluded.label,protocol=excluded.protocol,
                         host=excluded.host,port=excluded.port,credential=excluded.credential,
                         has_auth=excluded.has_auth,allow_shared=excluded.allow_shared,revision=excluded.revision''',
                (pid,label,protocol,host,port,self.cipher.encrypt(json.dumps(auth).encode()),int(bool(auth['username'])),int(shared),uuid.uuid4().hex,now()))
        return self.public(pid)

    def assign(self,slot,proxy_id):
        from .accounts import account_slot
        account_slot(slot)
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if proxy_id is None:
                c.execute('DELETE FROM proxy_assignments WHERE slot=?',(slot,))
            else:
                row=c.execute('SELECT allow_shared FROM network_proxies WHERE id=?',(proxy_id,)).fetchone()
                if not row:raise ProxyStoreError('Прокси не найден.',404)
                other=c.execute('SELECT slot FROM proxy_assignments WHERE proxy_id=? AND slot<>? LIMIT 1',(proxy_id,slot)).fetchone()
                if other and not row['allow_shared']:
                    raise ProxyStoreError(f'Прокси уже назначен аккаунту {other["slot"]}. Разрешите совместное использование или выберите другой.',409)
                c.execute('INSERT INTO proxy_assignments VALUES(?,?) ON CONFLICT(slot) DO UPDATE SET proxy_id=excluded.proxy_id',(slot,proxy_id))
        return self.selected(slot)

    def delete(self,proxy_id):
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM proxy_assignments WHERE proxy_id=?',(proxy_id,)).fetchone():
                raise ProxyStoreError('Сначала отключите этот прокси у назначенных аккаунтов.',409)
            if not c.execute('DELETE FROM network_proxies WHERE id=?',(proxy_id,)).rowcount:
                raise ProxyStoreError('Прокси не найден.',404)

    def connection(self,slot):
        row=self.db.one('SELECT p.* FROM network_proxies p JOIN proxy_assignments a ON a.proxy_id=p.id WHERE a.slot=?',(slot,))
        if not row:return None
        auth=json.loads(self.cipher.decrypt(row['credential']))
        return {'signature':row['id']+':'+row['revision'],'proxy_type':row['protocol'],
                'addr':row['host'],'port':row['port'],'username':auth['username'] or None,
                'password':auth['password'] or None,'rdns':True}


async def resolve_connection(settings):
    if not settings:return None
    # Pin the checked address to this connection, so DNS rebinding cannot turn
    # a configured public proxy into a connection to cloud metadata or localhost.
    try:
        addresses=await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            settings['addr'],settings['port'],type=socket.SOCK_STREAM),timeout=8)
        ips=[item[4][0] for item in addresses]
        if not ips or any(not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast for ip in ips):
            raise ValueError('Non-public proxy')
    except Exception:
        raise ProxyStoreError('Не удалось подтвердить публичный адрес прокси. Проверьте его настройки.') from None
    return {key:value for key,value in settings.items() if key!='signature'}|{'addr':ips[0]}
