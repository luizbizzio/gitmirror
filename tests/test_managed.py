"""Guided setup tests with synthetic passwords, tokens and SSH keys."""
import base64
import hashlib
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import gitmirror
from test_git_local import local_git_only, mirror
from test_flexible import local_backup
from test_web import ASSETS, login, PASSWORD


@pytest.fixture
def installation(tmp_path):
    managed = gitmirror.ManagedInstallation(tmp_path / 'managed')
    managed.initialize()
    host_key = 'localhost ' + ' '.join(managed.public_key().split()[:2])
    return managed, {
        'code': managed.bootstrap.read_text('utf-8').strip(),
        'password': PASSWORD,
        'source': {'provider': 'github', 'owner': 'synthetic', 'token': 'dummy-token-for-setup-test'},
        'destinations': [{'id': 'disk-a', 'provider': 'local'}],
        'repositories': ['demo'],
        'known_hosts': host_key,
        'schedule': {'enabled': False, 'interval_hours': 12},
    }


def managed_app(installation, **options):
    revision = installation.active()
    config = revision.config if revision else None
    settings = gitmirror.load_config(config) if config else installation.placeholder()
    return gitmirror.create_web_app(settings, config, password=None, assets_dir=ASSETS,
                                   allow_actions=True, restore_dir=installation.restores,
                                   managed=installation, revision=revision, **options)


def test_one_time_browser_setup_rejects_cross_origin_and_bad_code(installation):
    managed, config = installation
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        response = client.get('/api/setup')
        assert response.json()['required'] and response.json()['public_key'].startswith('ssh-ed25519')
        assert config['code'] not in response.text
        assert client.get('/api/managed/settings').status_code == 401
        assert client.post('/api/login', json={'password': PASSWORD}).status_code == 401
        assert client.post('/api/setup', json=config, headers={'Origin': 'https://example.invalid'}).status_code == 403
        assert client.post('/api/setup', json={**config, 'code': 'incorrect'}).status_code == 403
        assert managed.active() is None
        assert client.post('/api/setup', json={**config, 'known_hosts': '-----BEGIN OPENSSH PRIVATE KEY-----'}).status_code == 400
        assert managed.active() is None and not list(managed.versions.iterdir())
        assert client.post('/api/setup', json=config).status_code == 200
        assert client.post('/api/setup', json=config).status_code == 409
    assert managed.active() and not managed.bootstrap.exists()
    revision = managed.active()
    assert config['source']['token'] not in revision.config.read_text('utf-8')
    assert PASSWORD not in (revision.path / 'admin.json').read_text()
    assert gitmirror.load_config(revision.config)['repositories'] == ['demo']
    assert gitmirror.backup_volume(managed.volumes / 'disk-a')


def test_managed_edit_is_authorized_validated_and_atomic(installation):
    managed, config = installation
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        assert client.post('/api/setup', json=config).status_code == 200
    original = managed.active()
    config.pop('code')
    config['revision'] = original.identifier
    config['password'] = ''
    config['source'].pop('token')
    path = managed.work / '.gitmirror.lock'
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        csrf = login(client)
        response = client.get('/api/managed/settings')
        assert 'dummy-token-for-setup-test' not in response.text
        assert 'token_file' not in response.text and 'admin.json' not in response.text
        assert client.post('/api/managed/settings', json=config).status_code == 403
        assert client.post('/api/managed/settings', headers=csrf,
                           json={**config, 'revision': 'stale-editor'}).status_code == 409
        assert client.post('/api/managed/settings', headers=csrf,
                           json={**config, 'repositories': ['demo', 'DEMO']}).status_code == 400
        assert client.post('/api/managed/settings', headers=csrf,
                           json={**config, 'password': 'changed-strong-secret', 'current_password': 'wrong'}).status_code == 403
        assert managed.active() == original
        with gitmirror.FileLock(str(path), timeout=0):
            assert client.post('/api/managed/settings', headers=csrf, json=config).status_code == 409
        assert managed.active() == original
        config['schedule']['interval_hours'] = 6
        assert client.post('/api/managed/settings', headers=csrf, json=config).status_code == 200
    updated = managed.active()
    assert updated != original and original.config.exists()
    assert original.identifier in managed.previous.read_text()
    assert gitmirror.load_config(updated.config)['schedule']['interval_hours'] == 6
    assert (updated.path / 'source.token').read_text() == 'dummy-token-for-setup-test'
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        login(client)


def test_configured_repository_filter_preserves_other_repositories(mirror, local_backup, capsys):
    settings = gitmirror.load_config(Path(mirror.args[1]))
    settings['repositories'] = ['DEMO']
    assert gitmirror.execute_sync(settings) == 0
    assert '[1/1] demo' in capsys.readouterr().out
    settings['repositories'] = ['absent']
    with pytest.raises(gitmirror.SyncError, match='None of the configured repository names'):
        gitmirror.execute_sync(settings)


def test_setup_host_key_lookup_requires_bootstrap_and_exact_fingerprint(installation, monkeypatch):
    managed, setup = installation
    encoded = managed.public_key().split()[1]
    verified = base64.b64encode(hashlib.sha256(base64.b64decode(encoded)).digest()).decode().rstrip('=')
    calls = []

    def scan(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(stdout=f'[mirror.example.invalid]:2222 ssh-ed25519 {encoded}\n')

    monkeypatch.setattr(gitmirror.subprocess, 'run', scan)
    arguments = {'host': 'mirror.example.invalid', 'port': 2222, 'fingerprint': f'SHA256:{verified}'}
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        path = '/api/managed/ssh-host'
        assert client.post(path, json=arguments).status_code == 403
        assert not calls
        arguments['code'] = setup['code']
        assert client.post(path, json={**arguments, 'host': '-malformed'}).status_code == 400
        assert client.post(path, json={**arguments, 'fingerprint': 'SHA256:' + 'A' * 43}).status_code == 502
        response = client.post(path, json=arguments)
        assert response.json()['known_host'] == f'[mirror.example.invalid]:2222 ssh-ed25519 {encoded}'
        assert calls[-1][-1] == 'mirror.example.invalid'


def test_browser_setup_can_enable_schedule(installation):
    managed, setup = installation
    setup['schedule']['enabled'] = True
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        assert client.post('/api/setup', json=setup).status_code == 200
    assert gitmirror.load_config(managed.active().config)['schedule']['enabled'] is True


@pytest.mark.parametrize('pause', ['maintenance-pending', 'migration-pending'])
def test_maintenance_guard_blocks_applied_sync_without_service_environment(tmp_path, monkeypatch, pause):
    etc = tmp_path / 'etc'
    work = tmp_path / 'work'
    etc.mkdir()
    monkeypatch.setattr(gitmirror, 'MANAGED_SYSTEM_STATE', work)
    monkeypatch.setattr(gitmirror, 'MANAGED_SYSTEM_ETC', etc)
    monkeypatch.delenv('GITMIRROR_MAINTENANCE_GUARD', raising=False)
    settings = {'storage': {'path': work}}
    guard = etc / pause
    guard.touch()
    assert gitmirror.managed_writes_blocked(settings)
    with pytest.raises(gitmirror.SyncError, match='paused'):
        gitmirror.execute_sync(settings, apply=True)
    assert not work.exists()
    guard.unlink()
    assert not gitmirror.managed_writes_blocked(settings)
    custom_guard = tmp_path / 'manual-maintenance'
    custom_guard.touch()
    monkeypatch.setenv('GITMIRROR_MAINTENANCE_GUARD', str(custom_guard))
    assert gitmirror.managed_writes_blocked(settings)


def test_unconfigured_dashboard_has_preview_without_opening_private_apis(installation):
    managed, setup = installation
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        status = client.get('/api/setup')
        assert status.status_code == 200
        assert status.json()['required'] is True
        html = client.get('/')
        script = client.get('/assets/app.js')
        styles = client.get('/assets/style.css')
        assert html.status_code == 200 and script.status_code == 200 and styles.status_code == 200
        assert 'id="top-setup"' in html.text
        assert 'id="settings-preview"' in html.text
        assert 'id="setup"' in html.text
        assert 'id="source-storage-chart"' in html.text
        assert 'id="backup-storage-chart"' in html.text
        assert 'id="repository-onboarding"' in html.text
        assert 'id="activity-panel"' in html.text
        assert 'form.classList.add("setup-wizard")' in script.text
        assert 'renderStorageChart(' in script.text
        assert 'warmupInventory()' in script.text
        assert 'configuration-jump' in script.text
        assert 'source_size_bytes' not in html.text
        assert '.donut-legend' in styles.text
        assert '.empty-panel' in styles.text
        assert 'id="main-navigation"' in html.text
        assert 'id="nav-toggle"' in html.text
        assert 'id="sidebar-backdrop"' in html.text
        assert 'class="settings-preview settings-grid"' in html.text
        assert 'routeFromUrl()' in script.text
        assert 'configuration-nav' in script.text
        assert '"gitmirror-sidebar"' in script.text
        assert 'font: 15px/1.48 "Source Sans Pro"' in styles.text
        assert '--workspace-width: 1450px' in styles.text
        assert 'enterPreview(data)' in script.text
        assert 'if (previewMode || !csrf) return;' in script.text
        assert 'style="height:' not in script.text
        assert '.sidebar-status' in styles.text
        assert '<meta name="color-scheme" content="dark">' in html.text
        assert 'color-scheme: dark;' in styles.text
        assert '--background: #101820;' in styles.text
        assert setup['code'] not in html.text
        assert setup['source']['token'] not in html.text
        assert client.get('/api/overview').status_code == 401
        assert client.get('/api/managed/settings').status_code == 401


def test_first_login_sets_six_digit_password_before_configuration(installation):
    managed, payload = installation
    with TestClient(managed_app(managed), base_url='https://localhost') as client:
        status = client.get('/api/setup').json()
        assert status['required'] and not status['password_ready']
        assert client.post('/api/login', json={'password': payload['code']}).json()['first_login']
        assert client.get('/api/overview').status_code == 401
        csrf = {'X-CSRF-Token': client.get('/api/session').json()['csrf']}
        assert client.post('/api/setup/password', headers=csrf,
                           json={'password': '12345'}).status_code == 400
        assert client.post('/api/setup/password', headers=csrf,
                           json={'password': '123456'}).status_code == 200
        assert not managed.bootstrap.exists() and managed.pending_admin.exists()
        assert client.get('/api/setup').json()['password_ready']
        assert client.post('/api/setup', json={**payload, 'password': '',
                           'code': ''}).status_code == 403
        response = client.post('/api/setup', headers=csrf,
                               json={**payload, 'password': '', 'code': ''})
        assert response.status_code == 200, response.text
    assert managed.active() and not managed.pending_admin.exists()
    with TestClient(managed_app(managed), base_url='https://localhost') as client:
        assert client.post('/api/login', json={'password': '123456'}).status_code == 200
        assert client.get('/api/overview').status_code == 200


def test_totp_enrollment_login_recovery_and_disable(installation):
    managed, payload = installation
    with TestClient(managed_app(managed), base_url='https://localhost') as client:
        assert client.post('/api/setup', json=payload).status_code == 200
    with TestClient(managed_app(managed), base_url='https://localhost') as client:
        csrf = login(client)
        assert client.get('/api/security').json()['enabled'] is False
        assert client.post('/api/security/totp/start', headers=csrf,
                           json={'password': 'incorrect'}).status_code == 403
        started = client.post('/api/security/totp/start', headers=csrf,
                              json={'password': PASSWORD})
        assert started.status_code == 200, started.text
        secret = started.json()['secret']
        assert len(secret) == 32
        assert secret not in client.get('/api/security').text
        assert client.post('/api/security/totp/confirm', headers=csrf,
                           json={'code': 'not-a-code'}).status_code == 400
        confirmed = client.post('/api/security/totp/confirm', headers=csrf,
                                json={'code': gitmirror.totp_at(secret, int(time.time() // 30))})
        assert confirmed.status_code == 200, confirmed.text
        recovery = confirmed.json()['recovery_codes']
        assert len(recovery) == len(set(recovery)) == 8
        assert recovery[0] not in (managed.root / 'security.json').read_text('utf-8')
        assert client.get('/api/security').json()['enabled']
        assert client.post('/api/logout', headers=csrf, json={}).status_code == 200

        signin = client.post('/api/login', json={'password': PASSWORD})
        assert signin.json()['mfa_required']
        assert client.get('/api/session').status_code == 401
        assert client.post('/api/login/2fa', json={'code': '000000x'}).status_code == 401
        verified = client.post('/api/login/2fa', json={'code': recovery[0]})
        assert verified.status_code == 200, verified.text
        assert client.get('/api/overview').status_code == 200
        csrf = {'X-CSRF-Token': client.get('/api/session').json()['csrf']}
        assert client.post('/api/logout', headers=csrf, json={}).status_code == 200
        assert client.post('/api/login', json={'password': PASSWORD}).json()['mfa_required']
        assert client.post('/api/login/2fa', json={'code': recovery[0]}).status_code == 401
        assert client.post('/api/login/2fa', json={'code': recovery[1]}).status_code == 200
        csrf = {'X-CSRF-Token': client.get('/api/session').json()['csrf']}
        disabled = client.post('/api/security/totp/disable', headers=csrf, json={
            'password': PASSWORD, 'code': recovery[2]})
        assert disabled.status_code == 200, disabled.text
        assert not client.get('/api/security').json()['enabled']
        assert client.post('/api/logout', headers=csrf, json={}).status_code == 200
        assert client.post('/api/login', json={'password': PASSWORD}).json()['authenticated']
