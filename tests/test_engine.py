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
