import subprocess
from pathlib import Path
from gitmirror import engine


def cmd(*args, cwd=None):
    return subprocess.run(['git', *map(str,args)], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_rewrite_preserves_previous_destination_ref(tmp_path):
    work = tmp_path / 'work'
    work.mkdir()
    cmd('init', '-b', 'main', work)
    cmd('config', 'user.email', 'gitmirror@example.invalid', cwd=work)
    cmd('config', 'user.name', 'GitMirror Test', cwd=work)
    (work / 'data.txt').write_text('old')
    cmd('add', 'data.txt', cwd=work)
    cmd('commit', '-m', 'old', cwd=work)
    source = tmp_path / 'source.git'
    dest = tmp_path / 'dest.git'
    repo = tmp_path / 'local.git'
    cmd('clone', '--bare', work, source)
    cmd('clone', '--bare', work, dest)
    cmd('clone', '--bare', source, repo)
    old = engine.remote_refs(str(dest))
    assert len(old) == 1
    engine.ROOT = tmp_path
    engine.RUN_ID = 'integration-test'
    engine.backup_destination_refs(repo, 'gitlab', str(dest), old)
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
    assert engine.remote_refs(str(dest)) == engine.remote_refs(str(source))
    assert cmd('-C', repo, 'rev-parse',
               'refs/archive/integration-test/gitlab/heads/main') == saved
