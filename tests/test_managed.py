"""Guided setup tests with synthetic passwords, tokens and SSH keys."""
import base64
import hashlib
import io
import os
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
    settings = {'storage': {'path': work}}
    (etc / 'migration-pending').touch()
    with pytest.raises(gitmirror.SyncError, match='paused'):
        gitmirror.execute_sync(settings, apply=True)
    assert not work.exists()
    (etc / 'migration-pending').unlink()
    (etc / 'legacy').mkdir()
    assert gitmirror.managed_writes_blocked(settings)
    (etc / 'migration-state').write_text('blocked\nLost power while cron was parked\n')
    assert gitmirror.managed_writes_blocked(settings)
    (etc / 'migration-state').write_text('complete\nOriginal cron parked\n')
    assert not gitmirror.managed_writes_blocked(settings)
    (etc / 'migration-pending').touch()
    assert gitmirror.managed_writes_blocked(settings)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux flock')
def test_cutover_flock_honors_live_inode_and_can_lease_a_recreated_file(tmp_path, monkeypatch, capsys):
    import fcntl

    lock = tmp_path / 'legacy'
    lock.touch()
    expected = lock.stat().st_ino
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    with lock.open('rb') as entry:
        fcntl.flock(entry, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(gitmirror.SyncError, match='old job holds'):
            gitmirror.hold_legacy_flock(lock)
    lock.rename(tmp_path / 'previous-lock')
    lock.touch()
    gitmirror.hold_legacy_flock(lock)
    assert int(capsys.readouterr().out.split()[1]) != expected
    pipe = tmp_path / 'named-pipe'
    os.mkfifo(pipe)
    with pytest.raises(gitmirror.SyncError, match='regular file'):
        gitmirror.hold_legacy_flock(pipe)
    link = tmp_path / 'link'
    link.symlink_to(lock)
    with pytest.raises(gitmirror.SyncError, match='without following links'):
        gitmirror.hold_legacy_flock(link)


@pytest.mark.parametrize('arguments', [
    b'/usr/bin/python3\x00-u\x00/home/rpi5/bin/gitmirror.py\x00--apply',
    b'/usr/bin/flock\x00-n\x00/tmp/gitmirror.lock\x00/usr/bin/python3\x00-u\x00/home/rpi5/bin/gitmirror.py',
    b'/bin/sh\x00-c\x00/usr/bin/flock -n /tmp/gitmirror.lock /usr/bin/python3 -u /home/rpi5/bin/gitmirror.py --apply',
    b'python3\x00-u\x00./gitmirror.py\x00--apply',
])
def test_known_legacy_process_blocks_application_without_reading_the_tmp_lock(tmp_path, arguments):
    record = tmp_path / 'target'
    record.write_text('/home/rpi5/bin/gitmirror.py\n')
    proc = tmp_path / 'proc'
    (proc / 'self').mkdir(parents=True)
    mountinfo = proc / 'self/mountinfo'
    mountinfo.write_text('44 24 0:16 / /proc rw,nosuid - proc proc rw\n')
    process = proc / '123456789'
    process.mkdir()
    (process / 'cmdline').write_bytes(arguments)
    with pytest.raises(gitmirror.SyncError, match='process 123456789'):
        gitmirror.assert_legacy_job_inactive(record, proc=proc)
    (process / 'cmdline').write_bytes(b'/opt/gitmirror-managed/current/venv/bin/python3\x00/elsewhere/gitmirror.py')
    gitmirror.assert_legacy_job_inactive(record, proc=proc)
    mountinfo.write_text('44 24 0:16 / /proc rw,nosuid - proc proc rw,hidepid=2\n')
    with pytest.raises(gitmirror.SyncError, match='hides other users'):
        gitmirror.assert_legacy_job_inactive(record, proc=proc)
