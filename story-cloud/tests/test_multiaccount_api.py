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
    assert state['active_account'] == 2 and state['max_accounts'] == 5
    assert {a['slot'] for a in state['accounts']} == {1,2}
    assert {r['id'] for r in state['sources']} == {second}
    assert client.delete('/api/sources/'+first).status_code == 200
    client.headers['X-Story-Account'] = '1'
    assert {r['id'] for r in client.get('/api/state').json()['sources']} == {first}


@pytest.mark.parametrize('slot',['0','6','-1','hello','2; DROP TABLE account'])
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
