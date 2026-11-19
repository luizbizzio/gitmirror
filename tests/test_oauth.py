"""Exercise the managed OAuth routes with synthetic providers and private state."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest

import gitmirror as gm
from test_managed import installation, managed_app
from test_web import login


pytestmark = pytest.mark.skipif(os.name == 'nt', reason='OAuth vault requires POSIX owner-only files')

ORIGIN = 'https://localhost'
ROUTES = '/api/managed/oauth'
PRIVATE = ('fixture-access-token', 'fixture-refresh-token', 'fixture-code-verifier',
           'fixture-device-code', 'fixture-client-secret', 'fixture-authorization-code')


def app_registration(provider, **changes):
    data = {'provider': provider, 'client_id': f'{provider}-test-client'}
    if provider != 'github':
        data['redirect_uri'] = f'{ORIGIN}{ROUTES}/{provider}/callback'
    if provider == 'gitea':
        data.update(api_url='https://gitea.example.test/api/v1', private_ips=['192.168.56.2'], ssh_port=2222)
    return {**data, **changes}


def registry(root, *registrations):
    path = root / 'oauth-apps.json'
    path.unlink(missing_ok=True)
    gm.private_text(path, json.dumps(registrations))
    return path


def tokens(**changes):
    return {'access_token': PRIVATE[0], 'refresh_token': PRIVATE[1], 'expires_in': 3600,
            'token_type': 'Bearer', **changes}


class MockProvider:
    def __init__(self, app, *, role='source'):
        self.app = app
        self.role = role
        self.calls = []
        self.codes = []
        self.challenge = ''
        self.auth_error = False
        self.install = True
        self.session_tokens = tokens(scope=app.scopes(role))
        self.poll_results = []
        self.identity = 71
        self.version = '1.25.5'

    def __call__(self, url, *, private_ips=frozenset(), method='GET', headers=None, form=None, payload=None):
        app = self.app
        assert private_ips == app.private_ips
        self.calls.append((url, form, headers, method, payload))
        if url == app.token_url:
            if form.get('grant_type') == 'authorization_code':
                self.codes.append(form)
                assert form['redirect_uri'] == app.redirect
                challenge = base64.urlsafe_b64encode(hashlib.sha256(
                    form['code_verifier'].encode()).digest()).decode().rstrip('=')
                if challenge != self.challenge or form['code'] != PRIVATE[5]:
                    return 400, {'error': 'invalid_grant', 'error_description': 'echo ' + PRIVATE[5]}
            if self.poll_results:
                return self.poll_results.pop(0)
            return 200, self.session_tokens
        if url.endswith(('/login/device/code', '/oauth/authorize_device')):
            suffix = '/login/device' if app.provider == 'github' else '/oauth/device'
            return 200, {'device_code': PRIVATE[3], 'user_code': 'ABCD-1234',
                         'verification_uri': app.issuer + suffix, 'expires_in': 300, 'interval': 5}
        if url.endswith('/version'):
            return 200, {'version': self.version}
        if url.endswith('/user'):
            assert headers['Authorization'].startswith('Bearer ')
            if self.auth_error:
                return 401, {'message': PRIVATE[0]}
            return 200, {'id': self.identity, 'login': 'synthetic', 'username': 'synthetic'}
        if url.endswith('/oauth/token/info'):
            return 200, {'scope': [app.scopes(self.role)], 'resource_owner_id': self.identity,
                         'application': {'uid': app.client_id}}
        if '/user/installations?' in url:
            item = {'id': 23, 'account': {'login': 'synthetic'}, 'repository_selection': 'all',
                    'permissions': {'metadata': 'read', 'administration': 'write'}}
            return 200, {'installations': [item] if self.install else []}
        if any(path in url for path in ('/user/installations/23/repositories?', '/user/repos?', '/projects?')):
            if 'page=2' in urlparse(url).query:
                return 200, []
            host, port = app.ssh_endpoint
            repo = {'id': 6, 'name': 'demo', 'owner': {'login': 'synthetic'}, 'private': True,
                    'visibility': 'private', 'default_branch': 'main', 'path_with_namespace': 'synthetic/demo',
                    'ssh_url': f'ssh://git@{host}:{port}/synthetic/demo.git',
                    'ssh_url_to_repo': f'ssh://git@{host}:{port}/synthetic/demo.git'}
            return 200, {'repositories': [repo]} if app.provider == 'github' else [repo]
        if url.endswith('/credentials/revoke'):
            assert method == 'POST' and payload['credentials'] == [PRIVATE[1], PRIVATE[0]]
            return 202, None
        if url.endswith('/oauth/revoke'):
            return 200, {}
        raise AssertionError(f'Unexpected provider endpoint {url}')


def auth_payload(config, app, **changes):
    return {'provider': app.provider, 'api_url': app.base, 'code': config.get('code'), 'role': 'source',
            'full_access_confirmed': True, **changes}


def manager(client):
    return client.app.state.oauth_flows


def start(client, data, method, **kwargs):
    response = client.post(ROUTES + '/start', json={**data, 'method': method}, **kwargs)
    assert response.status_code == 200, response.text
    clean(response)
    return response.json()


def clean(response):
    for value in PRIVATE:
        assert value not in response.text
        assert value not in response.headers.get('location', '')


def browser_accept(client, started, provider, code=PRIVATE[5], **query):
    params = parse_qs(urlparse(started['url']).query)
    response = client.get(f'{ROUTES}/{provider}/callback', params={
        'state': params['state'][0], 'code': code, **query}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['referrer-policy'] == 'no-referrer'
    clean(response)
    return response.headers['location']


def progress(client, flow, operation='status', **kwargs):
    response = client.post(f'{ROUTES}/flow/{flow["flow"]}/{operation}', **kwargs)
    assert response.status_code == 200, response.text
    clean(response)
    return response.json()


@pytest.mark.parametrize('provider,secret', [('gitlab', ''), ('gitlab', PRIVATE[4]),
                                            ('codeberg', ''), ('gitea', ''), ('gitea', PRIVATE[4])])
def test_pkce_setup_one_use_private_storage(installation, monkeypatch, provider, secret):
    managed, setup = installation
    data = app_registration(provider, client_secret=secret)
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    monkeypatch.setattr(gm, 'oauth_http', fake)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        started = start(client, auth_payload(setup, app), 'browser')
        query = parse_qs(urlparse(started['url']).query)
        fake.challenge = query['code_challenge'][0]
        assert query['code_challenge_method'] == ['S256']
        assert query['redirect_uri'] == [app.redirect]
        assert urlparse(started['url']).hostname == urlparse(app.issuer).hostname
        assert browser_accept(client, started, provider) == '/#oauth-connected'
        assert browser_accept(client, started, provider) == '/#oauth-failed'
        assert len(fake.codes) == 1
        assert bool(fake.codes[0].get('client_secret')) == bool(secret)
        connection = progress(client, started)['connection']
        assert connection['account'] == 'synthetic' and connection['status'] == 'connected'
        path = gm.oauth_file(managed.root, connection['id'])
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert fake.codes[0]['code_verifier'] not in path.read_text()
        assert PRIVATE[5] not in path.read_text()
        setup['source'] = {'provider': provider, 'api_url': app.base, 'owner': 'synthetic', 'oauth_id': connection['id']}
        rejected = client.post('/api/setup', json=setup)
        assert rejected.status_code == 400 and 'SSH host key' in rejected.text
        host, port = app.ssh_endpoint
        entry = host if port == 22 else f'[{host}]:{port}'
        setup['known_hosts'] = entry + ' ' + ' '.join(managed.public_key().split()[:2])
        assert client.post('/api/setup', json=setup).status_code == 200
    config = gm.load_config(managed.active().config)
    assert config['source']['oauth_id'] == connection['id']
    assert 'token_file' not in config['source']
    assert PRIVATE[0] not in managed.active().config.read_text()
    assert gm.validate_managed(managed) == 0
    assert gm.Provider.from_config(config['source']).authenticate().discover()[0]['name'] == 'demo'


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_device_poll_honors_interval_slow_down_and_disconnection(installation, monkeypatch, provider):
    managed, setup = installation
    data = app_registration(provider, device=True)
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    fake.poll_results = [(400, {'error': 'authorization_pending'}), (200, {'error': 'slow_down', 'interval': 13})]
    monkeypatch.setattr(gm, 'oauth_http', fake)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        form = auth_payload(setup, app)
        started = start(client, form, 'device')
        assert started['user_code'] == 'ABCD-1234' and started['expires_at']
        assert started['url'].startswith(app.issuer + '/')
        assert len(fake.calls) == 1
        assert progress(client, started)['status'] == 'awaiting' and len(fake.calls) == 1
        flow = manager(client).flows[started['flow']]
        flow.next_poll = 0
        assert progress(client, started)['status'] == 'awaiting' and len(fake.calls) == 2
        assert flow.next_poll > gm.time.monotonic() + 4
        flow.next_poll = 0
        assert progress(client, started)['status'] == 'awaiting' and flow.interval == 13
        assert progress(client, started)['status'] == 'awaiting' and len(fake.calls) == 3
        flow.next_poll = 0
        result = progress(client, started)
        assert result['status'] == 'connected' and not flow.device_code
        record_id = result['connection']['id']
        path = gm.oauth_file(managed.root, record_id)
        assert PRIVATE[3] not in path.read_text()
        response = client.post(f'{ROUTES}/connection/{record_id}/disconnect', json={'code': setup['code']})
        assert response.status_code == 200 and response.json()['connection']['status'] == 'disconnected'
        assert 'revocation requested' in response.json()['message']
        clean(response)
        assert 'access_token' not in path.read_text() and 'refresh_token' not in path.read_text()
        assert not managed.active() and not list(managed.volumes.iterdir())


def test_pkce_brute_force_browser_binding_state_and_origin(installation, monkeypatch):
    managed, setup = installation
    data = app_registration('codeberg')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    monkeypatch.setattr(gm, 'oauth_http', fake)
    form = auth_payload(setup, app, method='browser')
    with TestClient(managed_app(managed, allowed_hosts=['other.test']), base_url=ORIGIN) as client:
        assert client.post(ROUTES + '/start', json=form, headers={'Origin': 'https://bad.test'}).status_code == 403
        assert client.post(ROUTES + '/start', json=form, headers={'Host': 'other.test'}).status_code == 422
        for _ in range(5):
            assert client.post(ROUTES + '/start', json={**form, 'code': 'incorrect'}).status_code == 403
        assert client.post(ROUTES + '/start', json=form).status_code == 429
    with TestClient(managed_app(managed), base_url=ORIGIN) as client, TestClient(
            client.app, base_url=ORIGIN) as other:
        assert client.post(ROUTES + '/start', json={**form, 'full_access_confirmed': False}).status_code == 422
        started = start(client, form, 'browser')
        assert browser_accept(other, started, 'codeberg') == '/#oauth-failed'
        assert browser_accept(client, started, 'gitlab') == '/#oauth-failed'
        assert browser_accept(client, started, 'codeberg', state='incorrect') == '/#oauth-failed'
        assert not fake.calls
        assert browser_accept(client, started, 'codeberg', iss='https://evil.test') == '/#oauth-failed'
        assert 'invalid response' in progress(client, started)['message']
        assert browser_accept(client, started, 'codeberg') == '/#oauth-failed'
        assert not fake.calls


@pytest.mark.parametrize('failure', ['expired', 'cancelled', 'denied', 'mismatch', 'missing_refresh', 'wrong_account'])
def test_browser_failed_approval_never_creates_credentials(installation, monkeypatch, failure):
    managed, setup = installation
    data = app_registration('gitlab')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    monkeypatch.setattr(gm, 'oauth_http', fake)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        payload = auth_payload(setup, app, owner='different' if failure == 'wrong_account' else '')
        started = start(client, payload, 'browser')
        fake.challenge = parse_qs(urlparse(started['url']).query)['code_challenge'][0] if failure != 'mismatch' else 'incorrect'
        flow = manager(client).flows[started['flow']]
        args = {}
        if failure == 'expired':
            flow.end = 0
        if failure == 'cancelled':
            assert progress(client, started, 'cancel')['status'] == 'cancelled'
        if failure == 'denied':
            args['error'] = 'access_denied'
        if failure == 'missing_refresh':
            fake.session_tokens.pop('refresh_token')
        assert browser_accept(client, started, 'gitlab', **args) == '/#oauth-failed'
        assert flow.status == ('cancelled' if failure == 'cancelled' else 'failed')
        assert not flow.verifier and not flow.state
        assert not (managed.root / 'oauth').exists()


def seed_record(managed, app, **changes):
    path = gm.oauth_file(managed.root, 'f' * 32, create=True)
    record = {**gm.oauth_tokens(tokens()), 'status': 'connected', 'provider': app.provider, 'api_url': app.base,
              'account': 'synthetic', 'account_id': 71, 'role': 'source', 'last_checked': gm.time.time(),
              'client_id': app.client_id, **changes}
    gm.store_oauth(path, record)
    return path


def test_cli_and_scheduler_refresh_share_single_rotation(installation, monkeypatch):
    managed, _ = installation
    app = gm.oauth_registration(app_registration('gitea'))
    path = seed_record(managed, app, refresh_at=0, expires_at=1)
    fake = MockProvider(app)
    fake.session_tokens = tokens(access_token='new-fixture-access', refresh_token='new-fixture-refresh')
    monkeypatch.setattr(gm, 'oauth_http', fake)

    def get(refresh):
        return gm.oauth_access(managed.root, path.stem, app, 'source', 'synthetic', refresh=refresh)

    with pytest.raises(gm.OAuthError, match='need renewal'):
        get(False)
    assert not fake.calls and not path.with_suffix('.lock').exists()
    with ThreadPoolExecutor(6) as pool:
        assert list(pool.map(get, [True] * 12)) == ['new-fixture-access'] * 12
    assert len([call for call in fake.calls if call[0] == app.token_url]) == 1
    assert 'new-fixture-refresh' in path.read_text()
    assert PRIVATE[1] not in path.read_text()


@pytest.mark.parametrize('failure', ['revoked', 'expired_refresh', 'no_refresh', 'timeout', 'identity', 'interrupted'])
def test_refresh_fails_closed_and_recovers_from_interruption(installation, monkeypatch, failure):
    managed, _ = installation
    app = gm.oauth_registration(app_registration('gitea'))
    path = seed_record(managed, app, refresh_at=0, expires_at=1)
    original = gm.oauth_record(managed.root, path.stem)
    fake = MockProvider(app)
    fake.session_tokens = tokens(access_token='new-fixture-access', refresh_token='new-fixture-refresh')
    if failure == 'revoked':
        fake.poll_results = [(400, {'error': 'invalid_grant'})]
    if failure == 'expired_refresh':
        gm.store_oauth(path, {**original, 'refresh_expires_at': 9})
    if failure == 'no_refresh':
        gm.store_oauth(path, {**original, 'refresh_token': None})
    if failure == 'identity':
        fake.identity += 1

    def provider(url, **kwargs):
        if (failure == 'timeout' or (failure == 'interrupted' and url.endswith('/user'))):
            raise gm.OAuthNetworkError('Synthetic outage')
        return fake(url, **kwargs)

    monkeypatch.setattr(gm, 'oauth_http', provider)
    with pytest.raises(gm.OAuthError):
        gm.oauth_access(managed.root, path.stem, app, 'source', 'synthetic', refresh=True)
    record = gm.oauth_record(managed.root, path.stem)
    if failure == 'interrupted':
        assert record['identity_pending'] and record['refresh_token'] == 'new-fixture-refresh'
        monkeypatch.setattr(gm, 'oauth_http', fake)
        assert gm.oauth_access(managed.root, path.stem, app, 'source', 'synthetic', refresh=True) == 'new-fixture-access'
        assert len([call for call in fake.calls if call[0] == app.token_url]) == 1
    elif failure == 'timeout':
        assert record == original
    else:
        assert record['status'] == 'reconnect_required' and 'access_token' not in record


def test_settings_preserves_legacy_and_requires_session_csrf(installation, monkeypatch):
    managed, setup = installation
    initial = managed.save(setup, bootstrap=True)
    data = app_registration('codeberg')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    monkeypatch.setattr(gm, 'oauth_http', fake)
    form = auth_payload(setup, app, method='browser')
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        assert client.post(ROUTES + '/start', json=form).status_code == 401
        csrf = login(client)
        assert client.post(ROUTES + '/start', json=form).status_code == 403
        started = start(client, form, 'browser', headers=csrf)
        fake.challenge = parse_qs(urlparse(started['url']).query)['code_challenge'][0]
        assert client.post('/api/logout', headers=csrf).status_code == 200
        assert browser_accept(client, started, 'codeberg') == '/#oauth-failed'
        assert not fake.calls
    assert managed.active().identifier == initial
    assert (managed.active().path / 'source.token').read_text() == setup['source']['token']


def test_reconnect_keeps_account_binding_and_old_credentials_on_failure(installation, monkeypatch):
    managed, setup = installation
    data = app_registration('gitea')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    path = seed_record(managed, app)
    previous = path.read_bytes()
    fake = MockProvider(app)
    fake.session_tokens = tokens(access_token='replacement-access', refresh_token='replacement-refresh')
    monkeypatch.setattr(gm, 'oauth_http', fake)
    def reconnect(client):
        started = start(client, auth_payload(setup, app, connection=path.stem), 'browser')
        fake.challenge = parse_qs(urlparse(started['url']).query)['code_challenge'][0]
        return started
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        started = reconnect(client)
        fake.identity += 1
        assert browser_accept(client, started, 'gitea') == '/#oauth-failed'
        assert path.read_bytes() == previous
        fake.identity -= 1
        started = reconnect(client)
        with gm.FileLock(str(managed.work / '.gitmirror.lock'), timeout=0):
            assert browser_accept(client, started, 'gitea') == '/#oauth-failed'
        assert path.read_bytes() == previous
        started = reconnect(client)
        assert browser_accept(client, started, 'gitea') == '/#oauth-connected'
        assert progress(client, started)['connection']['id'] == path.stem
        assert gm.oauth_record(managed.root, path.stem)['access_token'] == 'replacement-access'


def test_gitlab_destination_requires_write_scope(installation, monkeypatch):
    managed, setup = installation
    data = app_registration('gitlab')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app, role='source')
    monkeypatch.setattr(gm, 'oauth_http', fake)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        started = start(client, auth_payload(setup, app, role='destination'), 'browser')
        query = parse_qs(urlparse(started['url']).query)
        assert query['scope'] == ['api']
        fake.challenge = query['code_challenge'][0]
        assert browser_accept(client, started, 'gitlab') == '/#oauth-failed'
        assert 'scope' in progress(client, started)['message']


def test_github_app_installation_required_and_list_used_for_source(installation, monkeypatch):
    managed, setup = installation
    data = app_registration('github')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    monkeypatch.setattr(gm, 'oauth_http', fake)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        started = start(client, auth_payload(setup, app), 'device')
        fake.install = False
        manager(client).flows[started['flow']].next_poll = 0
        assert 'installation' in progress(client, started)['message']
        assert not (managed.root / 'oauth').exists()
        started = start(client, auth_payload(setup, app), 'device')
        fake.install = True
        manager(client).flows[started['flow']].next_poll = 0
        connection = progress(client, started)['connection']
        spec = {'provider': 'github', 'api_url': app.base, 'owner': 'synthetic', '_oauth_root': managed.root,
                '_oauth_role': 'source', 'oauth_id': connection['id']}
        assert gm.Provider.from_config(spec).authenticate().discover()[0]['name'] == 'demo'
        assert not any('/user/repos' in call[0] for call in fake.calls)


def test_unregistered_and_untrusted_instances_are_rejected(installation, monkeypatch):
    managed, setup = installation
    def unexpected(*args, **kwargs):
        pytest.fail('Untrusted endpoint was contacted')
    monkeypatch.setattr(gm, 'oauth_http', unexpected)
    data = app_registration('gitea')
    app = gm.oauth_registration(data)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        request = auth_payload(setup, app)
        resp = client.post(ROUTES + '/options', json=request)
        assert resp.status_code == 422 and 'No OAuth app registered' in resp.text
        path = registry(managed.root, data)
        for url in ['http://localhost/api/v1', 'https://169.254.169.254/api/v1',
                    'https://gitea.example.test.evil/api/v1', 'https://gitea.example.test/api/v1/../api/v1']:
            assert client.post(ROUTES + '/options', json={**request, 'api_url': url}).status_code == 422
        path.chmod(0o644)
        assert 'chmod 600' in client.post(ROUTES + '/options', json=request).text
    with pytest.raises(gm.OAuthError, match='exact IP addresses'):
        gm.oauth_registration(app_registration('gitea', private_ips=['192.168.0.0/16']))
    with pytest.raises(gm.OAuthError):
        gm.oauth_registration(app_registration('gitea', private_ips=['127.0.0.1']))


def test_oauth_https_blocks_redirect_and_dns_rebinding(monkeypatch):
    class Redirect:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    class Connection:
        def __init__(self, *args):
            pass

        def request(self, method, path, body, headers):
            assert path == '/oauth/token'

        def getresponse(self):
            return Redirect()

        def close(self):
            pass

    connection = gm.OAuthHTTPSConnection('gitea.example.test', 443, frozenset())
    monkeypatch.setattr(gm.socket, 'getaddrinfo', lambda *a, **kw: [(0, 0, 0, '', ('127.0.0.1', 443))])
    with pytest.raises(gm.OAuthError, match='network policy'):
        connection.connect()
    monkeypatch.setattr(gm, 'OAuthHTTPSConnection', Connection)
    with pytest.raises(gm.OAuthError, match='redirects are blocked'):
        gm.oauth_http('https://gitea.example.test/oauth/token', form={'code': PRIVATE[5]})


def test_ssh_endpoint_policy_rejects_host_and_port_changes(tmp_path):
    app = gm.oauth_registration(app_registration('gitea'))
    credential = gm.OAuthCredential(tmp_path, 'f' * 32, app, 'source', 'synthetic', True)
    credential.check_ssh('ssh://git@gitea.example.test:2222/synthetic/demo.git')
    for url in ('git@gitea.example.test:synthetic/demo.git', 'git@other.test:synthetic/demo.git',
                'https://gitea.example.test:2222/synthetic/demo.git', 'ssh://a@localhost:2222/demo.git'):
        with pytest.raises(gm.OAuthError):
            credential.check_ssh(url)


@pytest.mark.parametrize('version', ['1.22.6', '15.0.12+gitea-1.22.0'])
def test_gitea_requires_a_version_known_to_enforce_granular_scopes(installation, monkeypatch, version):
    managed, setup = installation
    data = app_registration('gitea')
    app = gm.oauth_registration(data)
    registry(managed.root, data)
    fake = MockProvider(app)
    fake.version = version
    monkeypatch.setattr(gm, 'oauth_http', fake)
    with TestClient(managed_app(managed), base_url=ORIGIN) as client:
        response = client.post(ROUTES + '/start', json=auth_payload(setup, app, method='browser'))
        assert response.status_code == 422 and 'granular scopes' in response.text
    assert [call[0] for call in fake.calls] == [app.base + '/version']
