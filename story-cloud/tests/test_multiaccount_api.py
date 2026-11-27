import pytest
from app.db import now
from test_app import setup, login, account


def connected(db, config, uid):
    db.execute('INSERT INTO account VALUES(1,?,?,?,?,?)',
               (str(uid), 'Account '+str(uid), 'test_account',
                config.cipher().encrypt(b'synthetic-session'), now()))


def test_account_header_scopes_state_sources_and_jobs(setup):
    client, db, gateway, worker, config = setup
    login(client)
    connected(db, config, 90001)
    hub = client.app.state.hub
    connected(hub.databases[2], config, 90002)
    first = client.post('/api/sources', json={'ref':'@group_one'}).json()['id']
    client.headers['X-Story-Account'] = '2'
    second = client.post('/api/sources', json={'ref':'@group_two'}).json()['id']
    state = client.get('/api/state').json()
    assert state['account']['user_id'] == '90002'
    assert state['active_account'] == 2 and state['max_accounts'] == 15 and state['module_version'] == 2
    assert {a['slot'] for a in state['accounts']} == {1,2}
    assert {r['id'] for r in state['sources']} == {second}
    assert client.delete('/api/sources/'+first).status_code == 200
    client.headers['X-Story-Account'] = '1'
    assert {r['id'] for r in client.get('/api/state').json()['sources']} == {first}


@pytest.mark.parametrize('slot',['0','16','-1','hello','2; DROP TABLE account'])
def test_account_selection_is_bounded(setup,slot):
    client,*_ = setup
    login(client)
    assert client.get('/api/state',headers={'X-Story-Account':slot}).status_code == 400


def test_disconnect_removes_only_the_selected_account(setup):
    client,db,gateway,worker,config = setup
    login(client)
    hub = client.app.state.hub
    for slot in range(1,6):
        connected(hub.databases[slot],config,90000+slot)
    client.headers['X-Story-Account'] = '3'
    assert client.delete('/api/account').status_code == 200
    assert hub.databases[3].account() is None
    assert {a['slot'] for a in client.get('/api/accounts').json()['accounts']} == {1,2,4,5}
    assert hub.databases[1].account()['user_id'] == '90001'


def test_conditional_state_requires_login_and_changes_with_state(setup):
    client,db,*_=setup
    assert client.get('/api/state',headers={'If-None-Match':'*'}).status_code==401
    login(client)
    first=client.get('/api/state');tag=first.headers['etag']
    assert first.status_code==200 and tag.startswith('W/"')
    unchanged=client.get('/api/state',headers={'If-None-Match':tag})
    assert unchanged.status_code==304 and unchanged.content==b''
    assert 'private' in unchanged.headers['cache-control'] and 'no-store' in unchanged.headers['cache-control']
    assert 'X-Story-Account' in unchanged.headers['vary']
    assert client.get('/api/state',headers={'If-None-Match':'"other", '+tag[2:]}).status_code==304
    assert client.post('/api/sources',json={'ref':'@a_new_group'}).status_code==200
    changed=client.get('/api/state',headers={'If-None-Match':tag})
    assert changed.status_code==200 and changed.headers['etag']!=tag
    client.post('/api/auth/logout')
    assert client.get('/api/state',headers={'If-None-Match':changed.headers['etag']}).status_code==401


def test_conditional_responses_are_scoped_to_account_and_filter(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    first=client.get('/api/state')
    second=client.get('/api/state',headers={'X-Story-Account':'15','If-None-Match':first.headers['etag']})
    assert second.status_code==200 and second.json()['active_account']==15
    person=gateway.person(11111)
    import json
    with db.connect() as c:
        c.execute('INSERT INTO people(user_id,name,username,peer,stories,created_at) VALUES(?,?,?,?,?,?)',
                  (person['user_id'],person['name'],person['username'],person['peer'],json.dumps([{'id':7,'expires':now()+1000}]),now()))
    everyone=client.get('/api/people?active=false')
    empty=client.get('/api/people?active=false&search=nobody',headers={'If-None-Match':everyone.headers['etag']})
    assert empty.status_code==200 and empty.json()['people']==[]
    other=client.get('/api/people?active=false',headers={'X-Story-Account':'15','If-None-Match':everyone.headers['etag']})
    assert other.status_code==200 and other.json()['people']==[]


def test_large_people_page_uses_bulk_counts_and_does_not_count_attempts_as_views(setup,monkeypatch):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    with db.connect() as c:
        for uid in range(10000,10100):
            person=gateway.person(uid)
            c.execute('INSERT INTO people(user_id,name,username,peer,created_at) VALUES(?,?,?,?,?)',
                      (person['user_id'],person['name'],person['username'],person['peer'],now()))
        c.execute('INSERT INTO views VALUES(?,?,?,?)',('90000','10000',7,now()))
        c.executemany('INSERT INTO view_attempts VALUES(?,?,?,?)',[('90000','10000',7,now()),('90000','10000',8,now()),('99999','10000',9,now())])
    def no_per_person_queries(*args):raise AssertionError('Page statistics must be read in bulk')
    monkeypatch.setattr(db,'viewed_ids',no_per_person_queries)
    monkeypatch.setattr(db,'confirmed_viewed_ids',no_per_person_queries)
    first=client.get('/api/people?active=false')
    rows=first.json()['people'];row=next(p for p in rows if p['user_id']=='10000')
    assert len(rows)==100 and row['viewed_count']==1 and row['unconfirmed_count']==1
    assert 'peer' not in first.text
    assert client.get('/api/people?active=false',headers={'If-None-Match':first.headers['etag']}).status_code==304
    db.execute('INSERT INTO view_attempts VALUES(?,?,?,?)',('90000','10000',9,now()))
    changed=client.get('/api/people?active=false',headers={'If-None-Match':first.headers['etag']})
    assert changed.status_code==200
    assert next(p for p in changed.json()['people'] if p['user_id']=='10000')['unconfirmed_count']==2


def test_queue_pause_names_the_head_job_and_resume_keeps_telegram_deadline(setup):
    client,db,gateway,worker,config=setup
    login(client);account(db,config)
    first=db.create_job('masslook',{'phase':'gather'})
    db.update_job(first,status='paused',message='Проверьте результат просмотра.')
    for _ in range(2):db.create_job('masslook',{'phase':'gather'})
    db.create_job('view',{'user_ids':[]})
    deadline=now()+600
    db.execute("INSERT INTO runtime VALUES('telegram_wait_until',?)",(deadline,))
    paused=client.get('/api/state').json()['queue_pause']
    assert paused=={'id':first,'message':'Проверьте результат просмотра.','waiting_count':2}
    assert client.get('/api/state',headers={'X-Story-Account':'15'}).json()['queue_pause'] is None
    assert client.post('/api/jobs/'+first+'/control',json={'action':'resume'}).status_code==200
    assert client.get('/api/state').json()['queue_pause'] is None
    assert db.one("SELECT value FROM runtime WHERE key='telegram_wait_until'")['value']==deadline
    assert db.claim() is None
