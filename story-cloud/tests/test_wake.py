import asyncio
import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from test_app import FakeGateway


def app_for(tmp_path, origin='https://private-panel.test'):
    return create_app(Config(tmp_path,'a-long-test-password','a-test-encryption-secret-that-is-long-enough',
                             public_origin=origin,cookie_secure=False),gateway_factory=FakeGateway,run_worker=False)


def test_public_startup_document_contains_only_fixed_readiness_and_return_link(tmp_path):
    app=app_for(tmp_path)
    with TestClient(app) as client:
        response=client.get('/wake?return=https://evil.example/steal')
        assert response.status_code==200
        assert 'data-panel-origin="https://private-panel.test"' in response.text
        assert 'evil.example' not in response.text
        assert '/assets/wake.js' in response.text
        assert 'frame-ancestors https://private-panel.test' in response.headers['Content-Security-Policy']
        assert 'X-Frame-Options' not in response.headers
        assert response.headers['Cache-Control']=='no-store'
        assert response.headers['Referrer-Policy']=='no-referrer'
        # Embedding is limited to the readiness document. API auth is unchanged.
        state=client.get('/api/state')
        assert state.status_code==401 and state.headers['X-Frame-Options']=='DENY'
        assert "frame-ancestors 'none'" in state.headers['Content-Security-Policy']
        assert client.get('/').headers['X-Frame-Options']=='DENY'
        assert client.post('/wake').status_code==405
        assert not app.state.db.account() and not app.state.db.all('SELECT * FROM jobs')


def test_startup_document_remains_available_during_checkpoint_restore(tmp_path,monkeypatch):
    class RestoringStorage:
        ready=False
        def __init__(self,*args): pass
        def changed(self): pass
        async def run(self,*args): await asyncio.Event().wait()
        async def close(self): pass
    monkeypatch.setattr('app.main.SnapshotStore',RestoringStorage)
    config=Config(tmp_path,'a-long-test-password','a-test-encryption-secret-that-is-long-enough',
                  cookie_secure=False,public_origin='https://private-panel.test',
                  snapshot_site_url='https://private-panel.test',bridge_secret='test-bridge',snapshot_site_token='test-token')
    app=create_app(config,gateway_factory=FakeGateway,run_worker=False)
    with TestClient(app) as client:
        assert client.get('/wake').status_code==200
        assert client.get('/assets/wake.js').status_code==200
        assert client.get('/health').json()['ready'] is False
        assert client.get('/api/state').status_code==503


@pytest.mark.parametrize('origin',['','http://private-panel.test','https://user:secret@private-panel.test',
    'https://private-panel.test?return=evil','https://private-panel.test/path',
    'https://private-panel.test:1234','https://private-panel.test\".evil'])
def test_startup_never_reflects_an_invalid_return_origin(tmp_path,origin):
    with TestClient(app_for(tmp_path,origin)) as client:
        response=client.get('/wake')
        assert response.status_code==404
        assert response.headers['X-Frame-Options']=='DENY'
