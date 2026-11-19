"""Tests for the optional authenticated web dashboard.

These tests do not contact providers or modify hosted repositories.
"""
import io
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import gitmirror


@pytest.fixture
def web_setup(tmp_path):
    settings = {
        'source': {'provider': 'github', 'owner': 'tester',
                   'token_env': 'TEST_WEB_SOURCE', 'api_url': 'https://api.github.com'},
        'destinations': {
            'mirror': {'provider': 'codeberg', 'owner': 'mirror',
                       'token_env': 'TEST_WEB_TARGET', 'api_url': 'https://codeberg.org/api/v1'},
        },
        'storage': {'path': tmp_path / 'working'},
        'schedule': {'interval_hours': 12},
        'version': 2,
    }
    config = tmp_path / 'config.yml'
    config.write_text('version: 2\n', encoding='utf-8')
    assets = Path(gitmirror.__file__).resolve().parent / 'web'
    return settings, config, assets


def login(client):
    response = client.post('/api/login', json={'password': 'long-secret-password'})
    assert response.status_code == 200
    token = client.get('/api/session')
    assert token.status_code == 200
    return token.json()['csrf']


def test_web_authentication_and_security(web_setup):
    settings, config, assets = web_setup
    app = gitmirror.create_web_app(settings, config, password='long-secret-password',
                                   assets_dir=assets)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get('/').status_code == 200
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/assets/../gitmirror.py').status_code == 404
        assert client.get('/api/overview').status_code == 401
        assert client.post('/api/login', json={'password': 'wrong'}).status_code == 401
        csrf = login(client)
        response = client.get('/api/overview')
        assert response.status_code == 200
        body = response.json()
        assert body['source']['provider'] == 'github'
        assert body['destinations'][0]['id'] == 'mirror'
        assert 'TEST_WEB_TARGET' not in str(body)
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['content-security-policy'].startswith("default-src 'none'")
        assert client.post('/api/sync', json={'apply': False}).status_code == 403
        assert client.post('/api/logout', headers={'X-CSRF-Token': csrf},
                           json={}).status_code == 200
        assert client.get('/api/overview').status_code == 401


def test_bad_host_rejected_and_login_rate_limited(web_setup):
    settings, config, assets = web_setup
    app = gitmirror.create_web_app(settings, config, password='long-secret-password',
                                   assets_dir=assets)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get('/api/session', headers={'host': 'attacker.example'}).status_code == 400
        for _ in range(5):
            assert client.post('/api/login', json={'password': 'incorrect'}).status_code == 401
        assert client.post('/api/login', json={'password': 'long-secret-password'}).status_code == 429


def test_web_write_requests_require_csrf_confirmation_and_permission(web_setup, monkeypatch):
    settings, config, assets = web_setup
    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            self.command = cmd
            self.stdout = io.StringIO('[1/1] example\nSUMMARY: successful=1 failed=0\n')
        def wait(self):
            return 0
    calls = []
    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        return FakeProcess(cmd, **kwargs)
    monkeypatch.setattr(gitmirror.subprocess, 'Popen', fake_popen)
    app = gitmirror.create_web_app(settings, config, password='long-secret-password',
                                   allow_actions=True, assets_dir=assets)
    with TestClient(app, base_url="http://localhost") as client:
        csrf = login(client)
        assert client.post('/api/sync', json={'apply': False}).status_code == 403
        wrong_origin = client.post('/api/sync',
                                   headers={'X-CSRF-Token': csrf, 'Origin': 'https://evil.example'},
                                   json={'apply': False})
        assert wrong_origin.status_code == 403
        missing_confirm = client.post('/api/sync', headers={'X-CSRF-Token': csrf},
                                      json={'repo': 'example', 'apply': True})
        assert missing_confirm.status_code == 400
        invalid_target = client.post('/api/sync', headers={'X-CSRF-Token': csrf},
                                     json={'repo': 'example', 'apply': False, 'destinations': ['unknown']})
        assert invalid_target.status_code == 400
        response = client.post('/api/sync', headers={'X-CSRF-Token': csrf},
                               json={'repo': 'example', 'apply': True,
                                     'confirmation': 'SYNC', 'destinations': ['mirror']})
        assert response.status_code == 200, response.text
        run_id = response.json()['id']
        for _ in range(60):
            rows = client.get('/api/overview').json()['runs']
            if rows and rows[0]['state'] != 'running':
                break
            time.sleep(0.02)
        assert rows[0]['state'] == 'succeeded'
        assert calls
        assert '--apply' in calls[0] and 'example' in calls[0] and 'mirror' in calls[0]
        log = client.get(f'/api/runs/{run_id}/log').json()['log']
        assert 'SUMMARY' in log
        assert client.get('/api/runs/bad/log').status_code == 404


def test_web_rejects_short_password(web_setup):
    settings, config, assets = web_setup
    with pytest.raises(gitmirror.SyncError, match='12 characters'):
        gitmirror.create_web_app(settings, config, password='short', assets_dir=assets)
