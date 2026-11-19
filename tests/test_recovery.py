import errno
import json
import shutil
from pathlib import Path

import pytest
import yaml

import gitmirror
from test_flexible import configured, local_backup, published
from test_git_local import add_lfs_file, cmd, local_git_only, mirror


def make_generation(mirror, local, tag=None):
    if tag:
        cmd('tag', tag, cwd=mirror.work)
        cmd('push', 'source', '--tags', cwd=mirror.work)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    return published(local)


@pytest.mark.parametrize('include_archive', [False, True])
def test_fallback_generations_restore_without_previous_backup_or_work_cache(
        mirror, local_backup, monkeypatch, tmp_path, include_archive):
    local, upstream = local_backup
    original = cmd('rev-parse', 'HEAD', cwd=mirror.work)
    content, lfs_path = add_lfs_file(mirror.work)
    cmd('lfs', 'push', '--all', 'source', 'main', cwd=mirror.work)
    cmd('push', '--no-verify', 'source', 'main', cwd=mirror.work)
    before = make_generation(mirror, local, 'old')
    before_refs = gitmirror.local_refs(before)
    stray = before / 'lfs' / 'objects' / 'unreferenced'
    stray.write_text('unused')
    calls = []

    def unsupported(source, target):
        calls.append(source)
        return False

    monkeypatch.setattr(gitmirror, 'clone_file', unsupported)
    cmd('reset', '--hard', original, cwd=mirror.work)
    cmd('tag', '-d', 'old', cwd=mirror.work)
    cmd('push', '--no-verify', '--force', 'source', 'main', ':refs/tags/old', cwd=mirror.work)
    after = make_generation(mirror, local)
    assert calls
    assert not (after / 'objects/info/alternates').exists()
    assert not (after / 'lfs/objects/unreferenced').exists()
    assert not after.joinpath(*lfs_path).samefile(before.joinpath(*lfs_path))
    for file in (before / 'objects').rglob('*'):
        duplicate = after / file.relative_to(before)
        if file.is_file() and duplicate.is_file():
            assert not file.samefile(duplicate)
    shutil.rmtree(before, onerror=gitmirror.unlock_backup_file)
    mirror.store.rename(tmp_path / 'working-unavailable')
    mirror.source.rename(tmp_path / 'source-unavailable.git')
    monkeypatch.delenv('TEST_FLEX_SOURCE')
    monkeypatch.setattr(gitmirror, 'api', lambda *a, **k: pytest.fail('offline restore accessed the API'))

    output = tmp_path / 'offline restored.git'
    args = [*mirror.args, 'restore', '--repo', '45', '--output', str(output)]
    if include_archive:
        args.append('--include-archive')
    assert gitmirror.main(args) == 0
    assert gitmirror.local_refs(output) == {'refs/heads/main': original}
    archived = gitmirror.local_refs(output, f'refs/archive/{before.stem}')
    assert archived == (before_refs if include_archive else {})
    if include_archive:
        assert output.joinpath(*lfs_path).read_bytes() == content
    else:
        assert not output.joinpath(*lfs_path).exists()
    assert not (output / 'objects/info/alternates').exists()


def enable_retention(mirror, **policy):
    config = Path(mirror.args[1])
    spec = yaml.safe_load(config.read_text())
    spec['destinations'][0]['retention'] = policy
    config.write_text(yaml.safe_dump(spec), encoding='utf-8')


def test_retention_preview_and_apply_respect_count_and_age(mirror, local_backup, capsys):
    local, _ = local_backup
    first = make_generation(mirror, local)
    second = make_generation(mirror, local, 'second')
    third = make_generation(mirror, local, 'third')
    assert gitmirror.main([*mirror.args, 'cleanup', '--apply']) == 0
    assert first.exists() and second.exists() and third.exists()
    old_manifest = gitmirror.backup_manifest(first)
    old_manifest['created_ns'] -= 10 * 86_400 * 1_000_000_000
    gitmirror.store_json(first / gitmirror.BACKUP_MANIFEST, old_manifest)
    enable_retention(mirror, keep=1, days=5)
    capsys.readouterr()
    assert gitmirror.main([*mirror.args, 'cleanup', '--dry-run']) == 0
    preview = capsys.readouterr().out
    assert 'Would remove generation' in preview and first.name in preview and second.name not in preview
    assert 'logical bytes stored' in preview
    assert first.exists() and second.exists() and third.exists()
    assert gitmirror.main([*mirror.args, 'cleanup', '--apply']) == 0
    assert not first.exists() and second.exists() and third == published(local)
    enable_retention(mirror, keep=2)
    assert gitmirror.main([*mirror.args, 'cleanup', '--apply']) == 0
    assert second.exists() and third == published(local)
    assert gitmirror.main([*mirror.args, 'verify-backup']) == 0


@pytest.mark.parametrize('corruption', ['current', 'history', 'unlisted'])
def test_cleanup_preserves_generation_without_verified_replacement(mirror, local_backup, corruption):
    local, _ = local_backup
    first = make_generation(mirror, local)
    latest = make_generation(mirror, local, 'latest')
    manifest = gitmirror.backup_manifest(latest)
    archived = f'refs/archive/{first.stem}/heads/main'
    if corruption == 'unlisted':
        cmd('update-ref', 'refs/heads/other', 'HEAD', cwd=first)
    else:
        cmd('update-ref', '-d', archived, cwd=latest)
        if corruption == 'history':
            manifest['refs'].pop(archived)
            gitmirror.store_json(latest / gitmirror.BACKUP_MANIFEST, manifest)
    enable_retention(mirror, keep=1)
    assert gitmirror.main([*mirror.args, 'cleanup', '--apply']) == (2 if corruption == 'current' else 0)
    assert first.exists() and latest == published(local)


def test_automatic_retention_runs_only_when_explicitly_enabled(mirror, local_backup):
    local, _ = local_backup
    first = make_generation(mirror, local)
    enable_retention(mirror, keep=1, automatic=True)
    second = make_generation(mirror, local, 'automatic')
    assert not first.exists() and second.exists()
    assert gitmirror.main([*mirror.args, 'verify-backup']) == 0


def test_older_generation_restores_even_with_damaged_current_and_refuses_overwrite(
        mirror, local_backup, monkeypatch, tmp_path, capsys):
    local, _ = local_backup
    first = make_generation(mirror, local, 'before')
    expected = gitmirror.local_refs(first)
    latest = make_generation(mirror, local, 'after')
    (latest.parent.parent / 'CURRENT').write_text('damaged pointer')
    monkeypatch.delenv('TEST_FLEX_SOURCE')
    monkeypatch.setattr(gitmirror, 'api', lambda *a, **k: pytest.fail('offline restore accessed the API'))
    assert gitmirror.main([*mirror.args, 'restore', '--list']) == 0
    listing = capsys.readouterr().out
    assert first.stem in listing and latest.stem in listing
    target = tmp_path / 'restore old.git'
    args = [*mirror.args, 'restore', '--repo', 'demo', '--generation', first.stem, '--output', str(target)]
    assert gitmirror.main(args) == 0
    assert gitmirror.local_refs(target) == expected
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'preserve').write_text('existing data')
    assert gitmirror.main([*args[:-1], str(existing)]) == 2
    assert (existing / 'preserve').read_text() == 'existing data'


def test_reflink_disk_full_does_not_fall_back_to_copy(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    target = tmp_path / 'target'
    source.write_bytes(b'data')

    def full(*args):
        raise OSError(errno.ENOSPC, 'device full')

    monkeypatch.setattr(gitmirror, 'clone_file', full)
    with pytest.raises(OSError) as exc:
        gitmirror.BackupCopy()(source, target)
    assert exc.value.errno == errno.ENOSPC and not target.exists()


def test_multiple_gitlab_instances_and_drives_have_isolated_failures(mirror, monkeypatch, tmp_path, capsys):
    original = gitmirror.api
    hosts = {}
    destinations = []
    for name, owner in [('lab-east', 'alice'), ('lab-west', 'bea')]:
        base = f'https://{name}.example.invalid/api/v4'
        repo = tmp_path / f'{name}.git'
        cmd('clone', '--bare', mirror.work, repo)
        key = f'TEST_{name.upper().replace("-", "_")}'
        monkeypatch.setenv(key, name)
        dest = {'id': name, 'owner': owner, 'provider': 'gitlab', 'token_env': key, 'api_url': base}
        destinations.append(dest)
        hosts[base] = (name, owner, repo)
        journal = mirror.store / 'gitlab-protection-journal' / gitmirror.scope_id(dest)
        journal.mkdir(parents=True)
        (journal / '777.json').write_text(json.dumps([{'action': 'force', 'name': 'main', 'id': name}]))

    drives = {name: tmp_path / name for name in ('disk-a', 'disk-b')}
    for name, path in drives.items():
        path.mkdir()
        destinations.append({'id': name, 'path': str(path), 'provider': 'local'})
    config = Path(mirror.args[1])
    config.write_text(yaml.safe_dump({
        'version': 2, 'source': {'provider': 'github', 'owner': 'source', 'token_env': 'TEST_GITHUB_TOKEN'},
        'destinations': destinations, 'storage': {'path': str(mirror.store)}, 'retry': {'delays_seconds': []},
    }), encoding='utf-8')

    force = {'lab-east': True, 'lab-west': True}
    recovered = []
    authentication = []

    def mock_api(url, headers, method='GET', payload=None):
        for base, (name, owner, repo) in hosts.items():
            if not url.startswith(base + '/'):
                continue
            assert headers['PRIVATE-TOKEN'] == name
            endpoint = url.removeprefix(base)
            if endpoint == '/user':
                authentication.append(name)
                return 200, {'username': owner}
            if endpoint == f'/projects/{owner}%2Fdemo':
                return 200, {'id': 777, 'ssh_url_to_repo': repo.as_uri(), 'visibility': 'public',
                             'default_branch': 'main'}
            if endpoint == '/projects/777/protected_branches/main':
                if method == 'PATCH':
                    force[name] = payload['allow_force_push']
                    recovered.append(name)
                return 200, {'name': 'main', 'allow_force_push': force[name]}
            if endpoint.startswith('/projects/777/protected_'):
                return 200, []
            pytest.fail(f'unexpected destination API request: {url}')
        return original(url, headers, method=method, payload=payload)

    monkeypatch.setattr(gitmirror, 'api', mock_api)
    assert gitmirror.main([*mirror.args, 'init-backup', '--destination', 'disk-a', '--destination', 'disk-b']) == 0
    assert gitmirror.main([*mirror.args, 'sync', '--dry-run', '--destination', 'lab-west']) == 0
    assert authentication == ['lab-west'] and not recovered
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 0
    assert recovered == ['lab-east', 'lab-west']
    for path in drives.values():
        assert published(path).is_dir()
    drives['disk-a'].rename(tmp_path / 'disk-a-unplugged')
    cmd('tag', 'independent', cwd=mirror.work)
    cmd('push', 'source', '--tags', cwd=mirror.work)
    capsys.readouterr()
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 1
    failure = capsys.readouterr().out
    assert 'disk-a: BLOCKED' in failure and 'successful/checked destinations=3, failed=1' in failure
    assert 'refs/tags/independent' in gitmirror.local_refs(published(drives['disk-b']))
    for _, _, repo in hosts.values():
        assert 'refs/tags/independent' in gitmirror.local_refs(repo)
