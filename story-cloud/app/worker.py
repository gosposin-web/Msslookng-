import asyncio
import json
import logging
import sqlite3

from telethon.errors import FloodWaitError

from .db import now
from .telegram import friendly_error
from .snapshot import SnapshotError
from .account_access import account_error

ACTIVE = ('queued','running','waiting','paused')
TERMINAL = ('done','failed','cancelled')
VIEW_LIMIT_24H = 200


class Worker:
    def __init__(self, db, gateway, storage=None):
        self.db = db
        self.gateway = gateway
        self.storage = storage

    async def run(self):
        recovered_generation=None
        needs_recovery=True
        while True:
            try:
                if self.storage:
                    if not self.storage.ready:
                        await asyncio.sleep(1)
                        continue
                    await self.storage.flush()
                generation=getattr(self.storage,'restore_generation',0)
                if needs_recovery or recovered_generation!=generation:
                    self.db.recover()
                    recovered_generation=generation
                    needs_recovery=False
                job_id = self.db.claim()
                if not job_id:
                    await asyncio.sleep(1)
                    continue
                await self.process(job_id)
            except asyncio.CancelledError:
                raise
            except SnapshotError:
                await asyncio.sleep(3)
            except Exception as exc:
                # A transient database error must not permanently kill the worker.
                logging.getLogger('story_cloud.worker').error('worker_cycle_failed exception=%s',type(exc).__name__)
                needs_recovery=True
                await asyncio.sleep(5)

    def ensure_snapshot(self,job):
        if self.storage and (not getattr(self.storage,'ready',True) or
                job.get('_restore_generation')!=getattr(self.storage,'restore_generation',0)):
            raise SnapshotError('Облачное сохранение восстановлено заново. Задание приостановлено для проверки.')

    def wait_for_telegram(self,job_id,seconds):
        seconds=max(1,int(seconds))
        self.db.execute("INSERT INTO runtime VALUES('telegram_wait_until',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",(now()+seconds,))
        if self.db.running(job_id):
            self.db.update_job(job_id,status='waiting',next_run=now()+seconds,message=f'Telegram попросил подождать {seconds} сек.')
            self.db.event(f'Ограничение Telegram: ожидание {seconds} сек.',job_id,'warning')

    async def process(self, job_id):
        job = self.db.job(job_id)
        if not job or job['status'] != 'running':
            return
        job['_restore_generation']=getattr(self.storage,'restore_generation',0)
        try:
            if self.storage:
                await self.storage.flush()
            async with self.gateway.lock:
                if not self.db.running(job_id):
                    return
                if self.storage:
                    await self.storage.flush()
                self.ensure_snapshot(job)
                async with asyncio.timeout(45):
                    delay = await (self.scan(job) if job['kind']=='scan' else self.view(job))
            self.db.requeue(job_id,delay=delay)
            if self.storage:
                await self.storage.flush()
        except asyncio.CancelledError:
            raise
        except SnapshotError as exc:
            current=self.db.job(job_id)
            if current and current['status'] in ('running','queued'):
                self.db.update_job(job_id,status='paused',message=str(exc))
        except sqlite3.OperationalError:
            if self.db.running(job_id):
                if job['kind']=='view':
                    self.db.update_job(job_id,status='paused',message='Не удалось сохранить результат. Проверьте его перед продолжением.')
                else:
                    retries=job['retries']+1
                    self.db.update_job(job_id,status='waiting' if retries<=3 else 'paused',retries=retries,
                                       next_run=now()+30,message='База данных временно недоступна. Сбор будет повторён.')
        except FloodWaitError as exc:
            # Exact server-requested minimum. Resume cannot clear this deadline.
            self.wait_for_telegram(job_id,exc.seconds)
        except (TimeoutError,ConnectionError,OSError) as exc:
            retries = job['retries']+1
            if self.db.running(job_id):
                self.db.update_job(job_id,status='waiting' if retries<=3 else 'failed',
                                   retries=retries,next_run=now()+min(300,15*2**retries),
                                   message=friendly_error(exc))
        except Exception as exc:
            error=account_error(exc)
            if error.retry_after:
                self.wait_for_telegram(job_id,error.retry_after)
                return
            fatal={'SESSION_REVOKED','SESSION_EXPIRED','SESSION_INVALID','SESSION_DUPLICATED','ACCOUNT_BANNED','ACCOUNT_DELETED','ACCOUNT_FROZEN'}
            if error.code in fatal:
                message=str(error)+' Все задания приостановлены. Проверьте аккаунт во вкладке «Аккаунт».'
                self.db.execute("UPDATE jobs SET status='paused',message=?,updated_at=? WHERE status IN ('queued','running','waiting')",(message,now()))
                self.db.event(message,job_id,'error')
                await self.gateway.close()
                return
            if self.db.running(job_id):
                message = str(error) if error.code=='CLIENT_INTERNAL_ERROR' else friendly_error(exc)
                self.db.update_job(job_id,status='failed',message=message)
                self.db.event(message,job_id,'error')
                logging.getLogger('story_cloud.worker').warning('job_failed kind=%s exception=%s code=%s',job['kind'],type(exc).__name__,error.code)

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
            self.ensure_snapshot(job)
            if not self.db.running(job['id']):
                return 0
            count=self.db.save_people(page.people,source['id'],job['id'],source['max_users'])
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
        self.ensure_snapshot(job)
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
            self.ensure_snapshot(job)
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
                    self.ensure_snapshot(job)
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
