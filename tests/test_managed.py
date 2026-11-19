"""Guided setup tests with synthetic passwords, tokens and SSH keys."""
import base64
import hashlib
from pathlib import Path
import sys
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
        assert client.post('/api/login', json={'password': PASSWORD}).status_code == 409
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


def test_migration_blocks_enable_and_web_apply_but_allows_diagnostics(installation, monkeypatch):
    managed, setup = installation
    guard = managed.root / 'migration-pending'
    guard.touch()
    monkeypatch.setenv('GITMIRROR_MIGRATION_GUARD', str(guard))
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        assert client.get('/api/setup').json()['migration_pending'] is True
        enabled = {**setup, 'schedule': {**setup['schedule'], 'enabled': True}}
        assert client.post('/api/setup', json=enabled).status_code == 400
        outside = [{**setup['destinations'][0], 'path': str(managed.root.parent / 'legacy-storage')}]
        assert client.post('/api/setup', json={**setup, 'destinations': outside}).status_code == 400
        assert managed.active() is None
        assert client.post('/api/setup', json=setup).status_code == 200
    with TestClient(managed_app(managed), base_url='http://localhost') as client:
        assert client.post('/api/managed/check', json={}).status_code == 401
        header = login(client)
        assert client.post('/api/managed/check', headers=header, json={'ssh': 1}).status_code == 400
        assert client.post('/api/sync', headers=header, json={'apply': True, 'confirmation': 'SYNC ALL'}).status_code == 409
        settings = gitmirror.load_config(managed.active().config)
        with pytest.raises(gitmirror.SyncError, match='paused'):
            gitmirror.execute_sync(settings, apply=True)


def test_official_managed_cli_checks_recorded_guards_without_service_environment(tmp_path, monkeypatch):
    etc = tmp_path / 'etc'
    work = tmp_path / 'work'
    etc.mkdir()
    monkeypatch.setattr(gitmirror, 'MANAGED_SYSTEM_STATE', work)
    monkeypatch.setattr(gitmirror, 'MANAGED_SYSTEM_ETC', etc)
    monkeypatch.delenv('GITMIRROR_MIGRATION_GUARD', raising=False)
    monkeypatch.delenv('GITMIRROR_MAINTENANCE_GUARD', raising=False)
    monkeypatch.delenv('GITMIRROR_LEGACY_LOCK_RECORD', raising=False)
    settings = {'storage': {'path': work}}
    (etc / 'migration-pending').touch()
    with pytest.raises(gitmirror.SyncError, match='paused'):
        gitmirror.execute_sync(settings, apply=True)
    assert not work.exists()
    (etc / 'migration-pending').unlink()
    (etc / 'legacy').mkdir()
    with pytest.raises(gitmirror.SyncError, match='recorded legacy flock is missing'):
        with gitmirror.legacy_sync_interlock(settings):
            pytest.fail('Missing legacy interlock must block application writes')


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux flock')
def test_legacy_interlock_honors_busy_and_replaced_lock(tmp_path, monkeypatch):
    import fcntl

    lock = tmp_path / 'legacy'
    lock.touch()
    lock_info = lock.stat()
    record = tmp_path / 'legacy-lock'
    record.write_text(f'{lock}\n{lock_info.st_dev} {lock_info.st_ino}\n')
    monkeypatch.setenv('GITMIRROR_LEGACY_LOCK_RECORD', str(record))
    with lock.open('rb') as entry:
        fcntl.flock(entry, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(gitmirror.SyncError, match='busy'):
            with gitmirror.legacy_sync_interlock():
                pytest.fail('The old and new jobs acquired the same lock')
    with gitmirror.legacy_sync_interlock():
        pass
    lock.rename(tmp_path / 'previous-lock')
    lock.touch()
    with pytest.raises(gitmirror.SyncError, match='inode changed'):
        with gitmirror.legacy_sync_interlock():
            pytest.fail('The inode changed without interrupting sync')
