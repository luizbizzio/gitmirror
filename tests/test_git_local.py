import hashlib
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
import yaml

import gitmirror
from gitmirror import main


def cmd(*args, cwd=None):
    return subprocess.run(['git', *map(str, args)], cwd=cwd, check=True,
                          capture_output=True, encoding='utf-8').stdout.strip()


@pytest.fixture(autouse=True)
def local_git_only(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(tmp_path / 'gitconfig'))
    monkeypatch.setenv('GIT_ALLOW_PROTOCOL', 'file')
    for key in ('ROOT', 'GH_OWNER', 'CB_OWNER', 'GH_API', 'GL_API', 'CB_API',
                'JOURNAL_DIR', 'RETRY_DELAYS', 'RUN_ID', 'SSH_ENV'):
        monkeypatch.setattr(gitmirror, key, getattr(gitmirror, key))
    monkeypatch.setattr(gitmirror, 'SSH_ENV', dict(os.environ))


def init_work(path):
    cmd('init', '-b', 'main', path)
    cmd('config', 'user.email', 'gitmirror@example.invalid', cwd=path)
    cmd('config', 'user.name', 'GitMirror Test', cwd=path)
    (path / 'data.txt').write_text('first', encoding='utf-8')
    cmd('add', 'data.txt', cwd=path)
    cmd('commit', '-m', 'Initial commit', cwd=path)
    return path


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    work = init_work(tmp_path / 'work')
    source = tmp_path / 'source.git'
    dest = tmp_path / 'dest.git'
    store = tmp_path / 'store'
    config = tmp_path / 'config.yml'
    cmd('clone', '--bare', work, source)
    cmd('clone', '--bare', work, dest)
    cmd('remote', 'add', 'source', source.as_uri(), cwd=work)
    cmd('remote', 'add', 'dest', dest.as_uri(), cwd=work)
    config.write_text(yaml.safe_dump({
        'version': 1,
        'source': {'provider': 'github', 'owner': 'source', 'token_env': 'TEST_GITHUB_TOKEN'},
        'destinations': {
            'gitlab': {'token_env': 'TEST_GITLAB_TOKEN'},
            'codeberg': {'owner': 'mirror', 'token_env': 'TEST_CODEBERG_TOKEN'},
        },
        'storage': {'path': str(store)},
        'retry': {'delays_seconds': []},
    }), encoding='utf-8')
    monkeypatch.setenv('TEST_GITHUB_TOKEN', 'test-source')
    monkeypatch.setenv('TEST_GITLAB_TOKEN', 'test-destination')
    reads = []

    def mock_api(url, headers, method='GET', payload=None):
        assert method == 'GET'
        reads.append(url)
        if url.startswith('https://api.github.com/user/repos?'):
            return 200, [{'name': 'demo', 'owner': {'login': 'source'},
                          'private': False, 'ssh_url': source.as_uri()}]
        if url == 'https://gitlab.com/api/v4/user':
            return 200, {'username': 'mirror'}
        if url == 'https://gitlab.com/api/v4/projects/mirror%2Fdemo':
            return 200, {'id': 123, 'ssh_url_to_repo': dest.as_uri(),
                         'visibility': 'public', 'default_branch': 'main'}
        if url.startswith('https://gitlab.com/api/v4/projects/123/protected_'):
            return 200, []
        pytest.fail(f'Unexpected provider request {url}')

    monkeypatch.setattr(gitmirror, 'api', mock_api)
    return SimpleNamespace(work=work, source=source, dest=dest, store=store,
                           repo=store / 'demo.git', reads=reads,
                           args=['--config', str(config)])


def add_lfs_file(work, payload=b'Local GitMirror LFS test'):
    cmd('lfs', 'install', '--local', cwd=work)
    cmd('lfs', 'track', '*.bin', cwd=work)
    (work / 'data.bin').write_bytes(payload)
    cmd('add', '.gitattributes', 'data.bin', cwd=work)
    cmd('commit', '-m', 'Add binary to LFS', cwd=work)
    oid = hashlib.sha256(payload).hexdigest()
    return payload, ('lfs', 'objects', oid[:2], oid[2:4], oid)


def test_rewrite_preserves_previous_destination_ref(tmp_path):
    work = init_work(tmp_path / 'work')
    source = tmp_path / 'source.git'
    dest = tmp_path / 'dest.git'
    repo = tmp_path / 'local.git'
    cmd('clone', '--bare', work, source)
    cmd('clone', '--bare', work, dest)
    cmd('clone', '--bare', source, repo)
    old = gitmirror.remote_refs(str(dest))
    assert len(old) == 1
    gitmirror.ROOT = tmp_path
    gitmirror.RUN_ID = 'integration-test'
    gitmirror.backup_destination_refs(repo, 'gitlab', str(dest), old)
    saved = cmd('-C', repo, 'rev-parse',
                'refs/archive/integration-test/gitlab/heads/main')
    assert saved == old['refs/heads/main']
    (work / 'data.txt').write_text('new')
    cmd('add', 'data.txt', cwd=work)
    cmd('commit', '--amend', '-m', 'rewritten', cwd=work)
    cmd('push', '--force', source, 'main', cwd=work)
    cmd('-C', repo, 'fetch', '--no-tags', '--refmap=', str(source),
        '+refs/heads/*:refs/heads/*')
    cmd('-C', repo, 'push', '--force', '--prune', str(dest),
        '+refs/heads/*:refs/heads/*')
    assert gitmirror.remote_refs(str(dest)) == gitmirror.remote_refs(str(source))
    assert cmd('-C', repo, 'rev-parse',
               'refs/archive/integration-test/gitlab/heads/main') == saved


@pytest.mark.parametrize('command, flags', [('sync', []), ('sync', ['--dry-run']), ('run', ['--dry-run'])])
def test_dry_run_does_not_create_storage_or_modify_repos(mirror, command, flags, monkeypatch):
    def stop_after_first_cycle(delay):
        assert delay == 12 * 3600
        raise KeyboardInterrupt

    monkeypatch.setattr('gitmirror.time.sleep', stop_after_first_cycle)
    monkeypatch.setenv('GIT_SSH_COMMAND', 'ssh -o StrictHostKeyChecking=no')
    cmd('update-ref', 'refs/heads/destination-only', 'refs/heads/main', cwd=mirror.dest)
    source_before = gitmirror.remote_refs(mirror.source.as_uri())
    dest_before = gitmirror.remote_refs(mirror.dest.as_uri())
    assert main([*mirror.args, command, '--platform', 'gitlab', *flags]) == (130 if command == 'run' else 0)
    assert mirror.reads
    assert any('protected_branches' in url for url in mirror.reads)
    assert 'StrictHostKeyChecking=yes' in gitmirror.SSH_ENV['GIT_SSH_COMMAND']
    assert not mirror.store.exists()
    assert gitmirror.remote_refs(mirror.source.as_uri()) == source_before
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == dest_before


@pytest.mark.parametrize('difference', ['name', 'hash', 'extra'])
def test_backup_rejects_any_ref_difference(mirror, difference, monkeypatch, capsys):
    observed = gitmirror.remote_refs(mirror.dest.as_uri())
    if difference == 'name':
        observed['refs/heads/replaced'] = observed.pop('refs/heads/main')
    elif difference == 'hash':
        observed['refs/heads/main'] = '0' * len(observed['refs/heads/main'])
    else:
        cmd('update-ref', 'refs/heads/unexpected', 'refs/heads/main', cwd=mirror.dest)
    cmd('init', '--bare', mirror.repo)
    monkeypatch.setattr(gitmirror, 'RUN_ID', 'verification-test')
    with pytest.raises(gitmirror.SyncError, match='Destination backup ref mismatch'):
        gitmirror.backup_destination_refs(mirror.repo, 'gitlab', mirror.dest.as_uri(), observed)
    assert 'Backup verified' not in capsys.readouterr().out


def test_rewrite_delete_and_restore_git_and_destination_only_lfs(mirror):
    base = cmd('rev-parse', 'HEAD', cwd=mirror.work)
    old_payload, old_object = add_lfs_file(mirror.work)
    cmd('branch', 'old-branch', cwd=mirror.work)
    cmd('tag', '-a', 'release', '-m', 'Original tag', cwd=mirror.work)
    cmd('tag', 'old-tag', cwd=mirror.work)
    cmd('lfs', 'push', '--all', 'dest', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'dest', 'main', 'old-branch', '--tags', cwd=mirror.work)
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    cmd('reset', '--hard', base, cwd=mirror.work)
    cmd('branch', '-D', 'old-branch', cwd=mirror.work)
    cmd('tag', '-d', 'old-tag', cwd=mirror.work)
    cmd('tag', '-fa', 'release', '-m', 'Replacement tag', cwd=mirror.work)
    cmd('push', '--no-verify', '--force', 'source', 'main', '--tags', cwd=mirror.work)

    assert main([*mirror.args, 'sync', '--platform', 'gitlab', '--apply']) == 0
    actual = gitmirror.remote_refs(mirror.dest.as_uri())
    assert actual == gitmirror.remote_refs(mirror.source.as_uri())
    assert actual == {
        'refs/heads/main': base,
        'refs/tags/release': cmd('rev-parse', 'refs/tags/release', cwd=mirror.work),
    }
    dest_refs = cmd('for-each-ref', '--format=%(refname)', cwd=mirror.dest).splitlines()
    assert not any(ref.startswith('refs/archive/') for ref in dest_refs)

    restored = mirror.store / 'restored.git'
    prefix = f'refs/archive/{gitmirror.RUN_ID}/gitlab'
    cmd('init', '--bare', restored)
    cmd('remote', 'add', 'archive', mirror.repo.as_uri(), cwd=restored)
    cmd('fetch', '--no-tags', 'archive', f'{prefix}/heads/*:refs/heads/*',
        f'{prefix}/tags/*:refs/tags/*', cwd=restored)
    cmd('lfs', 'fetch', '--all', 'archive', cwd=restored)
    assert gitmirror.remote_refs(restored.as_uri()) == previous
    assert restored.joinpath(*old_object).read_bytes() == old_payload
    cmd('fsck', '--full', cwd=restored)


def test_push_lease_preserves_a_concurrent_update(mirror):
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    (mirror.work / 'data.txt').write_text('concurrent update', encoding='utf-8')
    cmd('commit', '-am', 'Concurrent update', cwd=mirror.work)
    cmd('push', 'dest', 'main', cwd=mirror.work)
    cmd('update-ref', 'refs/heads/new-at-destination', 'refs/heads/main', cwd=mirror.dest)
    concurrent = gitmirror.remote_refs(mirror.dest.as_uri())
    cmd('reset', '--hard', previous['refs/heads/main'], cwd=mirror.work)
    (mirror.work / 'data.txt').write_text('source update', encoding='utf-8')
    cmd('commit', '-am', 'Source update', cwd=mirror.work)
    desired = {'refs/heads/main': cmd('rev-parse', 'HEAD', cwd=mirror.work)}
    with pytest.raises(gitmirror.SyncError, match='stale info'):
        gitmirror.push_snapshot(mirror.work, 'dest', desired, previous)
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == concurrent


@pytest.mark.parametrize('upload', [True, False], ids=['repair', 'silent-upload-failure'])
def test_matching_refs_still_require_lfs(mirror, monkeypatch, capsys, upload):
    payload, location = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    for remote in ('source', 'dest'):
        cmd('push', '--no-verify', remote, 'main', cwd=mirror.work)
    assert not mirror.dest.joinpath(*location).exists()
    if not upload:
        actual_git = gitmirror.git

        def skip_lfs_upload(*args, **kwargs):
            if 'lfs' in args and args[args.index('lfs') + 1] == 'push':
                return None
            return actual_git(*args, **kwargs)

        monkeypatch.setattr(gitmirror, 'git', skip_lfs_upload)
    assert main([*mirror.args, 'sync', '--platform', 'gitlab', '--apply']) == (0 if upload else 1)
    output = capsys.readouterr().out
    assert mirror.repo.joinpath(*location).read_bytes() == payload
    assert ('SYNCED (' in output) == upload
    assert mirror.dest.joinpath(*location).exists() == upload
    if upload:
        assert mirror.dest.joinpath(*location).read_bytes() == payload
    else:
        assert 'FAILED' in output


def test_missing_historical_source_lfs_blocks_sync(mirror, capsys):
    _, location = add_lfs_file(mirror.work)
    cmd('rm', 'data.bin', cwd=mirror.work)
    cmd('commit', '-m', 'Delete LFS file', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    assert not mirror.source.joinpath(*location).exists()
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    assert main([*mirror.args, 'sync', '--platform', 'gitlab', '--apply']) == 1
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous
    output = capsys.readouterr().out
    assert location[-1] in output
    assert 'SYNCED (' not in output


def test_missing_destination_only_lfs_blocks_rewrite(mirror, capsys):
    _, location = add_lfs_file(mirror.work)
    cmd('push', '--no-verify', 'dest', 'main', cwd=mirror.work)
    assert not mirror.dest.joinpath(*location).exists()
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    assert main([*mirror.args, 'sync', '--platform', 'gitlab', '--apply']) == 1
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous
    output = capsys.readouterr().out
    assert location[-1] in output
    assert 'Backup verified' not in output


def test_corrupt_lfs_cache_is_rejected_even_when_size_matches(mirror, capsys):
    payload, location = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    cmd('clone', '--bare', mirror.source, mirror.repo)
    cache_file = mirror.repo.joinpath(*location)
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b'x' * len(payload))

    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    assert main([*mirror.args, 'sync', '--platform', 'gitlab', '--apply']) == 1
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous
    assert 'Corrupt Git LFS object' in capsys.readouterr().out


@pytest.mark.parametrize('multiple', [False, True])
def test_server_without_atomic_push(mirror, multiple):
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    (mirror.work / 'data.txt').write_text('new', encoding='utf-8')
    cmd('commit', '-am', 'New tip', cwd=mirror.work)
    head = cmd('rev-parse', 'HEAD', cwd=mirror.work)
    desired = {'refs/heads/main': head}
    if multiple:
        desired['refs/tags/v1'] = head
    cmd('config', 'receive.advertiseAtomic', 'false', cwd=mirror.dest)
    if multiple:
        with pytest.raises(gitmirror.SyncError, match='lacks atomic push'):
            gitmirror.push_snapshot(mirror.work, 'dest', desired, previous)
        assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous
    else:
        gitmirror.push_snapshot(mirror.work, 'dest', desired, previous)
        assert gitmirror.remote_refs(mirror.dest.as_uri()) == desired


def test_atomic_rejection_preserves_all_refs(mirror):
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    (mirror.work / 'data.txt').write_text('new', encoding='utf-8')
    cmd('commit', '-am', 'New tip', cwd=mirror.work)
    head = cmd('rev-parse', 'HEAD', cwd=mirror.work)
    hook = mirror.dest / 'hooks' / 'update'
    hook.write_text('#!/bin/sh\n[ "$1" != "refs/tags/rejected" ]\n', encoding='utf-8', newline='\n')
    hook.chmod(0o755)

    with pytest.raises(gitmirror.SyncError, match='destination refs unchanged'):
        gitmirror.push_snapshot(mirror.work, 'dest', {'refs/heads/main': head, 'refs/tags/rejected': head}, previous)
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous


def test_push_refuses_second_push_url(mirror):
    other = mirror.store / 'unverified.git'
    cmd('init', '--bare', other)
    cmd('remote', 'set-url', '--add', '--push', 'dest', mirror.dest.as_uri(), cwd=mirror.work)
    cmd('remote', 'set-url', '--add', '--push', 'dest', other.as_uri(), cwd=mirror.work)
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    desired = {**previous, 'refs/tags/v1': previous['refs/heads/main']}
    with pytest.raises(gitmirror.SyncError, match='one URL matching'):
        gitmirror.push_snapshot(mirror.work, 'dest', desired, previous)
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous
    assert not gitmirror.remote_refs(other.as_uri())


@pytest.mark.parametrize('returncode', [0, 1])
def test_partial_result_fails_and_later_run_recovers(mirror, monkeypatch, capsys, returncode):
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    (mirror.work / 'data.txt').write_text('updated', encoding='utf-8')
    cmd('commit', '-am', 'Updated tip', cwd=mirror.work)
    cmd('tag', 'v1', cwd=mirror.work)
    cmd('push', 'source', 'main', '--tags', cwd=mirror.work)
    actual_git = gitmirror.git

    def partial(*args, **kwargs):
        if args[:2] == ('push', '--atomic'):
            cmd('push', 'dest', 'main', cwd=mirror.work)
            return subprocess.CompletedProcess(args, returncode, '', 'simulated partial update')
        return actual_git(*args, **kwargs)

    args = [*mirror.args, 'sync', '--platform', 'gitlab', '--apply']
    with monkeypatch.context() as patch:
        patch.setattr(gitmirror, 'git', partial)
        assert main(args) == 1
    output = capsys.readouterr().out
    assert 'inspect the verified backup' in output and 'SYNCED (' not in output
    assert 'refs/tags/v1' not in gitmirror.remote_refs(mirror.dest.as_uri())
    archived = f'refs/archive/{gitmirror.RUN_ID}/gitlab'
    assert gitmirror.local_refs(mirror.repo, archived) == previous
    assert main(args) == 0
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == gitmirror.remote_refs(mirror.source.as_uri())
    assert gitmirror.local_refs(mirror.repo, archived) == previous


def test_interrupt_before_lfs_backup_keeps_archive_unpublished(mirror, monkeypatch):
    _, location = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'dest', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'dest', 'main', cwd=mirror.work)
    previous = gitmirror.remote_refs(mirror.dest.as_uri())
    original = gitmirror.fetch_backup_lfs

    def interrupt(repo, remote, refs, objects):
        if remote == 'gitlab':
            raise KeyboardInterrupt
        return original(repo, remote, refs, objects)

    args = [*mirror.args, 'sync', '--platform', 'gitlab', '--apply']
    with monkeypatch.context() as patch:
        patch.setattr(gitmirror, 'fetch_backup_lfs', interrupt)
        assert main(args) == 130
    interrupted = gitmirror.RUN_ID
    assert not gitmirror.local_refs(mirror.repo, f'refs/archive/{interrupted}/gitlab')
    assert gitmirror.local_refs(mirror.repo, f'refs/gitmirror-staging/{interrupted}/gitlab') == previous
    assert gitmirror.remote_refs(mirror.dest.as_uri()) == previous
    assert main(args) == 0
    assert gitmirror.local_refs(mirror.repo, f'refs/archive/{gitmirror.RUN_ID}/gitlab') == previous
    assert mirror.repo.joinpath(*location).is_file()


def test_lfs_recent_evidence_is_incremental_and_full_mode_detects_damage(mirror, capsys):
    payload, old = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    args = [*mirror.args, 'sync', '--platform', 'gitlab', '--apply']
    assert main(args) == 0
    record = mirror.repo / 'gitmirror-lfs-gitlab.json'
    first = json.loads(record.read_text())['objects'][old[-1]]
    assert 'read back 1 objects' in capsys.readouterr().out
    assert main(args) == 0
    assert 'reused recent read-back evidence for 1 objects' in capsys.readouterr().out

    _, new = add_lfs_file(mirror.work, b'new LFS version')
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    assert main(args) == 0
    objects = json.loads(record.read_text())['objects']
    assert objects[old[-1]] == first and new[-1] in objects
    assert 'read back 1 objects; reused evidence for 1' in capsys.readouterr().out

    # The local LFS transport may hard-link objects between repositories.
    damaged = mirror.dest.joinpath(*old)
    damaged.unlink()
    damaged.write_bytes(b'x' * len(payload))
    assert main([*args, '--verify-lfs']) == 1
    assert 'SYNCED (' not in capsys.readouterr().out
    assert not json.loads(record.read_text())['objects']


def test_lfs_expired_record_cannot_hide_missing_destination_object(mirror, monkeypatch, capsys):
    _, location = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    args = [*mirror.args, 'sync', '--platform', 'gitlab', '--apply']
    assert main(args) == 0
    capsys.readouterr()
    mirror.dest.joinpath(*location).unlink()
    monkeypatch.setattr(gitmirror, 'LFS_VERIFICATION_MAX_AGE', 0)
    assert main(args) == 0
    assert mirror.dest.joinpath(*location).is_file()
    assert 'read back 1 objects' in capsys.readouterr().out


def test_existing_private_destination_is_published_after_ref_verification(mirror, monkeypatch):
    original_api = gitmirror.api
    visibility = 'private'
    rechecked_source = False

    def mock_api(url, headers, method='GET', payload=None):
        nonlocal visibility, rechecked_source
        if url == 'https://api.github.com/repos/source/demo':
            assert method == 'GET'
            rechecked_source = True
            return 200, {'private': False, 'owner': {'login': 'source'}}
        if url.endswith('/projects/mirror%2Fdemo'):
            status, project = original_api(url, headers)
            return status, {**project, 'visibility': visibility}
        if url.endswith('/projects/123'):
            assert method == 'PUT' and payload == {'visibility': 'public'}
            assert rechecked_source
            assert gitmirror.remote_refs(mirror.dest.as_uri()) == gitmirror.remote_refs(mirror.source.as_uri())
            visibility = 'public'
            return 200, {}
        return original_api(url, headers, method, payload)

    monkeypatch.setattr(gitmirror, 'api', mock_api)
    cmd('tag', 'v1', cwd=mirror.work)
    cmd('push', 'source', '--tags', cwd=mirror.work)
    assert main([*mirror.args, 'sync', '--platform', 'gitlab', '--apply']) == 0
    assert visibility == 'public'
