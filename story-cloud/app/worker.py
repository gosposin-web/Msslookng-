import asyncio
import json

from telethon.errors import FloodWaitError

from .db import now
from .telegram import friendly_error
from .snapshot import SnapshotError

ACTIVE = ('queued','running','waiting','paused')
TERMINAL = ('done','failed','cancelled')
VIEW_LIMIT_24H = 200


class Worker:
    def __init__(self, db, gateway, storage=None):
        self.db = db
        self.gateway = gateway
        self.storage = storage

    async def run(self):
        while self.storage and not self.storage.ready:
            await asyncio.sleep(1)
        self.db.recover()
        while True:
            if self.storage:
                try:
                    await self.storage.flush()
                except SnapshotError:
                    await asyncio.sleep(3)
                    continue
            job_id = self.db.claim()
            if not job_id:
                await asyncio.sleep(1)
                continue
            await self.process(job_id)

    async def process(self, job_id):
        job = self.db.job(job_id)
        if not job or job['status'] != 'running':
            return
        try:
            if self.storage:
                await self.storage.flush()
            async with self.gateway.lock:
                if not self.db.running(job_id):
                    return
                async with asyncio.timeout(45):
                    delay = await (self.scan(job) if job['kind']=='scan' else self.view(job))
            self.db.requeue(job_id,delay=delay)
            if self.storage:
                await self.storage.flush()
        except asyncio.CancelledError:
            raise
        except SnapshotError as exc:
            if self.db.running(job_id) or self.db.job(job_id)['status']=='queued':
                self.db.update_job(job_id,status='paused',message=str(exc))
        except FloodWaitError as exc:
            # Exact server-requested minimum. Resume cannot clear this deadline.
            seconds = max(1,int(exc.seconds))
            self.db.execute("INSERT INTO runtime VALUES('telegram_wait_until',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (now()+seconds,))
            if self.db.running(job_id):
                self.db.update_job(job_id,status='waiting',next_run=now()+seconds,
                                   message=f'Telegram попросил подождать {seconds} сек.')
                self.db.event(f'Ограничение Telegram: ожидание {seconds} сек.',job_id,'warning')
        except (TimeoutError,ConnectionError,OSError) as exc:
            retries = job['retries']+1
            if self.db.running(job_id):
                self.db.update_job(job_id,status='waiting' if retries<=3 else 'failed',
                                   retries=retries,next_run=now()+min(300,15*2**retries),
                                   message=friendly_error(exc))
        except Exception as exc:
            if self.db.running(job_id):
                message = friendly_error(exc)
                self.db.update_job(job_id,status='failed',message=message)
                self.db.event(message,job_id,'error')

    async def scan(self, job):
        p = job['payload']
        source = self.db.one('SELECT * FROM sources WHERE id=?',(job['source_id'],))
        if not source:
            self.db.update_job(job['id'],status='cancelled',message='Источник удалён.')
            return 0
        if p.get('phase','gather') == 'gather':
            count = self.db.one('SELECT COUNT(*) n FROM job_people WHERE job_id=?',(job['id'],))['n']
            if count>=source['max_users'] or p.get('pages',0)>=100:
                p['phase']='check'
                self.db.update_job(job['id'],payload=p)
                return 0
            page = await self.gateway.gather(source['ref'],source['mode'],p.get('cursor',0),source['max_users']-count)
            if not self.db.running(job['id']):
                return 0
            for person in page.people:
                self.db.save_person(person,source['id'],job['id'])
                count = self.db.one('SELECT COUNT(*) n FROM job_people WHERE job_id=?',(job['id'],))['n']
                if count>=source['max_users']:
                    break
            p['cursor']=page.cursor
            p['pages']=p.get('pages',0)+1
            if page.done or count>=source['max_users']:
                p['phase']='check'
            self.db.execute('UPDATE sources SET title=? WHERE id=?',(page.title,source['id']))
            self.db.update_job(job['id'],payload=p,collected=count,message='Собираем доступных пользователей.')
            return 2
        person = self.db.one('''SELECT p.* FROM people p JOIN job_people jp ON jp.user_id=p.user_id
                               WHERE jp.job_id=? AND jp.checked=0 ORDER BY p.user_id LIMIT 1''',(job['id'],))
        if not person:
            self.db.update_job(job['id'],status='done',message=f'Проверено {job["checked"]}; с историями: {job["found"]}.')
            self.db.event('Сбор завершён. Аудитория доступна для проверки.',job['id'])
            return 0
        stories = await self.gateway.stories(person)
        # Persist success before the next request. Pause after this RPC does not lose its result.
        with self.db.connect() as c:
            c.execute('UPDATE people SET stories=?,checked_at=? WHERE user_id=?',
                      (json.dumps(stories),now(),person['user_id']))
            c.execute('UPDATE job_people SET checked=1 WHERE job_id=? AND user_id=?',(job['id'],person['user_id']))
            c.execute('UPDATE jobs SET checked=checked+1,found=found+?,message=?,updated_at=? WHERE id=?',
                      (int(bool(stories)),'Проверяем доступные активные истории.',now(),job['id']))
        return 3

    async def view(self, job):
        p=job['payload']
        position=p.get('position',0)
        if position>=len(p['user_ids']):
            self.db.update_job(job['id'],status='done',message='Просмотр выбранной аудитории завершён.')
            self.db.event('Задание просмотра завершено.',job['id'])
            return 0
        account=self.db.account()
        if not account:
            raise ValueError('Account disconnected')
        recent=self.db.one('SELECT COUNT(*) n FROM views WHERE account_id=? AND viewed_at>?',
                           (account['user_id'],now()-86400))['n']
        recent+=self.db.one('SELECT COUNT(*) n FROM view_attempts WHERE account_id=? AND created_at>?',
                           (account['user_id'],now()-86400))['n']
        if recent>=VIEW_LIMIT_24H:
            self.db.update_job(job['id'],status='paused',message='Достигнут внутренний лимит: 200 историй за 24 часа.')
            return 0
        person=self.db.one('SELECT * FROM people WHERE user_id=?',(p['user_ids'][position],))
        count=0
        if person:
            stories=await self.gateway.stories(person)
            if not self.db.running(job['id']):
                return 0
            self.db.execute('UPDATE people SET stories=?,checked_at=? WHERE user_id=?',
                            (json.dumps(stories),now(),person['user_id']))
            seen=self.db.viewed_ids(account['user_id'],person['user_id'])
            ids=[s['id'] for s in stories if s['id'] not in seen][:min(200,VIEW_LIMIT_24H-recent)]
            if ids and self.db.running(job['id']):
                # Checkpoint BEFORE the irreversible RPC. An interruption leaves the job paused;
                # it is never automatically replayed after an uncertain network outcome.
                p['uncertain']={'user_id':person['user_id'],'story_ids':ids}
                with self.db.connect() as c:
                    c.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(p),job['id']))
                    for story_id in ids:
                        c.execute('INSERT OR IGNORE INTO view_attempts VALUES(?,?,?,?)',
                                  (account['user_id'],person['user_id'],story_id,now()))
                if self.storage:
                    # The intent MUST be durable before the view RPC is issued.
                    await self.storage.flush()
                try:
                    await self.gateway.register_views(person,ids)
                except FloodWaitError:
                    p.pop('uncertain',None)
                    self.db.update_job(job['id'],payload=p)
                    with self.db.connect() as c:
                        for story_id in ids:
                            c.execute('DELETE FROM view_attempts WHERE account_id=? AND user_id=? AND story_id=?',
                                      (account['user_id'],person['user_id'],story_id))
                    raise
                except BaseException:
                    if self.db.running(job['id']):
                        self.db.update_job(job['id'],status='paused',message='Ответ на просмотр не получен. При продолжении эта история будет пропущена.')
                    raise
                with self.db.connect() as c:
                    for story_id in ids:
                        c.execute('INSERT OR IGNORE INTO views VALUES(?,?,?,?)',
                                  (account['user_id'],person['user_id'],story_id,now()))
                        c.execute('DELETE FROM view_attempts WHERE account_id=? AND user_id=? AND story_id=?',
                                  (account['user_id'],person['user_id'],story_id))
                count=len(ids)
                p.pop('uncertain',None)
        p['position']=position+1
        self.db.update_job(job['id'],payload=p,checked=job['checked']+1,viewed=job['viewed']+count,
                           message='Просмотр выбранных историй.')
        return p['delay']
