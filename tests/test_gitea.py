import io
import urllib.error
import urllib.request

import pytest
import yaml

import gitmirror
from test_flexible import API_BASES, configured
from test_git_local import cmd, local_git_only, mirror


BASE = API_BASES['gitea']


def repository(*, owner='alice', repo='example', **fields):
    return {'id': 41, 'owner': {'login': owner}, 'name': repo, 'private': True,
            'ssh_url': f'git@ssh.example.invalid:{owner}/{repo}.git', 'default_branch': 'main', **fields}


def provider():
    return gitmirror.Provider('gitea', BASE, 'alice', {'Authorization': 'token synthetic-gitea-token'})


@pytest.mark.parametrize('value', [
    None, 'https://git.example.com', 'http://git.example.com/api/v1', 'https://user:password@git.example.com/api/v1',
    'https://git.example.com:invalid/api/v1', 'https://git.example.com/api/v1?token=secret',
    'https://git.example.com/api/v1#fragment', 'https://git.example.com/api/v1\n',
])
def test_gitea_requires_a_secure_explicit_api_base(value):
    spec = {'provider': 'gitea', 'owner': 'alice', 'token_env': 'TEST_GITEA_TOKEN'}
    if value is not None:
        spec['api_url'] = value
    with pytest.raises(gitmirror.ConfigError, match='api_url') as issue:
        gitmirror.provider_config(spec, 'source')
    assert 'password' not in str(issue.value) and 'secret' not in str(issue.value)


def test_gitea_subpath_config_and_missing_token(monkeypatch):
    spec = gitmirror.provider_config({'provider': 'gitea', 'owner': 'alice', 'token_env': 'TEST_GITEA_TOKEN',
                                    'api_url': 'https://git.example.com:8443/forge/api/v1/'}, 'source')
    assert spec['api_url'] == 'https://git.example.com:8443/forge/api/v1'
    monkeypatch.delenv('TEST_GITEA_TOKEN', raising=False)
    with pytest.raises(gitmirror.SyncError, match='empty or missing'):
        gitmirror.Provider.from_config(spec)
    monkeypatch.setenv('TEST_GITEA_TOKEN', 'synthetic-gitea-token')
    assert gitmirror.Provider.from_config(spec).headers == provider().headers
    assert 'synthetic-gitea-token' not in repr(gitmirror.Provider.from_config(spec))


def test_gitea_account_mismatch_and_pagination_with_short_pages(monkeypatch):
    request_paths = []
    pages = [[repository(archived=True, fork=True, size=8)],
             [repository(repo='open', id=42, private=False, size=0)],
             [repository(owner='other', id=43, size=50)], []]

    def respond(url, headers, method='GET', payload=None):
        request_paths.append(url.removeprefix(BASE))
        assert headers == provider().headers and method == 'GET'
        if url.endswith('/user'):
            return 200, {'login': 'Alice'}
        page = int(url.rsplit('=', 1)[-1])
        return 200, pages[page - 1]

    monkeypatch.setattr(gitmirror, 'api', respond)
    bound = provider().authenticate()
    assert [(row['name'], row['visibility'], row['size_bytes']) for row in bound.discover()] == [
        ('example', 'private', 8192), ('open', 'public', 0)]
    assert request_paths == ['/user', *[f'/user/repos?limit=50&page={index}' for index in range(1, 5)]]
    pages[2] = pages[0]
    with pytest.raises(gitmirror.SyncError, match='duplicate repository IDs'):
        bound.discover()
    monkeypatch.setattr(gitmirror, 'api', lambda *a, **kw: (200, {'login': 'someone-else'}))
    with pytest.raises(gitmirror.SyncError, match='differs'):
        provider().authenticate()


@pytest.mark.parametrize('fields', [
    {'id': '41'}, {'id': True}, {'id': 0}, {'private': 'false'}, {'default_branch': []},
    {'ssh_url': 'https://user:fictional-credential@git.example.com/alice/example.git'},
    {'ssh_url': 'git::ssh://git@git.example.com/alice/example.git'},
    {'ssh_url': 'ssh://git:fictional-credential@git.example.com/alice/example.git'},
    {'ssh_url': 'ssh://git@git.example.com:bad/alice/example.git'},
])
def test_gitea_rejects_invalid_metadata_without_leaking_url(fields):
    with pytest.raises(gitmirror.SyncError) as issue:
        provider().repo_state(repository(**fields))
    assert 'fictional-credential' not in str(issue.value)


@pytest.mark.parametrize('status', [401, 403, 429, 404])
def test_gitea_api_status_error_and_transient_retry(monkeypatch, status):
    calls = []
    monkeypatch.setattr(gitmirror, 'RETRY_DELAYS', (0,))

    def open_url(request, timeout):
        calls.append(request)
        assert request.get_header('Authorization') == provider().headers['Authorization']
        raise urllib.error.HTTPError(request.full_url, status, 'synthetic-gitea-token', {}, io.BytesIO())

    monkeypatch.setattr(gitmirror.API_OPENER, 'open', open_url)
    with pytest.raises(gitmirror.SyncError, match='Cannot validate|HTTP') as issue:
        provider().authenticate()
    assert 'synthetic-gitea-token' not in str(issue.value)
    assert len(calls) == (2 if status == 429 else 1)


def test_gitea_api_timeout_does_not_retry_creation_or_log_secret(monkeypatch):
    calls = []
    monkeypatch.setattr(gitmirror, 'RETRY_DELAYS', (0,))

    def open_url(request, timeout):
        calls.append(request.get_method())
        raise TimeoutError('synthetic-gitea-token')

    monkeypatch.setattr(gitmirror.API_OPENER, 'open', open_url)
    with pytest.raises(gitmirror.SyncError, match='TimeoutError') as issue:
        gitmirror.api(f'{BASE}/user/repos', provider().headers, 'POST', {'private': True})
    assert 'synthetic-gitea-token' not in str(issue.value)
    assert calls == ['POST']


@pytest.mark.parametrize('target', [
    'https://other.example.invalid/api/v1/user', 'http://gitea.example.invalid/api/v1/user',
    'https://gitea.example.invalid:8443/api/v1/user', 'https://secret@gitea.example.invalid/api/v1/user',
])
def test_provider_redirect_never_passes_token_to_another_origin(target):
    request = urllib.request.Request(BASE + '/user', headers=provider().headers)
    with pytest.raises(urllib.error.HTTPError, match='Unsafe API redirect'):
        gitmirror.SameOriginRedirect().redirect_request(request, None, 302, '', {}, target)


@pytest.mark.parametrize('failure', ['created-public', 'missing-readback', 'id-changed', 'wrong-owner', 'conflict'])
def test_gitea_refuses_ambiguous_creation(monkeypatch, failure):
    calls = []

    def request(url, headers, method='GET', payload=None):
        calls.append(method)
        if len(calls) == 1:
            return 404, None
        if method == 'POST':
            assert url == f'{BASE}/user/repos'
            assert payload == {'name': 'example', 'private': True, 'auto_init': False}
            if failure == 'conflict':
                return 409, None
            return 201, repository(private=failure != 'created-public')
        if failure == 'missing-readback':
            return 404, None
        return 200, repository(owner='other' if failure == 'wrong-owner' else 'alice',
                               id=1001 if failure == 'id-changed' else 41)

    monkeypatch.setattr(gitmirror, 'api', request)
    with pytest.raises(gitmirror.SyncError, match='Gitea'):
        provider().read_destination('example', create=True)
    assert calls == (['GET', 'POST'] if failure == 'conflict' else ['GET', 'POST', 'GET'])


def test_gitea_existing_repository_is_verified_without_creation(monkeypatch):
    def request(url, headers, method='GET', payload=None):
        assert url == f'{BASE}/repos/alice/example' and method == 'GET'
        assert headers['Cache-Control'] == 'no-cache'
        return 200, repository(ssh_url='ssh://git@ssh.example.invalid:2222/alice/example.git')

    monkeypatch.setattr(gitmirror, 'api', request)
    assert provider().read_destination('example', create=True).id == 41


def test_gitea_source_recheck_prevents_private_data_push(mirror, monkeypatch):
    _, upstream, hosts, _ = configured(mirror, monkeypatch, 'gitea', ['github'])
    cmd('tag', 'would-leak', cwd=mirror.work)
    cmd('push', 'source', '--tags', cwd=mirror.work)
    original = gitmirror.api

    def switch(url, headers, method='GET', payload=None):
        if url.endswith('/repos/source/demo'):
            upstream['visibility'] = 'private'
        return original(url, headers, method, payload)

    monkeypatch.setattr(gitmirror, 'api', switch)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 1
    assert 'refs/tags/would-leak' not in gitmirror.remote_refs(hosts['github']['repo'].as_uri())
    assert hosts['github']['writes'] == []


def test_gitea_dry_run_never_creates_repository_and_failed_ssh_aborts_apply(mirror, monkeypatch):
    _, upstream, hosts, _ = configured(mirror, monkeypatch, 'gitea', ['github'], missing=True)
    assert gitmirror.main([*mirror.args, 'sync', '--dry-run']) == 0
    assert hosts['github']['writes'] == []
    git = gitmirror.git

    def unreadable(*args, **kwargs):
        if args[0] == 'ls-remote' and upstream['url'] in args:
            raise gitmirror.SyncError('Host key verification failed')
        return git(*args, **kwargs)

    monkeypatch.setattr(gitmirror, 'git', unreadable)
    assert gitmirror.main([*mirror.args, 'sync', '--apply']) == 1
    assert hosts['github']['writes'] == []


def test_existing_yaml_v1_and_v2_keep_their_provider_credential_settings(tmp_path):
    file = tmp_path / 'config.yml'
    for version in (1, 2):
        destination = {'owner': 'old-owner', 'token_file': str(tmp_path / 'old-token')}
        settings = {'version': version, 'storage': {'path': str(tmp_path / 'state')},
                    'source': {'provider': 'github', 'owner': 'alice', 'token_env': 'OLD_PROVIDER_TOKEN'},
                    'destinations': {'codeberg': destination} if version == 1 else
                                    [{'provider': 'codeberg', **destination}]}
        file.write_text(yaml.safe_dump(settings), encoding='utf-8')
        loaded = gitmirror.load_config(file)
        assert loaded['source']['token_env'] == 'OLD_PROVIDER_TOKEN'
        assert loaded['destinations']['codeberg']['token_file'] == destination['token_file']
        assert loaded['storage']['path'] == tmp_path / 'state'
