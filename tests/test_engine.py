from gitmirror import parse_refs, count_changes, desired_visibility, SyncError, token
import pytest

def test_parse_refs_ignores_peeled_and_unrelated():
    sha = 'a' * 40
    refs = parse_refs(f'{sha}\trefs/heads/main\n{sha}\trefs/tags/v1\n{sha}\trefs/tags/v1^{{}}\n')
    assert refs == {'refs/heads/main': sha, 'refs/tags/v1': sha}

def test_changes():
    assert count_changes({'a':'1', 'b':'2'}, {'b':'3', 'c':'4'}) == (1, 1, 1)

def test_visibility():
    assert desired_visibility(True) == 'private'
    assert desired_visibility(False) == 'public'

def test_missing_token_env(monkeypatch):
    monkeypatch.delenv('TEST_GITMIRROR_TOKEN', raising=False)
    with pytest.raises(SyncError, match='missing'):
        token({'token_env':'TEST_GITMIRROR_TOKEN'})


def test_protection_journal_recovers_after_interrupted_patch(tmp_path, monkeypatch):
    import gitmirror

    monkeypatch.setattr(gitmirror, 'JOURNAL_DIR', tmp_path / 'journal')
    rule = {'name': 'main', 'allow_force_push': False}
    interrupted = True

    def mock_api(url, headers, method='GET', payload=None):
        if 'protected_branches?' in url:
            return 200, [dict(rule)]
        if 'protected_tags?' in url:
            return 200, []
        assert url.endswith('/protected_branches/main')
        if method == 'PATCH':
            rule.update(payload)
            if interrupted:
                raise KeyboardInterrupt
        return 200, dict(rule)

    monkeypatch.setattr(gitmirror, 'api', mock_api)
    with pytest.raises(KeyboardInterrupt):
        gitmirror.prepare_gl_permissions(3, 'main',
                                        {'refs/heads/main': 'new'}, {'refs/heads/main': 'old'}, {})
    assert rule['allow_force_push'] and gitmirror.load_journal(3)
    interrupted = False
    gitmirror.recover_all_gl_permissions({})
    assert not rule['allow_force_push'] and not gitmirror.load_journal(3)


def test_lfs_retry_rechecks_source_before_starting_another_upload(monkeypatch):
    import subprocess

    import gitmirror

    attempts = []

    def interrupted(args, **kwargs):
        attempts.append(args)
        return subprocess.CompletedProcess(args, 1, b'', b'connection reset')

    def source_changed():
        raise gitmirror.SourceStateError('GitHub visibility changed')

    monkeypatch.setattr(gitmirror, 'RETRY_DELAYS', (0,))
    monkeypatch.setattr(gitmirror.subprocess, 'run', interrupted)
    with pytest.raises(gitmirror.SourceStateError, match='visibility changed'):
        gitmirror.git('lfs', 'push', '--all', 'gitlab', before_retry=source_changed)
    assert len(attempts) == 1


def test_provider_credentials_are_not_redirected_across_origins():
    from urllib.error import HTTPError
    from urllib.request import Request

    from gitmirror import SameOriginRedirect

    request = Request('https://api.example.invalid/v1/user', headers={'PRIVATE-TOKEN': 'synthetic'})
    with pytest.raises(HTTPError):
        SameOriginRedirect().redirect_request(request, None, 302, '', {},
                                              'https://other.example.invalid/v1/user')
