"""HTTP tests for the optional dashboard, using disposable Git repositories."""
from contextlib import contextmanager
from pathlib import Path
import socket
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn
import yaml

import gitmirror
from test_flexible import local_backup, published
from test_git_local import local_git_only, mirror


PASSWORD = 'dashboard-test-passphrase'
ASSETS = Path(gitmirror.__file__).resolve().parent / 'web'


@pytest.fixture
def web_setup(tmp_path):
    spec = {
        'source': {'provider': 'github', 'owner': 'tester', 'token_env': 'TEST_WEB_SOURCE'},
        'destinations': [
            {'id': 'mirror', 'provider': 'codeberg', 'owner': 'mirror', 'token_env': 'TEST_WEB_TARGET'},
        ],
        'storage': {'path': str(tmp_path / 'working')},
        'schedule': {'interval_hours': 12},
        'version': 2,
    }
    config = tmp_path / 'config.yml'
    config.write_text(yaml.safe_dump(spec), encoding='utf-8')
    return gitmirror.load_config(config), config


def app_for(setup, **options):
    return gitmirror.create_web_app(*setup, password=PASSWORD, assets_dir=ASSETS, **options)


def login(client):
    response = client.post('/api/login', json={'password': PASSWORD})
    assert response.status_code == 200, response.text
    return {'X-CSRF-Token': client.get('/api/session').json()['csrf']}


def wait_for(test, *, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := test():
            return result
        time.sleep(.05)
    pytest.fail('Timed out waiting for the dashboard operation')


def job(client, identifier):
    row = next((item for item in client.get('/api/overview').json()['runs'] if item['id'] == identifier), None)
    return row if row and row['state'] not in ('queued', 'running') else None


def test_authentication_revocation_expiration_and_headers(web_setup):
    app = app_for(web_setup, session_ttl=2)
    with TestClient(app, base_url='https://localhost') as client:
        assert client.get('/').status_code == 200
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/assets/../gitmirror.py').status_code == 404
        for path in ('overview', 'session', 'backups', 'repositories', 'events'):
            assert client.get(f'/api/{path}').status_code == 401
        assert client.post('/api/login', json={'password': 'incorrect'}).status_code == 401
        authenticated = client.post('/api/login', json={'password': PASSWORD})
        cookie = authenticated.headers['set-cookie'].lower()
        assert all(attribute in cookie for attribute in ('secure', 'httponly', 'samesite=strict'))
        prior = authenticated.cookies.get('gm_session')
        csrf = {'X-CSRF-Token': client.get('/api/session').json()['csrf']}
        response = client.get('/api/overview')
        assert response.status_code == 200
        data = response.json()
        assert data['total_repositories'] is None and data['last_success'] is None and data['next_run'] is None
        assert data['destinations'][0]['id'] == 'mirror'
        assert 'TEST_WEB_TARGET' not in response.text
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['content-security-policy'].startswith("default-src 'none'")
        assert client.post('/api/logout', headers=csrf, json={}).status_code == 200
        assert client.get('/api/overview', headers={'Cookie': f'gm_session={prior}'}).status_code == 401
        login(client)
        wait_for(lambda: client.get('/api/session').status_code == 401, seconds=5)


def test_host_origin_credentials_and_login_budget(web_setup):
    with TestClient(app_for(web_setup), base_url='http://localhost') as client:
        assert client.get('/api/session', headers={'Host': 'example.invalid'}).status_code == 400
        assert client.get('/', headers={'Host': 'localhost.invalid'}).status_code == 400
        assert client.post('/api/login', headers={'Origin': 'https://localhost'}, json={'password': PASSWORD}).status_code == 403
        assert client.post('/api/login', content=b'x' * 17000,
                           headers={'Content-Type': 'application/json'}).status_code == 413
        assert client.post('/api/login', content=b'{"password":"no"}',
                           headers={'Content-Type': 'text/plain'}).status_code == 415
        for _ in range(5):
            assert client.post('/api/login', json={'password': 'incorrect'}).status_code == 401
        assert client.post('/api/login', json={'password': PASSWORD}).status_code == 429


def test_input_and_actions_authorization(web_setup):
    with TestClient(app_for(web_setup), base_url='http://localhost') as client:
        csrf = login(client)
        assert client.post('/api/sync', headers=csrf, json={'apply': False}).status_code == 403
    with TestClient(app_for(web_setup, allow_actions=True), base_url='http://localhost') as client:
        csrf = login(client)
        assert client.post('/api/sync', json={'apply': False}).status_code == 403
        assert client.post('/api/sync', headers=csrf, json={'apply': 1}).status_code == 400
        assert client.post('/api/sync', headers=csrf, json={'apply': True, 'repo': 'sample'}).status_code == 400
        assert client.post('/api/sync', headers=csrf, json={'apply': False, 'repo': 0}).status_code == 400
        assert client.post('/api/sync', headers=csrf,
                           json={'apply': False, 'destinations': ['unknown']}).status_code == 400
        assert client.post('/api/sync', headers=csrf, json={'apply': False, 'repo': '../secret'}).status_code == 400
        assert client.post('/api/sync', headers={**csrf, 'Origin': 'https://example.invalid'},
                           json={'apply': False}).status_code == 403
        assert client.get('/api/runs/../log').status_code == 404
        web_setup[1].write_text('version: [', encoding='utf-8')
        assert client.post('/api/sync', headers=csrf, json={'apply': False}).status_code == 409
        with pytest.raises(gitmirror.ConfigError):
            gitmirror.load_config(web_setup[1])


def test_real_cli_lock_and_dashboard_concurrency(web_setup):
    settings, _ = web_setup
    with TestClient(app_for(web_setup, allow_actions=True), base_url='http://localhost') as client:
        csrf = login(client)
        arguments = {'apply': True, 'repo': 'example', 'confirmation': 'SYNC', 'destinations': ['mirror']}
        journal = gitmirror.DashboardJournal(settings)
        with gitmirror.FileLock(str(journal.directory / '.action.lock'), timeout=0):
            assert client.post('/api/sync', headers=csrf, json=arguments).status_code == 409
        with gitmirror.FileLock(str(settings['storage']['path'] / '.gitmirror.lock'), timeout=0):
            response = client.post('/api/sync', headers=csrf, json=arguments)
            assert response.status_code == 200
            identifier = response.json()['id']
            result = wait_for(lambda: job(client, identifier))
            assert result['state'] == 'failed' and result['exit_code'] == 2 and result['origin'] == 'web'
            log = client.get(f'/api/runs/{identifier}/log').json()['log']
            assert 'Another GitMirror process is running' in log
        assert not (settings['storage']['path'] / 'work').exists()
    with TestClient(app_for(web_setup), base_url='http://localhost') as client:
        login(client)
        assert job(client, identifier)['state'] == 'failed'


def test_cli_activity_backup_and_safe_http_restore(mirror, local_backup, tmp_path, monkeypatch):
    local, _ = local_backup
    config = Path(mirror.args[1])
    settings = gitmirror.load_config(config)
    output = tmp_path / 'restore-results'
    output.mkdir()
    app = app_for((settings, config), allow_actions=True, restore_dir=output)
    args = [*mirror.args, 'sync']
    assert gitmirror.main([*args, '--dry-run']) == 0
    monkeypatch.setenv('GITMIRROR_TRIGGER', 'cron')
    assert gitmirror.main([*args, '--apply']) == 0
    generation = published(local)
    monkeypatch.delenv('TEST_FLEX_SOURCE', raising=False)
    with TestClient(app, base_url='http://localhost') as client:
        csrf = login(client)
        overview = client.get('/api/overview').json()
        assert overview['last_success'] and overview['total_repositories'] == 1
        assert len(overview['runs']) == 1
        result = overview['runs'][0]
        assert result['origin'] == 'cron'
        assert client.get(f'/api/runs/{result["id"]}/log').json()['results'][0]['state'] == 'succeeded'
        inventory = client.get('/api/repositories').json()['repositories']
        assert inventory[0]['name'] == 'demo' and inventory[0]['last_backup']
        backups = client.get('/api/backups').json()['destinations']
        assert backups[0]['repositories'][0]['generations'][0]['integrity'] == 'verified'
        data = {'repo': '45', 'generation': generation.stem, 'destinations': ['local']}
        response = client.post('/api/backups/verify', headers=csrf, json=data)
        assert response.status_code == 200
        assert wait_for(lambda: job(client, response.json()['id']))['state'] == 'succeeded'
        data.update(output='../unsafe', confirmation='RESTORE')
        assert client.post('/api/backups/restore', headers=csrf, json=data).status_code == 400
        data['output'] = 'restored.git'
        assert client.post('/api/backups/restore', headers=csrf, json={**data, 'confirmation': ''}).status_code == 400
        response = client.post('/api/backups/restore', headers=csrf, json=data)
        assert response.status_code == 200
        assert wait_for(lambda: job(client, response.json()['id']))['state'] == 'succeeded'
        assert gitmirror.local_refs(output / 'restored.git') == gitmirror.local_refs(generation)
        assert client.post('/api/backups/restore', headers=csrf, json=data).status_code == 409
        assert not (tmp_path / 'unsafe').exists()


@contextmanager
def live_http(app):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        address = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', timeout_graceful_shutdown=3))
        worker = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        worker.start()
        try:
            wait_for(lambda: server.started, seconds=6)
            with httpx.Client(base_url=address, timeout=7) as client:
                yield client
        finally:
            server.should_exit = True
            worker.join(8)
            assert not worker.is_alive()


def test_actual_http_event_stream_changes_and_revokes(web_setup, monkeypatch):
    settings, _ = web_setup
    secret = 'test-secret-for-all-logs'
    monkeypatch.setenv('TEST_WEB_SOURCE', secret)
    with live_http(app_for(web_setup)) as client:
        csrf = login(client)
        with client.stream('GET', '/api/events') as response:
            assert response.status_code == 200 and response.headers['content-type'].startswith('text/event-stream')
            events = (line for line in response.iter_lines() if line)
            first = next(events)
            assert first.startswith('data: ')
            journal = gitmirror.DashboardJournal(settings)
            now = time.time()
            journal.write('INSERT INTO schedules VALUES (?,?,?,?)', (journal.scope, 'demo', now + 50, now + 60))

            def observed(activity):
                activity.progress('demo', 1, 1)
                activity.result('demo', 'mirror', 'failed', f'Auth failed for {secret}')
                print(f'password={secret} url=https://who:sign-in@example.invalid/repo')
                return 1

            assert gitmirror.observe_dashboard_operation(settings, 'sync', 'apply', 'scheduled', None,
                                                         [], observed, enabled=True) == 1
            assert next(events) != first
            state = client.get('/api/overview').json()
            assert state['next_run'] == now + 50 and state['failed'] == 1
            assert secret not in str(state)
            identifier = state['runs'][0]['id']
            log = client.get(f'/api/runs/{identifier}/log').json()
            assert '[redacted]' in log['log'] and secret not in str(log) and 'sign-in@' not in log['log']
            assert log['progress'] == {'repository': 'demo', 'index': 1, 'total': 1}
            assert client.post('/api/logout', headers=csrf, json={}).status_code == 200
            assert next(events) == 'event: expired'


def test_web_rejects_short_password_and_unsafe_bind(web_setup):
    settings, config = web_setup
    with pytest.raises(gitmirror.SyncError, match='12 to 1024'):
        gitmirror.create_web_app(settings, config, password='short', assets_dir=ASSETS)
    with pytest.raises(gitmirror.SyncError, match='--allow-lan'):
        gitmirror.serve_web(config, host='0.0.0.0')
    with pytest.raises(gitmirror.SyncError, match='requires TLS'):
        gitmirror.serve_web(config, host='0.0.0.0', allow_lan=True)
