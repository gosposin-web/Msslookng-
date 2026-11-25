import json
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from app.account_access import AccountAccessError
from app.channels import create_channel
from app.db import now
from app.telegram import TelegramGateway
from test_app import setup, login, account, tick
from test_profiles import panel


def target(db,gateway,uid=11111):
    person=gateway.person(uid)
    db.execute('INSERT INTO people(user_id,name,username,peer,created_at) VALUES(?,?,?,?,?)',
               (person['user_id'],person['name'],person['username'],person['peer'],now()))
    return person


@pytest.mark.asyncio
@pytest.mark.parametrize('premium,expected',[(True,0),(False,1),(None,0)])
async def test_new_view_rule_uses_author_premium_and_preserves_views(setup,premium,expected):
    client,db,gateway,worker,config=setup
    account(db,config)
    target(db,gateway)
    async def status(person): return premium
    gateway.premium_for_person=status
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5,'auto_react':True})
    for _ in range(6):
        await tick(db,worker)
    assert db.job(jid)['status']=='done'
    assert db.job(jid)['viewed']==1
    assert db.job(jid)['reacted']==expected
    assert len(gateway.calls)==1 and len(gateway.reactions)==expected


@pytest.mark.asyncio
async def test_premium_is_refreshed_before_the_reaction(setup):
    _,db,gateway,worker,config=setup
    account(db,config);target(db,gateway)
    values=iter([False,True])
    async def status(person): return next(values)
    gateway.premium_for_person=status
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5,'auto_react':True})
    for _ in range(5): await tick(db,worker)
    assert db.job(jid)['status']=='done' and gateway.reactions==[]
    assert db.one('SELECT status FROM reaction_tasks')['status']=='skipped'


@pytest.mark.asyncio
async def test_old_view_jobs_remain_view_only(setup):
    _,db,gateway,worker,config=setup
    account(db,config);target(db,gateway)
    async def forbidden(person): raise AssertionError('Legacy jobs must not gain new side effects')
    gateway.premium_for_person=forbidden
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5})
    for _ in range(4): await tick(db,worker)
    assert db.job(jid)['status']=='done' and gateway.reactions==[]


@pytest.mark.asyncio
async def test_uncertain_reaction_is_never_resent_after_resume(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config);target(db,gateway)
    async def status(person): return False
    async def fail(person,story_id):
        gateway.reactions.append((person['user_id'],story_id))
        raise TimeoutError('synthetic lost reply')
    gateway.premium_for_person=status;gateway.react_story=fail
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5,'auto_react':True})
    await tick(db,worker);await tick(db,worker)
    assert db.job(jid)['status']=='paused'
    assert db.one('SELECT status FROM reaction_tasks')['status']=='uncertain'
    assert client.post(f'/api/jobs/{jid}/control',json={'action':'resume'}).status_code==200
    for _ in range(4): await tick(db,worker)
    assert len(gateway.reactions)==1 and db.job(jid)['status']=='done'


@pytest.mark.asyncio
async def test_reaction_intent_is_saved_before_rpc_and_flood_wait_is_respected(setup):
    _,db,gateway,worker,config=setup
    account(db,config);target(db,gateway)
    async def status(person): return False
    gateway.premium_for_person=status
    saved=[]
    class Storage:
        ready=True
        restore_generation=0
        async def flush(self):
            row=db.one('SELECT status FROM reaction_tasks')
            if row and row['status']=='sending': saved.append(True)
    worker.storage=Storage()
    async def react(person,story_id):
        assert saved
        raise errors.FloodWaitError(None,capture=60)
    gateway.react_story=react
    jid=db.create_job('view',{'user_ids':['11111'],'position':0,'delay':5,'auto_react':True})
    await tick(db,worker);await tick(db,worker)
    assert db.job(jid)['status']=='waiting'
    assert db.job(jid)['next_run']>=now()+59
    assert db.one('SELECT status FROM reaction_tasks')['status']=='pending'


def test_profile_bio_is_read_and_updated_in_telegram(panel):
    client,app,protocol=panel
    assert client.get('/api/account/profile').status_code==200
    response=client.post('/api/account/profile/about',json={'about':'Описание ZAVOD'})
    assert response.status_code==200
    assert protocol.about=='Описание ZAVOD'
    assert response.json()['account']['about']=='Описание ZAVOD'
    assert protocol.me.first_name=='Старое'
    assert client.post('/api/account/profile/about',json={'about':'a'*71}).status_code==400
    assert client.post('/api/account/profile/about',json={'about':''}).status_code==200


class ChannelGateway:
    def __init__(self,db):
        self.db=db;self.creates=0;self.fail_create=False;self.fail_username=False
    async def check_channel_username(self,name): return True
    async def create_channel(self,title,about):
        self.creates+=1
        if self.fail_create: raise ConnectionError('synthetic lost creation reply')
        return {'id':'70001','title':title,'username':None,'peer':b'synthetic-encrypted-channel'}
    async def channel_username(self,cid,name):
        if self.fail_username: raise AccountAccessError('USERNAME_OCCUPIED','Username уже занят.')
        self.db.execute('UPDATE channels SET username=? WHERE id=?',(name,cid))
    async def pin_channel(self,cid): return self.db.public_account()


@pytest.mark.asyncio
async def test_channel_creation_is_idempotent_and_preserves_partial_success(setup):
    _,db,_,_,config=setup
    account(db,config)
    gateway=ChannelGateway(db);gateway.fail_username=True
    args=(db,gateway,None,'Мой канал','Описание','test_channel',True,'stable-channel-request')
    first=await create_channel(*args)
    assert first['ok'] and first['warning'] and not first['pinned']
    second=await create_channel(*args)
    assert second['replayed'] and gateway.creates==1
    assert db.one('SELECT COUNT(*) n FROM channels')['n']==1
    assert 'peer' not in json.dumps(first)


@pytest.mark.asyncio
async def test_lost_channel_creation_reply_does_not_create_another_channel(setup):
    _,db,_,_,config=setup
    account(db,config)
    gateway=ChannelGateway(db);gateway.fail_create=True
    args=(db,gateway,None,'Мой канал','Описание','test_channel',True,'uncertain-channel-request')
    for _ in range(2):
        with pytest.raises(AccountAccessError) as result:
            await create_channel(*args)
        assert result.value.code=='CHANNEL_CREATE_UNCERTAIN'
    assert gateway.creates==1


@pytest.mark.asyncio
async def test_story_reaction_uses_standard_unpaid_emoji_rpc(setup):
    _,db,fake,_,config=setup
    account(db,config)
    gateway=TelegramGateway(db,config.cipher())
    requests=[]
    class Client:
        async def __call__(self,request): requests.append(request);return True
    async def client(): return Client()
    gateway.client_for_account=client
    await gateway.react_story(fake.person(11111),7)
    request=requests[0]
    assert isinstance(request,functions.stories.SendReactionRequest)
    assert request.reaction.emoticon=='👍' and request.story_id==7
    assert not request.add_to_recent
    assert bytes(request)
