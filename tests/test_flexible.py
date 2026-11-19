import errno
from itertools import permutations
import json
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml

import gitmirror
from test_git_local import add_lfs_file, cmd, local_git_only, mirror


PROVIDERS = tuple(gitmirror.PROVIDER_APIS)
PAIRS = list(permutations(PROVIDERS, 2))


def configured(mirror, monkeypatch, source, targets, *, private=False, missing=False):
    config = Path(mirror.args[1])
    local = config.parent / 'Backup Drive'
    local.mkdir()
    destinations, hosts, counters = [], {}, {'source_reads': 0}
    source_spec = {'provider': source, 'owner': 'source', 'token_env': 'TEST_FLEX_SOURCE'}
    monkeypatch.setenv(source_spec['token_env'], 'synthetic-source')
    for target in targets:
        if target == 'local':
            destinations.append({'provider': 'local', 'path': str(local)})
            continue
        owner = f'mirror-{target}'
        token = f'TEST_FLEX_{target.upper()}'
        monkeypatch.setenv(token, f'synthetic-{target}')
        destinations.append({'provider': target, 'owner': owner, 'token_env': token})
        dest = config.parent / f'{target}.git'
        cmd('clone', '--bare', mirror.work, dest)
        if missing:
            cmd('update-ref', '-d', 'refs/heads/main', cwd=dest)
        hosts[target] = {
            'owner': owner, 'repo': dest, 'visibility': 'public', 'default': 'main',
            'present': not missing, 'writes': [],
        }
    config.write_text(yaml.safe_dump({'version': 2, 'source': source_spec, 'destinations': destinations,
                                    'storage': {'path': str(mirror.store)}, 'retry': {'delays_seconds': []}}),
                      encoding='utf-8')
    upstream = {
        'owner': 'source', 'repo': mirror.source, 'visibility': 'private' if private else 'public',
        'default': 'main', 'present': True,
    }

    def serialize(platform, entry, identity):
        info = {'id': identity, 'default_branch': entry['default'], 'visibility': entry['visibility'],
                'archived': True, 'fork': True}
        if platform == 'gitlab':
            info.update(name='Demo Project', path_with_namespace=f'{entry["owner"]}/demo',
                        ssh_url_to_repo=entry['repo'].as_uri(), forked_from_project={'id': 99})
        else:
            info.update(name='demo', owner={'login': entry['owner']}, ssh_url=entry['repo'].as_uri(),
                        private=entry['visibility'] != 'public')
        return info

    def mock_api(url, headers, method='GET', payload=None):
        platform, base = next((key, value) for key, value in gitmirror.PROVIDER_APIS.items()
                              if url.startswith(value + '/'))
        path = url[len(base):]
        authenticate = 'PRIVATE-TOKEN' if platform == 'gitlab' else 'Authorization'
        assert 'synthetic-' in headers[authenticate]
        source_path = '/projects/source%2Fdemo' if source == 'gitlab' else '/repos/source/demo'
        lists = {'github': '/user/repos?affiliation=owner', 'gitlab': '/projects?owned=true',
                 'codeberg': '/user/repos?limit=50'}
        if platform == source and path.startswith(lists[source]):
            assert method == 'GET' and headers[authenticate].endswith('synthetic-source')
            if not upstream['present'] or not path.endswith('page=1'):
                return 200, []
            return 200, [serialize(platform, upstream, 45)]
        if platform == source and path == source_path:
            assert method == 'GET' and headers[authenticate].endswith('synthetic-source')
            counters['source_reads'] += 1
            return (200, serialize(platform, upstream, 45)) if upstream['present'] else (404, None)
        entry = hosts[platform]
        identity = 100 + PROVIDERS.index(platform)
        owner = entry['owner']
        endpoint = '/projects/' + quote(f'{owner}/demo', safe='') if platform == 'gitlab' else f'/repos/{owner}/demo'
        if path == '/user':
            assert method == 'GET'
            return 200, {'username' if platform == 'gitlab' else 'login': owner}
        if path.startswith(f'/projects/{identity}/protected_'):
            assert method == 'GET'
            return 200, []
        if method == 'POST':
            assert path == ('/projects' if platform == 'gitlab' else '/user/repos')
            assert payload.get('visibility') == 'private' or payload.get('private') is True
            entry['writes'].append('create private')
            entry.update(present=True, visibility='private')
            return 201, serialize(platform, entry, identity)
        if method in ('PUT', 'PATCH'):
            assert path == (f'/projects/{identity}' if platform == 'gitlab' else endpoint)
            if 'default_branch' in payload:
                branch = payload['default_branch']
                assert f'refs/heads/{branch}' in gitmirror.remote_refs(entry['repo'].as_uri())
                cmd('symbolic-ref', 'HEAD', f'refs/heads/{branch}', cwd=entry['repo'])
                entry['default'] = branch
            else:
                entry['visibility'] = payload.get('visibility', 'private' if payload.get('private') else 'public')
                entry['writes'].append(entry['visibility'])
            return 200, serialize(platform, entry, identity)
        assert path == endpoint and method == 'GET'
        return (200, serialize(platform, entry, identity)) if entry['present'] else (404, None)

    monkeypatch.setattr(gitmirror, 'api', mock_api)
    return local, upstream, hosts, counters


@pytest.mark.parametrize('source,target', PAIRS)
def test_remote_provider_pairs(mirror, monkeypatch, source, target):
    # Test private data, and creation of a new (initially private) GitHub repository.
    private = source == 'gitlab'
    _, _, hosts, counters = configured(mirror, monkeypatch, source, [target], private=private,
                                       missing=target == 'github')
    cmd('tag', 'v2', cwd=mirror.work)
    cmd('push', 'source', '--tags', cwd=mirror.work)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    dest = hosts[target]
    assert gitmirror.remote_refs(dest['repo'].as_uri()) == gitmirror.remote_refs(mirror.source.as_uri())
    assert dest['visibility'] == ('private' if private else 'public')
    if target == 'github':
        assert dest['writes'][0] == 'create private'
    assert counters['source_reads'] >= 3


@pytest.mark.parametrize('source', PROVIDERS)
def test_local_only_for_every_source_and_uses_native_paths(mirror, monkeypatch, source):
    local, _, _, _ = configured(mirror, monkeypatch, source, ['local'])
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 1  # uninitialized volume
    assert not list(local.iterdir())
    assert gitmirror.main([*mirror.args, 'init-backup']) == 0
    assert gitmirror.main([*mirror.args, 'sync', '--dry-run']) == 0
    assert not list(local.glob('*/45/CURRENT'))
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    backup = published(local)
    assert gitmirror.local_refs(backup) == gitmirror.remote_refs(mirror.source.as_uri())
    assert gitmirror.main([*mirror.args, 'verify-backup']) == 0


def published(local):
    [pointer] = local.glob('*/45/CURRENT')
    return pointer.parent / 'generations' / json.loads(pointer.read_text())['generation']


def test_hybrid_destinations_are_independent_of_api_failure(mirror, monkeypatch):
    local, _, hosts, _ = configured(mirror, monkeypatch, 'codeberg', ['local', 'github', 'gitlab'])
    original = gitmirror.api

    def unavailable(url, headers, method='GET', payload=None):
        if url == 'https://api.github.com/user':
            raise gitmirror.SyncError('simulated API outage')
        return original(url, headers, method=method, payload=payload)

    monkeypatch.setattr(gitmirror, 'api', unavailable)
    assert gitmirror.main([*mirror.args, 'init-backup']) == 0
    cmd('tag', 'hybrid', cwd=mirror.work)
    cmd('push', 'source', '--tags', cwd=mirror.work)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 1
    assert 'refs/tags/hybrid' in gitmirror.local_refs(published(local))
    assert 'refs/tags/hybrid' in gitmirror.local_refs(hosts['gitlab']['repo'])
    assert 'refs/tags/hybrid' not in gitmirror.local_refs(hosts['github']['repo'])


def test_same_remote_rejected_before_metadata_write(mirror, monkeypatch):
    _, _, hosts, _ = configured(mirror, monkeypatch, 'gitlab', ['github'], private=True)
    hosts['github']['repo'] = mirror.source
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 1
    assert not hosts['github']['writes']


def test_github_destination_changes_default_before_removing_old_branch(mirror, monkeypatch):
    _, upstream, hosts, _ = configured(mirror, monkeypatch, 'codeberg', ['github'])
    destination = hosts['github']['repo']
    cmd('branch', '-m', 'trunk', cwd=mirror.work)
    cmd('push', 'source', 'trunk', cwd=mirror.work)
    cmd('symbolic-ref', 'HEAD', 'refs/heads/trunk', cwd=mirror.source)
    cmd('push', 'source', ':main', cwd=mirror.work)
    upstream['default'] = 'trunk'
    real_api = gitmirror.api

    def verify_order(url, headers, method='GET', payload=None):
        if method == 'PATCH' and payload == {'default_branch': 'trunk'}:
            assert set(gitmirror.local_refs(destination)) == {'refs/heads/main', 'refs/heads/trunk'}
        return real_api(url, headers, method=method, payload=payload)

    monkeypatch.setattr(gitmirror, 'api', verify_order)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    assert cmd('symbolic-ref', 'HEAD', cwd=destination) == 'refs/heads/trunk'
    assert set(gitmirror.local_refs(destination)) == {'refs/heads/trunk'}


@pytest.fixture
def local_backup(mirror, monkeypatch):
    local, upstream, _, _ = configured(mirror, monkeypatch, 'gitlab', ['local'])
    assert gitmirror.main([*mirror.args, 'init-backup']) == 0
    return local, upstream


def test_history_and_lfs_restore_without_provider_or_working_cache(mirror, local_backup, monkeypatch, tmp_path):
    local, upstream = local_backup
    base = cmd('rev-parse', 'HEAD', cwd=mirror.work)
    content, oid_path = add_lfs_file(mirror.work)
    cmd('branch', 'old-branch', cwd=mirror.work)
    cmd('tag', 'release', cwd=mirror.work)
    cmd('tag', 'old-tag', cwd=mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', 'old-branch', '--tags', cwd=mirror.work)
    before = gitmirror.remote_refs(mirror.source.as_uri())
    args = [*mirror.args, 'sync', '--apply']
    assert gitmirror.main(args) == 0
    previous = published(local)

    cmd('reset', '--hard', base, cwd=mirror.work)
    cmd('branch', '-m', 'trunk', cwd=mirror.work)
    cmd('tag', '-f', 'release', cwd=mirror.work)
    cmd('tag', '-d', 'old-tag', cwd=mirror.work)
    cmd('push', '--no-verify', '--force', 'source', 'trunk', 'refs/tags/release', cwd=mirror.work)
    cmd('symbolic-ref', 'HEAD', 'refs/heads/trunk', cwd=mirror.source)
    cmd('push', '--no-verify', 'source', ':main', ':old-branch', ':refs/tags/old-tag', cwd=mirror.work)
    upstream['default'] = 'trunk'
    expected = gitmirror.remote_refs(mirror.source.as_uri())
    assert gitmirror.main(args) == 0
    current = published(local)
    assert current != previous and previous.is_dir()
    assert gitmirror.local_refs(current) == expected
    assert gitmirror.local_refs(current, f'refs/archive/{previous.stem}') == before
    cmd('gc', '--prune=now', cwd=current)

    # The backup is sufficient with the provider data and working cache both unavailable.
    mirror.source.rename(tmp_path / 'source-offline.git')
    mirror.store.rename(tmp_path / 'work-offline')
    monkeypatch.delenv('TEST_FLEX_SOURCE')
    monkeypatch.setattr(gitmirror, 'api', lambda *args, **kwargs: pytest.fail('offline restore touched an API'))
    assert gitmirror.main([*mirror.args, 'verify-backup', '--only', 'demo']) == 0
    restored = tmp_path / 'restored.git'
    cmd('init', '--bare', restored)
    cmd('remote', 'add', 'backup', current.as_uri(), cwd=restored)
    cmd('fetch', '--no-tags', 'backup', 'refs/heads/*:refs/heads/*', 'refs/tags/*:refs/tags/*',
        'refs/archive/*:refs/archive/*', cwd=restored)
    cmd('symbolic-ref', 'HEAD', 'refs/heads/trunk', cwd=restored)
    cmd('lfs', 'fetch', '--all', 'backup', cwd=restored)
    assert restored.joinpath(*oid_path).read_bytes() == content
    assert gitmirror.local_refs(restored) == expected
    assert gitmirror.local_refs(restored, f'refs/archive/{previous.stem}') == before
    cmd('fsck', '--full', cwd=restored)


@pytest.mark.parametrize('failure', ['interrupt', 'enospc', 'disconnect', 'git'])
def test_failed_local_backup_keeps_previous_generation(mirror, local_backup, monkeypatch, failure):
    local, _ = local_backup
    args = [*mirror.args, 'sync', '--apply']
    assert gitmirror.main(args) == 0
    previous = published(local)
    marker = local / gitmirror.BACKUP_MARKER
    detached_marker = local / 'detached-volume-marker'
    cmd('tag', 'next', cwd=mirror.work)
    cmd('push', 'source', '--tags', cwd=mirror.work)

    def fail_before_publish(repo):
        if failure == 'interrupt':
            raise KeyboardInterrupt
        if failure == 'enospc':
            raise OSError(errno.ENOSPC, 'test volume full')
        if failure == 'disconnect':
            marker.rename(detached_marker)

    original_git = gitmirror.git

    def fail_fetch(*args, **kwargs):
        if '--no-write-fetch-head' in args:
            raise gitmirror.SyncError('simulated Git transfer failure')
        return original_git(*args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == 'git':
            patch.setattr(gitmirror, 'git', fail_fetch)
        else:
            patch.setattr(gitmirror, 'flush_snapshot', fail_before_publish)
        assert gitmirror.main(args) == (130 if failure == 'interrupt' else 1)
    assert published(local) == previous
    if failure == 'disconnect':
        detached_marker.rename(marker)
    assert gitmirror.main([*mirror.args, 'verify-backup']) == 0
    assert gitmirror.main(args) == 0
    assert published(local) != previous and previous.is_dir()


@pytest.mark.parametrize('failure', ['missing_lfs', 'corrupt_lfs', 'git_object', 'refs'])
def test_volatile_or_corrupt_local_backup_is_never_reported_as_verified(mirror, local_backup, monkeypatch, failure):
    local, _ = local_backup
    data, location = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    repo = published(local)
    if failure == 'missing_lfs':
        repo.joinpath(*location).unlink()
    elif failure == 'corrupt_lfs':
        repo.joinpath(*location).write_bytes(b'x' * len(data))
    elif failure == 'refs':
        cmd('update-ref', 'refs/tags/extra', 'refs/heads/main', cwd=repo)
    else:
        root = repo / 'objects'
        candidates = list(root.glob('pack/*.pack')) or list(root.glob('[0-9a-f][0-9a-f]/*'))
        assert candidates
        candidates[0].chmod(0o600)
        candidates[0].write_bytes(b'damaged Git object')
    monkeypatch.setattr(gitmirror, 'api', lambda *args, **kwargs: pytest.fail('verify-backup called a provider'))
    assert gitmirror.main([*mirror.args, 'verify-backup']) == 2


def test_missing_source_preserves_local_backup(mirror, local_backup):
    local, upstream = local_backup
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    previous = published(local)
    upstream['present'] = False
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 2
    assert published(local) == previous
    assert gitmirror.main([*mirror.args, 'verify-backup']) == 0
