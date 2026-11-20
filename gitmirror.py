"""GitMirror mirrors Git history across hosts and verifies local Git and LFS backups."""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import closing, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field, replace
import datetime as dt
import errno
import fnmatch
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from urllib.parse import urlparse
import urllib.request
import uuid

from filelock import FileLock, Timeout
import yaml

__version__ = "0.1.0a1"


# Configuration

PROVIDER_APIS = {
    'github': 'https://api.github.com',
    'gitlab': 'https://gitlab.com/api/v4',
    'codeberg': 'https://codeberg.org/api/v1',
    'gitea': None,
}
FORGE_PROVIDERS = ('codeberg', 'gitea')
PROVIDER_LABELS = {'github': 'GitHub', 'gitlab': 'GitLab', 'codeberg': 'Codeberg', 'gitea': 'Gitea'}

class ConfigError(ValueError):
    pass


def _mapping(data, name):
    if not isinstance(data, dict):
        raise ConfigError(f'{name} must be a YAML mapping')
    return data


def _token_source(cfg, label, *, managed=False):
    if 'token' in cfg:
        raise ConfigError(f'{label}: plaintext token in config is not supported; use token_env or token_file')
    if 'oauth_id' in cfg:
        if (not managed or any(k in cfg for k in ('token_env', 'token_file'))
                or not oauth_id(cfg['oauth_id'])):
            raise ConfigError(f'{label}: OAuth connections must be configured in the managed dashboard')
        return
    keys = [k for k in ('token_env', 'token_file') if k in cfg]
    if len(keys) != 1 or not isinstance(cfg[keys[0]], str) or not cfg[keys[0]].strip():
        raise ConfigError(f'{label}: specify exactly one token_env or token_file')


def _url(value, name):
    try:
        parsed = urlparse(value) if isinstance(value, str) else None
        valid = (parsed and parsed.scheme == 'https' and parsed.hostname and parsed.port != 0
                 and not any(c.isspace() or ord(c) < 32 or c in '\\@?#' for c in value)
                 and not parsed.params)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f'{name} must be an HTTPS API URL without credentials or query parameters')
    return value.rstrip('/')


def provider_config(spec, label, *, owner_optional=False, oauth_root=None, role='source'):
    cfg = {k: v for k, v in _mapping(spec, label).items() if k not in ('_oauth_root', '_oauth_role')}
    provider = cfg.get('provider')
    if not isinstance(provider, str) or provider not in PROVIDER_APIS:
        raise ConfigError(f'{label}.provider must be github, gitlab, codeberg or gitea')
    owner = cfg.get('owner')
    if not (owner_optional and owner is None):
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or owner in ('.', '..'):
            raise ConfigError(f'{label}.owner must be a provider account name')
    _token_source(cfg, label, managed=oauth_root is not None)
    base = _url(cfg.get('api_url', PROVIDER_APIS[provider]), f'{label}.api_url')
    if provider == 'gitea' and not urlparse(base).path.endswith('/api/v1'):
        raise ConfigError(f'{label}.api_url must include Gitea’s /api/v1 path')
    cfg['api_url'] = base
    if 'oauth_id' in cfg:
        cfg.update(_oauth_root=oauth_root, _oauth_role=role)
    return cfg


def config_path(value, label, *, absolute=False):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f'{label} must be a non-empty path')
    try:
        path = Path(os.path.expandvars(value)).expanduser()
        if absolute and not path.is_absolute():
            raise ConfigError(f'{label} must be an absolute path')
        return path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f'{label} is not a usable path') from exc


def retention_config(value, label):
    if value is None:
        return None
    policy = _mapping(value, label)
    if policy.keys() - {'keep', 'days', 'automatic'}:
        raise ConfigError(f'{label}: use keep, days and automatic')
    keep, days, auto = policy.get('keep', 1), policy.get('days'), policy.get('automatic', False)
    if (type(keep) is not int or keep < 1 or (days is not None and (type(days) is not int or days < 1))
            or type(auto) is not bool):
        raise ConfigError(f'{label}: keep and days must be positive integers and automatic must be boolean')
    return {'keep': keep, 'days': days, 'automatic': auto}


def load_config(path):
    file = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(file.read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        location = f' at line {mark.line + 1}' if mark else ''
        raise ConfigError(f'Invalid configuration YAML{location}') from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f'Cannot load config {file}: {exc}') from exc
    raw = _mapping(raw, 'config')
    version = raw.get('version')
    if type(version) is not int or version not in (1, 2):
        raise ConfigError('version must be 1 or 2')
    oauth_root = (file.parents[2] if version == 2 and file.name == 'config.yml'
                  and file.parent.parent.name == 'configurations' and oauth_id(file.parent.name) else None)
    source = provider_config(raw.get('source'), 'source', oauth_root=oauth_root)
    entries = raw.get('destinations')
    if version == 1:
        if source['provider'] != 'github':
            raise ConfigError('version 1 requires a GitHub source; use version 2 for other providers')
        entries = [{**_mapping(spec, 'destination'), 'provider': platform}
                   for platform, spec in _mapping(entries, 'destinations').items()]
        if any(spec['provider'] not in ('gitlab', 'codeberg') for spec in entries):
            raise ConfigError('version 1 supports GitLab and Codeberg destinations')
    if not isinstance(entries, list) or not entries:
        raise ConfigError('destinations must contain at least one destination')
    storage = _mapping(raw.get('storage', {'path': '~/gitmirror-data'}), 'storage')
    work_path = config_path(storage.get('path'), 'storage.path')
    dests = {}
    endpoint_accounts = set()
    local_roots = []
    for index, entry in enumerate(entries, 1):
        label = f'destinations[{index}]'
        cfg = _mapping(entry, label)
        platform = cfg.get('provider')
        identifier = cfg.get('id', platform)
        if (not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', identifier)
                or identifier in ('origin', 'all', 'both')):
            raise ConfigError(f'{label}.id must be a lowercase identifier distinct from origin, all and both')
        if identifier in dests:
            raise ConfigError(f'{label}: duplicate destination ID {identifier}; give each destination a unique id')
        if platform == 'local':
            if any(cfg.get(option, True) is not True for option in ('mirror', 'archive', 'preserve_history')):
                raise ConfigError(f'{label}: local backups require mirror, archive and preserve_history')
            local_path = config_path(cfg.get('path'), f'{label}.path', absolute=True)
            if local_path.is_relative_to(work_path) or work_path.is_relative_to(local_path):
                raise ConfigError('The local backup and working storage paths must be separate')
            if any(local_path.is_relative_to(root) or root.is_relative_to(local_path) for root in local_roots):
                raise ConfigError('Local backup destinations must have separate, non-overlapping paths')
            local_roots.append(local_path)
            cfg = {'provider': 'local', 'path': local_path,
                   'retention': retention_config(cfg.get('retention'), f'{label}.retention')}
        else:
            cfg = provider_config(cfg, label, owner_optional=version == 1 and platform == 'gitlab',
                                  oauth_root=oauth_root, role='destination')
            account = platform, cfg['api_url'], (cfg.get('owner') or '').casefold()
            if account == (source['provider'], source['api_url'], source['owner'].casefold()):
                raise ConfigError('The source account cannot also be a destination on the same host')
            if account in endpoint_accounts:
                raise ConfigError('The same remote account and API endpoint cannot be configured twice')
            endpoint_accounts.add(account)
        dests[identifier] = cfg
    retry = _mapping(raw.get('retry', {'delays_seconds': [3, 8, 20]}), 'retry')
    delays = retry.get('delays_seconds', [3, 8, 20])
    if (not isinstance(delays, list) or len(delays) > 8 or
        any(type(v) is not int or v < 0 or v > 300 for v in delays)):
        raise ConfigError('retry.delays_seconds must be a list of 0–8 integers from 0 to 300')
    retry['delays_seconds'] = delays
    schedule = _mapping(raw.get('schedule', {'interval_hours': 12}), 'schedule')
    hours = schedule.get('interval_hours', 12)
    if type(hours) not in (int, float) or not (0.1 <= hours <= 720):
        raise ConfigError('schedule.interval_hours must be between 0.1 and 720')
    schedule['interval_hours'] = hours
    if 'enabled' in schedule and type(schedule['enabled']) is not bool:
        raise ConfigError('schedule.enabled must be true or false')
    include = raw.get('repositories', [])
    if (not isinstance(include, list) or len(include) > 10000
            or any(not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo)
                   for repo in include)
            or len({repo.casefold() for repo in include}) != len(include)):
        raise ConfigError('repositories must be a list of distinct repository names')
    oauth_ssh = {'_oauth_ssh': file.parent / 'ssh.config'} if any(
        'oauth_id' in cfg for cfg in (source, *dests.values())) else {}
    return {'version': version, 'source': source, 'destinations': dests, **oauth_ssh,
            'storage': {'path': work_path}, 'retry': retry, 'schedule': schedule, 'repositories': include}


# Runtime, credentials, and transports

HOME = Path.home()
ROOT = HOME / 'gitmirror-data'
GL_API = 'https://gitlab.com/api/v4'
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
SSH_ENV = dict(os.environ)
LOG_SECRETS = ()
SSH_ENV.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=20')
JOURNAL_DIR = ROOT / 'gitlab-protection-journal'
RETRY_DELAYS = (3, 8, 20)
LFS_VERIFICATION_MAX_AGE = 7 * 24 * 60 * 60
TRANSIENT_GIT_ERRORS = (
    'connection closed', 'connection timed out', 'connection reset',
    'connection refused', 'connection aborted', 'broken pipe',
    'could not resolve hostname', 'temporary failure in name resolution',
    'network is unreachable', 'no route to host', 'kex_exchange_identification',
    'remote end hung up unexpectedly', 'operation timed out',
    'client_loop: send disconnect',
)

def configure_runtime(settings, *, apply):
    """Configure working storage and Git transport for a run."""
    global ROOT, GL_API
    global JOURNAL_DIR, RETRY_DELAYS, RUN_ID, SSH_ENV, LOG_SECRETS
    ROOT = settings['storage']['path']
    GL_API = PROVIDER_APIS['gitlab']
    JOURNAL_DIR = ROOT / 'gitlab-protection-journal'
    RETRY_DELAYS = tuple(settings['retry']['delays_seconds'])
    LOG_SECRETS = dashboard_secret_values(settings)
    RUN_ID = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    SSH_ENV = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_NAMESPACE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        SSH_ENV.pop(key, None)
    ssh = 'ssh -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=yes -o UpdateHostKeys=no'
    managed_ssh = str(settings.get('_oauth_ssh') or SSH_ENV.get('GITMIRROR_MANAGED_SSH_CONFIG') or '')
    if managed_ssh:
        ssh += f' -F {shlex.quote(managed_ssh)}'
    if apply:
        if managed_ssh:
            SSH_ENV['GIT_SSH_COMMAND'] = ssh
        else:
            SSH_ENV.setdefault('GIT_SSH_COMMAND', ssh)
    else:
        SSH_ENV['GIT_SSH_COMMAND'] = ssh + ' -o ControlPath=none -o AddKeysToAgent=no -o PermitLocalCommand=no'
        SSH_ENV['GIT_SSH_VARIANT'] = 'ssh'
        SSH_ENV['GIT_OPTIONAL_LOCKS'] = '0'
        SSH_ENV['GIT_TERMINAL_PROMPT'] = '0'


class SyncError(Exception):
    pass


class SourceStateError(SyncError):
    """The discovery snapshot can no longer be used for any destination."""


def token(spec):
    """Resolve a token from the environment or a separate file, never a YAML literal."""
    if 'token_env' in spec:
        key = spec['token_env']
        value = os.getenv(key, '').strip()
        if not value:
            raise SyncError(f'Environment variable {key} is empty or missing')
        return value
    path = Path(spec['token_file']).expanduser()
    try:
        value = path.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise SyncError(f'Cannot read token file: {path} ({exc})') from exc
    if not value:
        raise SyncError(f'Empty token file: {path}')
    return value


# Browser-authorized provider credentials

OAUTH_DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
OAUTH_REAUTHORIZE = 'Provider authorization expired or was revoked. Reconnect the account.'
OAUTH_PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ('10.0.0.0/8', '172.16.0.0/12',
                                                          '192.168.0.0/16', 'fc00::/7')))


class OAuthError(SyncError):
    pass


class OAuthNetworkError(OAuthError):
    pass


class OAuthRevoked(OAuthError):
    pass


def oauth_id(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{32}', value) is not None


def oauth_api_url(provider, value):
    if not isinstance(provider, str) or provider not in PROVIDER_APIS:
        raise OAuthError('Choose a hosting provider')
    if value is None or value == '':
        value = PROVIDER_APIS[provider]
    try:
        value = _url(value, 'OAuth API URL')
    except ConfigError:
        raise OAuthError('Use the registered HTTPS API URL') from None
    parts = urlparse(value)
    origin = urlsplit_origin(f'https://{parts.netloc.lower()}')
    if (not origin or parts.hostname.endswith('.') or '%' in parts.path
            or re.fullmatch(r'(?:/[A-Za-z0-9_.-]+)*', parts.path) is None
            or any(part in ('.', '..') for part in parts.path.split('/'))):
        raise OAuthError('Use the registered HTTPS API URL')
    host = f'[{parts.hostname}]' if ':' in parts.hostname else parts.hostname
    authority = f'{host}:{parts.port}' if parts.port not in (None, 443) else host
    base = f'https://{authority}{parts.path}'
    if provider in ('github', 'codeberg'):
        if base != PROVIDER_APIS[provider]:
            raise OAuthError('This provider supports browser authorization only at its public service')
    elif not parts.path.endswith('/api/v4' if provider == 'gitlab' else '/api/v1'):
        raise OAuthError('Use the registered API URL including its version path')
    return base


def oauth_private_file(path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in (os.geteuid(), 0)):
            raise OAuthError('OAuth files must be regular files restricted to their owner (chmod 600)')
        data = stream.read(262145)
    if len(data) > 262144:
        raise OAuthError('OAuth storage file exceeds the size limit')
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        raise OAuthError('OAuth storage contains invalid JSON') from None


def oauth_secret(value, *, limit=32768):
    return isinstance(value, str) and 0 < len(value) <= limit and value.isascii() and all(
        33 <= ord(char) <= 126 for char in value)


@dataclass(frozen=True)
class OAuthApp:
    provider: str
    base: str
    client_id: str
    secret: str = field(repr=False)
    redirect: str = ''
    device: bool = False
    private_ips: frozenset = frozenset()
    ssh_port: int = 22

    @property
    def issuer(self):
        if self.provider == 'github':
            return 'https://github.com'
        if self.provider == 'codeberg':
            return 'https://codeberg.org'
        return self.base.rsplit('/api/', 1)[0]

    @property
    def callback_path(self):
        return f'/api/managed/oauth/{self.provider}/callback'

    @property
    def token_url(self):
        suffix = '/oauth/token' if self.provider == 'gitlab' else '/login/oauth/access_token'
        return self.issuer + suffix

    @property
    def permissions_url(self):
        suffix = {'github': '/settings/installations', 'gitlab': '/-/user_settings/applications'}.get(
            self.provider, '/user/settings/applications')
        return self.issuer + suffix

    def scopes(self, role):
        if self.provider == 'github':
            return 'offline_access'
        if self.provider == 'gitlab':
            return 'read_api' if role == 'source' else 'api'
        if self.provider == 'gitea':
            return 'read:user read:repository' if role == 'source' else 'read:user write:repository'
        return ''

    @property
    def ssh_endpoint(self):
        return urlparse(self.issuer).hostname, self.ssh_port


def oauth_registration(data):
    if not isinstance(data, dict):
        raise OAuthError('Invalid OAuth application registration')
    provider = data.get('provider')
    base = oauth_api_url(provider, data.get('api_url'))
    client, secret = data.get('client_id'), data.get('client_secret', '')
    redirect, device = data.get('redirect_uri', ''), data.get('device', provider == 'github')
    allowed, port = data.get('private_ips', []), data.get('ssh_port', 22)
    if (not isinstance(client, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,256}', client)
            or (secret != '' and not oauth_secret(secret, limit=1024)) or type(device) is not bool
            or type(port) is not int or not 1 <= port <= 65535 or not isinstance(allowed, list)
            or len(allowed) > 16 or not isinstance(redirect, str)):
        raise OAuthError('Invalid OAuth application registration')
    try:
        ip_set = frozenset(ipaddress.ip_address(value) for value in allowed if isinstance(value, str))
    except ValueError:
        raise OAuthError('OAuth private address policy must use exact IP addresses') from None
    if (any(not isinstance(value, str) for value in allowed)
            or any(not any(ip in network for network in OAUTH_PRIVATE_NETWORKS) for ip in ip_set)
            or ((ip_set or port != 22) and base == PROVIDER_APIS[provider])):
        raise OAuthError('Private IP and nonstandard SSH port exceptions are for approved self-hosted instances')
    app = OAuthApp(provider, base, client, secret, redirect, device, ip_set, port)
    if provider == 'github':
        if not device or redirect:
            raise OAuthError('GitHub Apps use device authorization and require Device Flow to be enabled')
    elif ((device and (provider != 'gitlab' or secret))
          or (not redirect and not device)
          or (redirect and (not redirect.endswith(app.callback_path)
                            or not (origin := urlsplit_origin(redirect.removesuffix(app.callback_path)))
                            or origin.scheme != 'https'))):
        raise OAuthError('Register the exact HTTPS callback and use a public GitLab client for device authorization')
    return app


def oauth_app(root, provider, value):
    if os.name == 'nt':
        raise OAuthError('Browser authorization requires a managed host with private POSIX storage. Use an API token here.')
    base = oauth_api_url(provider, value)
    try:
        registrations = oauth_private_file(root / 'oauth-apps.json')
    except FileNotFoundError:
        raise OAuthError('No OAuth app registered. Ask the server administrator to configure oauth-apps.json.') from None
    except OSError:
        raise OAuthError('Cannot read the OAuth application registry') from None
    if not isinstance(registrations, list) or len(registrations) > 100:
        raise OAuthError('OAuth application registry must contain a list of registrations')
    matches = []
    for data in registrations:
        app = oauth_registration(data)
        if (app.provider, app.base) == (provider, base):
            matches.append(app)
    if len(matches) != 1:
        raise OAuthError('Register exactly one OAuth app for this provider and HTTPS API URL')
    return matches[0]


class OAuthHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a checked address without another DNS lookup of the provider hostname."""

    def __init__(self, host, port, private_ips):
        super().__init__(host, port, timeout=12, context=ssl.create_default_context())
        self.private_ips = private_ips

    def connect(self):
        addresses = list(dict.fromkeys(
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)))
        if not addresses or any(
            ip not in self.private_ips and (not ip.is_global or ip.is_multicast or ip.is_reserved)
            for ip in addresses
        ):
            raise OAuthError('Provider DNS returned an address outside the approved OAuth network policy')
        deadline = time.monotonic() + self.timeout
        for index, ip in enumerate(addresses):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                raw = socket.create_connection((str(ip), self.port), min(remaining, 5))
            except OSError:
                if index == len(addresses) - 1:
                    raise
            else:
                break
        try:
            raw.settimeout(max(0.1, deadline - time.monotonic()))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(self.timeout)
        except BaseException:
            raw.close()
            raise


def oauth_http(url, *, private_ips=frozenset(), method='GET', headers=None, form=None, payload=None):
    """HTTPS without proxies or redirects. Provider errors never include remote payloads."""
    parts = urlparse(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise OAuthError('Invalid OAuth request URL')
    headers = dict(headers or {}, Accept='application/json')
    body = None
    if form is not None:
        method, body = 'POST', urllib.parse.urlencode(form).encode('ascii')
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
    try:
        with closing(OAuthHTTPSConnection(parts.hostname, parts.port or 443, private_ips)) as connection:
            connection.request(method, path, body, headers)
            with connection.getresponse() as response:
                status = response.status
                if 300 <= status < 400:
                    raise OAuthError('OAuth and provider API redirects are blocked')
                raw = response.read((8 << 20) + 1)
    except (OSError, http.client.HTTPException, ValueError):
        raise OAuthNetworkError('Secure provider connection failed') from None
    if len(raw) > (8 << 20):
        raise OAuthError('Provider response exceeded the size limit')
    try:
        parsed = json.loads(raw) if raw else None
    except (ValueError, RecursionError):
        if status < 400:
            raise OAuthError('Provider returned an invalid OAuth or API response') from None
        parsed = None
    return status, parsed


def oauth_token_response(status, data):
    # Use only known error categories. Some endpoints echo credentials in descriptions.
    if status >= 500 or status in (408, 429):
        raise OAuthNetworkError('Provider temporarily unavailable. Try again.')
    error = data.get('error') if isinstance(data, dict) else None
    if status != 200 or error:
        return {'invalid_token': 'invalid_grant', 'bad_refresh_token': 'invalid_grant',
                'token_expired': 'expired_token'}.get(error, error) if isinstance(error, str) else 'provider_error'
    return None


def oauth_tokens(data, previous=None):
    now = time.time()
    previous = previous or {}
    access = data.get('access_token') if isinstance(data, dict) else None
    refresh = data.get('refresh_token', previous.get('refresh_token')) if isinstance(data, dict) else None
    lifetime = data.get('expires_in') if isinstance(data, dict) else None
    refresh_lifetime = data.get('refresh_token_expires_in') if isinstance(data, dict) else None
    if (not oauth_secret(access) or not oauth_secret(refresh)
            or not isinstance(data.get('token_type'), str) or data['token_type'].lower() != 'bearer'
            or type(lifetime) is not int or not 0 < lifetime <= 366 * 86400
            or (refresh_lifetime is not None
                and (type(refresh_lifetime) is not int or not 0 < refresh_lifetime <= 5 * 366 * 86400))):
        raise OAuthError('Provider did not issue usable expiring credentials and a refresh token')
    refresh_expiry = (now + refresh_lifetime if refresh_lifetime else
                      previous.get('refresh_expires_at') if refresh == previous.get('refresh_token') else None)
    return {'access_token': access, 'refresh_token': refresh, 'expires_at': now + lifetime,
            'refresh_expires_at': refresh_expiry, 'refresh_at': now + lifetime - min(75, lifetime / 5)}


def oauth_directory(root, *, create=False):
    directory = root / 'oauth'
    if os.name == 'nt':
        raise OAuthError('OAuth credential storage requires private POSIX permissions')
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise OAuthError('OAuth credential directory must be service-owned and private (chmod 700)')
    return directory


def oauth_file(root, identifier, *, create=False):
    if not oauth_id(identifier):
        raise OAuthError('Unknown OAuth connection')
    return oauth_directory(root, create=create) / f'{identifier}.json'


def oauth_record(root, identifier):
    try:
        result = oauth_private_file(oauth_file(root, identifier))
    except FileNotFoundError:
        raise OAuthError('OAuth account is disconnected. Reconnect or configure an API token.') from None
    except OSError:
        raise OAuthError('Cannot read private OAuth credentials') from None
    if (not isinstance(result, dict) or result.get('status') not in ('connected', 'reconnect_required', 'disconnected')
            or not isinstance(result.get('account'), str) or not isinstance(result.get('client_id'), str)
            or type(result.get('account_id')) is not int or not isinstance(result.get('api_url'), str)
            or not isinstance(result.get('provider'), str) or result.get('role') not in ('source', 'destination')
            or type(result.get('last_checked')) not in (int, float)
            or (result['status'] == 'connected'
                and (type(result.get('expires_at')) not in (int, float)
                     or type(result.get('refresh_at')) not in (int, float)
                     or type(result.get('refresh_expires_at')) not in (type(None), int, float)
                     or not oauth_secret(result.get('access_token'))))):
        raise OAuthError('OAuth credential record is invalid')
    return result


def store_oauth(path, record):
    temporary = path.with_name(f'.{path.stem}.{uuid.uuid4().hex}.tmp')
    try:
        private_text(temporary, json.dumps(record))
        os.replace(temporary, path)
        fsync_directory(path.parent)
    except OSError:
        raise OAuthError('Cannot write private OAuth credentials') from None
    finally:
        temporary.unlink(missing_ok=True)


def oauth_binding(record, app, role, owner=None):
    if ((record.get('provider'), record.get('api_url'), record.get('role')) != (app.provider, app.base, role)
            or (owner is not None and record.get('account', '').casefold() != owner.casefold())):
        raise OAuthError('OAuth connection belongs to a different provider, instance, account or role')


def oauth_public(record, identifier):
    state = record['status']
    now = time.time()
    if state == 'connected' and (
            record.get('identity_pending') or now >= record['expires_at']
            or (record.get('refresh_expires_at') and now >= record['refresh_expires_at'])):
        state = ('refresh_due' if record.get('refresh_token') and
                 (not record.get('refresh_expires_at') or now < record['refresh_expires_at']) else 'reconnect_required')
    return {'id': identifier, 'status': state, 'account': record['account'],
            'provider': record['provider'], 'api_url': record['api_url'], 'role': record['role'],
            'expires_at': record.get('expires_at'), 'last_checked': record.get('last_checked')}


def oauth_without_tokens(record, status):
    keys = ('provider', 'api_url', 'role', 'account', 'account_id', 'client_id', 'last_checked')
    return {**{key: record[key] for key in keys}, 'status': status}


def oauth_headers(app, access):
    headers = {'Authorization': f'Bearer {access}'}
    if app.provider == 'github':
        headers['X-GitHub-Api-Version'] = '2022-11-28'
    return headers


def oauth_account(app, access):
    status, user = oauth_http(f'{app.base}/user', headers=oauth_headers(app, access), private_ips=app.private_ips)
    if status == 401:
        raise OAuthRevoked(OAUTH_REAUTHORIZE)
    if status == 403:
        raise OAuthError('Provider refused access to the authorized account')
    key = 'username' if app.provider == 'gitlab' else 'login'
    login = user.get(key) if isinstance(user, dict) else None
    if (status != 200 or not isinstance(login, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', login)
            or login in ('.', '..') or type(user.get('id')) is not int or user['id'] <= 0):
        raise OAuthError('Cannot verify the provider account')
    return login, user['id']


def oauth_exchange(app, **parameters):
    return oauth_http(app.token_url, private_ips=app.private_ips, form={
        'client_id': app.client_id,
        **({'client_secret': app.secret} if app.secret and app.provider != 'github' else {}),
        **parameters})


def oauth_gitlab_scope(app, role, account_id, data, access):
    scope = data.get('scope')
    if scope is None:
        status, info = oauth_http(f'{app.issuer}/oauth/token/info', private_ips=app.private_ips,
                                  headers=oauth_headers(app, access))
        application = info.get('application') if isinstance(info, dict) else None
        if (status != 200 or not isinstance(application, dict) or application.get('uid') != app.client_id
                or info.get('resource_owner_id') != account_id or not isinstance(info.get('scope'), list)):
            raise OAuthError('GitLab did not confirm the OAuth application and scope')
        granted = info['scope']
    else:
        granted = scope.split() if isinstance(scope, str) else []
    if app.scopes(role) not in granted:
        raise OAuthError('GitLab did not grant the scope required for this account')


def oauth_access(root, identifier, app, role, owner, *, refresh):
    def checked_record():
        record = oauth_record(root, identifier)
        oauth_binding(record, app, role, owner)
        if (record.get('client_id') != app.client_id or record.get('status') != 'connected'
                or not oauth_secret(record.get('access_token'))):
            raise OAuthError(OAUTH_REAUTHORIZE)
        return record

    record = checked_record()
    if time.time() < record['refresh_at'] and not record.get('identity_pending'):
        return record['access_token']
    if not refresh:
        if time.time() < record['expires_at'] and not record.get('identity_pending'):
            return record['access_token']
        raise OAuthError('OAuth credentials need renewal. Check the connection in Settings or run an applied sync.')
    path = oauth_file(root, identifier)
    try:
        with FileLock(str(path.with_suffix('.lock')), timeout=25):
            record = checked_record()
            now = time.time()
            if now >= record['refresh_at']:
                expiration = record.get('refresh_expires_at')
                if not oauth_secret(record.get('refresh_token')) or (expiration is not None and expiration <= now):
                    store_oauth(path, oauth_without_tokens(record, 'reconnect_required'))
                    raise OAuthError(OAUTH_REAUTHORIZE)
                status, data = oauth_exchange(app, grant_type='refresh_token', refresh_token=record['refresh_token'])
                error = oauth_token_response(status, data)
                if error in ('invalid_grant', 'expired_token', 'invalid_client', 'incorrect_client_credentials'):
                    store_oauth(path, oauth_without_tokens(record, 'reconnect_required'))
                    raise OAuthError(OAUTH_REAUTHORIZE)
                if error:
                    raise OAuthError('Provider rejected OAuth renewal. Check the registered application or reconnect.')
                record.update(oauth_tokens(data, previous=record), identity_pending=True)
                # Rotation may already have invalidated the old pair. Save the new pair before the identity read.
                store_oauth(path, record)
            if record.get('identity_pending'):
                try:
                    account, account_id = oauth_account(app, record['access_token'])
                except OAuthRevoked:
                    store_oauth(path, oauth_without_tokens(record, 'reconnect_required'))
                    raise
                if account.casefold() != owner.casefold() or account_id != record.get('account_id'):
                    store_oauth(path, oauth_without_tokens(record, 'reconnect_required'))
                    raise OAuthError('Provider returned credentials for a different account. Reconnect the account.')
                if app.provider == 'gitlab':
                    oauth_gitlab_scope(app, role, account_id, {}, record['access_token'])
                record.update(identity_pending=False, last_checked=time.time())
                store_oauth(path, record)
            return record['access_token']
    except Timeout:
        raise OAuthError('OAuth credential renewal is busy. Try again.') from None


@dataclass(frozen=True, repr=False)
class OAuthCredential:
    root: Path
    identifier: str
    app: OAuthApp
    role: str
    owner: str
    refresh: bool

    def revoke_locally(self, access):
        if self.refresh:
            path = oauth_file(self.root, self.identifier)
            with FileLock(str(path.with_suffix('.lock')), timeout=25):
                record = oauth_record(self.root, self.identifier)
                if record.get('access_token') == access:
                    store_oauth(path, oauth_without_tokens(record, 'reconnect_required'))

    def request(self, url, headers, method, payload):
        if not url.startswith(self.app.base + '/'):
            raise OAuthError('Provider API URL differs from the OAuth connection')
        access = oauth_access(self.root, self.identifier, self.app, self.role, self.owner, refresh=self.refresh)
        status, data = oauth_http(url, headers={**headers, **oauth_headers(self.app, access)}, method=method,
                                  payload=payload, private_ips=self.app.private_ips)
        if status == 401:
            self.revoke_locally(access)
            raise OAuthError(OAUTH_REAUTHORIZE)
        return status, data

    def check_account(self):
        access = oauth_access(self.root, self.identifier, self.app, self.role, self.owner, refresh=self.refresh)
        try:
            account, account_id = oauth_account(self.app, access)
        except OAuthRevoked:
            self.revoke_locally(access)
            raise
        path = oauth_file(self.root, self.identifier)
        if self.refresh:
            with FileLock(str(path.with_suffix('.lock')), timeout=25):
                record = oauth_record(self.root, self.identifier)
                if account.casefold() != self.owner.casefold() or record.get('account_id') != account_id:
                    raise OAuthError('OAuth account identity changed. Reconnect the account.')
                if record.get('access_token') == access:
                    store_oauth(path, {**record, 'last_checked': time.time()})
        elif (account.casefold() != self.owner.casefold()
              or oauth_record(self.root, self.identifier).get('account_id') != account_id):
            raise OAuthError('OAuth account identity changed. Reconnect the account.')

    def check_ssh(self, url):
        if not valid_ssh_url(url):
            raise OAuthError('Provider did not return a valid SSH repository URL')
        if url.startswith('ssh://'):
            parts = urlparse(url)
            endpoint = parts.hostname, parts.port or 22
        else:
            host_and_path = url.split('@', 1)[1]
            host = host_and_path.split(']:', 1)[0][1:] if host_and_path.startswith('[') else host_and_path.split(':', 1)[0]
            endpoint = host.casefold(), 22
        if endpoint != self.app.ssh_endpoint:
            raise OAuthError('Repository SSH host or port differs from the registered provider instance')


def github_installation(owner, role, request):
    for page in range(1, 101):
        status, data = request(f'/user/installations?per_page=100&page={page}')
        items = data.get('installations') if isinstance(data, dict) else None
        if status != 200 or not isinstance(items, list):
            break
        for item in items:
            account = item.get('account') if isinstance(item, dict) else None
            if (not isinstance(account, dict) or not isinstance(account.get('login'), str)
                    or account['login'].casefold() != owner.casefold() or item.get('suspended_at')):
                continue
            permissions = item.get('permissions') or {}
            if (type(item.get('id')) is int and item['id'] > 0 and isinstance(permissions, dict)
                    and permissions.get('metadata') in ('read', 'write')
                    and (role == 'source' or (permissions.get('administration') == 'write'
                                             and item.get('repository_selection') == 'all'))):
                return item['id']
        if len(items) < 100:
            break
    raise OAuthError('GitHub App installation or repository permissions are missing for this account')


def oauth_repository_access(app, role, account, access):
    def read(path):
        return oauth_http(app.base + path, private_ips=app.private_ips, headers=oauth_headers(app, access))

    if app.provider == 'github':
        installation = github_installation(account, role, read)
        status, data = read(f'/user/installations/{installation}/repositories?per_page=1')
        repos = data.get('repositories') if isinstance(data, dict) else None
        if role == 'source' and not repos:
            raise OAuthError('No repositories are authorized. Check the GitHub App installation and repository selection.')
    elif app.provider == 'gitlab':
        status, repos = read('/projects?owned=true&per_page=1')
    else:
        status, repos = read('/user/repos?limit=1')
    if status != 200 or not isinstance(repos, list):
        raise OAuthError('Provider did not confirm repository API access. Check app scopes and account permissions.')


@dataclass(repr=False)
class OAuthFlow:
    app: OAuthApp
    role: str
    browser: str
    session: str
    origin: str
    owner: str
    connection: str
    method: str
    end: float
    identifier: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = 'starting'
    state: str = ''
    verifier: str = ''
    device_code: str = ''
    user_code: str = ''
    url: str = ''
    expires_at: float | None = None
    interval: int = 5
    next_poll: float = 0
    message: str = ''
    result: dict | None = None

    def finish(self, state, message=''):
        self.status, self.message = state, message
        self.state = self.verifier = self.device_code = self.user_code = self.url = ''

    def public(self):
        return {'flow': self.identifier, 'status': self.status, 'method': self.method,
                'url': self.url or None, 'user_code': self.user_code or None,
                'expires_at': self.expires_at, 'message': self.message, 'connection': self.result}


class OAuthFlows:
    """Short-lived grants remain in memory and are bound to one browser and local authorization."""

    def __init__(self, installation, authorized):
        self.installation = installation
        self.authorized = authorized
        self.flows = {}
        self.lock = threading.Lock()

    def get(self, identifier, browser, session, origin):
        flow = self.flows.get(identifier)
        if (flow is None or (flow.browser, flow.session, flow.origin) != (browser, session, origin)
                or not self.authorized(flow.session)):
            raise OAuthError('Authorization is unavailable. Start a new connection.')
        if time.monotonic() >= flow.end and flow.status in ('starting', 'awaiting', 'working'):
            flow.finish('failed', 'Approval expired. Start a new connection.')
        return flow

    def start(self, flow):
        with self.lock:
            now = time.monotonic()
            self.flows = {key: item for key, item in self.flows.items() if item.end + 600 > now}
            for item in self.flows.values():
                if item.end <= now and item.status in ('starting', 'awaiting', 'working'):
                    item.finish('failed', 'Approval expired. Start a new connection.')
            if len(self.flows) >= 128 or sum(
                    item.status in ('starting', 'awaiting', 'working') and item.end > now
                    for item in self.flows.values()) >= 12:
                raise OAuthError('Too many pending connections. Cancel one or wait for approval to expire.')
            if any(item.connection == flow.connection and item.status in ('starting', 'awaiting', 'working')
                   and item.end > now for item in self.flows.values()):
                raise OAuthError('This account already has a pending connection')
            self.flows[flow.identifier] = flow
        try:
            if flow.app.provider == 'gitea':
                status, data = oauth_http(f'{flow.app.base}/version', private_ips=flow.app.private_ips)
                version = data.get('version') if isinstance(data, dict) else None
                match = re.match(r'^1\.(\d+)\.\d+(?:\+|$)', version) if isinstance(version, str) else None
                if status != 200 or not match or int(match[1]) < 23:
                    raise OAuthError('Browser authorization requires a Gitea 1.x release with granular scopes (1.23+)')
            if flow.method == 'device':
                self.start_device(flow)
            else:
                self.start_browser(flow)
        except (OAuthError, OSError) as exc:
            with self.lock:
                flow.finish('failed', str(exc) if isinstance(exc, OAuthError) else 'Cannot start provider authorization')
            raise
        with self.lock:
            return flow.public()

    def start_device(self, flow):
        app = flow.app
        suffix, verify = (('/login/device/code', '/login/device') if app.provider == 'github'
                          else ('/oauth/authorize_device', '/oauth/device'))
        status, data = oauth_http(app.issuer + suffix, private_ips=app.private_ips,
                                  form={'client_id': app.client_id, 'scope': app.scopes(flow.role)})
        if oauth_token_response(status, data):
            message = ('GitLab device approval is unavailable. Try browser approval or check the application grants.'
                       if app.provider == 'gitlab' else 'GitHub device approval failed. Check the App Client ID and enable Device Flow.')
            raise OAuthError(message)
        if not isinstance(data, dict):
            raise OAuthError('Provider returned invalid device approval details')
        url = data.get('verification_uri')
        code, user_code, lifetime = data.get('device_code'), data.get('user_code'), data.get('expires_in')
        interval = data.get('interval', 5)
        if (url != app.issuer + verify or not oauth_secret(code, limit=2048)
                or not isinstance(user_code, str) or not re.fullmatch(r'[A-Za-z0-9-]{4,32}', user_code)
                or code == user_code or type(lifetime) is not int or not 0 < lifetime <= 3600
                or type(interval) is not int or not 1 <= interval <= 3600):
            raise OAuthError('Provider returned invalid device approval details')
        with self.lock:
            if flow.status != 'starting':
                return
            flow.status, flow.device_code, flow.user_code, flow.url = 'awaiting', code, user_code, url
            now = time.monotonic()
            flow.interval, flow.end = interval, min(flow.end, now + lifetime)
            flow.next_poll, flow.expires_at = now + interval, time.time() + max(0, flow.end - now)

    def start_browser(self, flow):
        app = flow.app
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode().rstrip('=')
        state = secrets.token_urlsafe(32)
        path = '/oauth/authorize' if app.provider == 'gitlab' else '/login/oauth/authorize'
        query = {'client_id': app.client_id, 'redirect_uri': app.redirect, 'response_type': 'code',
                 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        if scopes := app.scopes(flow.role):
            query['scope'] = scopes
        with self.lock:
            if flow.status != 'starting':
                return
            flow.status, flow.verifier, flow.state = 'awaiting', verifier, state
            flow.url = app.issuer + path + '?' + urllib.parse.urlencode(query)
            flow.expires_at = time.time() + max(0, flow.end - time.monotonic())

    def poll(self, identifier, browser, session, origin):
        with self.lock:
            flow = self.get(identifier, browser, session, origin)
            if flow.method != 'device' or flow.status != 'awaiting' or time.monotonic() < flow.next_poll:
                return flow.public()
            code = flow.device_code
            flow.status = 'working'
        try:
            status, data = oauth_exchange(flow.app, grant_type=OAUTH_DEVICE_GRANT, device_code=code)
            error = 'slow_down' if status == 429 else oauth_token_response(status, data)
            if error in ('authorization_pending', 'slow_down'):
                hint = data.get('interval') if isinstance(data, dict) else None
                self.defer(flow, error == 'slow_down', hint)
            elif error:
                self.fail(flow, {
                    'expired_token': 'Approval expired. Start a new connection.',
                    'access_denied': 'The provider did not receive consent.',
                }.get(error, 'Device approval failed. Check the registration and permissions or retry in the browser.'))
            else:
                self.complete(flow, data)
        except OAuthNetworkError:
            self.defer(flow, True)
        except (OAuthError, OSError, Timeout) as exc:
            self.fail(flow, str(exc) if isinstance(exc, OAuthError) else 'Account storage is busy or unavailable. Try again.')
        with self.lock:
            return flow.public()

    def defer(self, flow, slow, hint=None):
        with self.lock:
            if flow.status != 'working':
                return
            if time.monotonic() >= flow.end:
                flow.finish('failed', 'Approval expired. Start a new connection.')
                return
            if slow:
                flow.interval += 5
            if type(hint) is int and 1 <= hint <= 3600:
                flow.interval = max(flow.interval, hint)
            flow.next_poll = time.monotonic() + flow.interval
            flow.status = 'awaiting'

    def callback(self, provider, query, browser, origin):
        state = query.get('state')
        if not isinstance(state, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', state):
            return False
        with self.lock:
            flow = next((item for item in self.flows.values() if item.state and
                         secrets.compare_digest(item.state, state)), None)
            if (not flow or flow.app.provider != provider or (flow.browser, flow.origin) != (browser, origin)
                    or not self.authorized(flow.session) or flow.status != 'awaiting'):
                return False
            if time.monotonic() >= flow.end:
                flow.finish('failed', 'Approval expired. Start a new connection.')
                return False
            verifier = flow.verifier
            flow.state = flow.verifier = flow.url = ''
            flow.status = 'working'
        try:
            if ('error' in query or not oauth_secret(query.get('code'), limit=2048)
                    or ('iss' in query and query['iss'] != flow.app.issuer)):
                raise OAuthError('The provider declined approval or returned an invalid response')
            status, data = oauth_exchange(flow.app, grant_type='authorization_code', code=query['code'],
                                          redirect_uri=flow.app.redirect, code_verifier=verifier)
            if oauth_token_response(status, data):
                raise OAuthError('Provider rejected the authorization code. Check the callback URI and try again.')
            self.complete(flow, data)
        except (OAuthError, OSError, Timeout) as exc:
            self.fail(flow, str(exc) if isinstance(exc, OAuthError) else 'Account storage is busy or unavailable. Try again.')
        return flow.status == 'connected'

    def fail(self, flow, message):
        with self.lock:
            if flow.status == 'working':
                flow.finish('failed', message)

    def complete(self, flow, data):
        app = flow.app
        try:
            record = oauth_tokens(data)
        except OAuthError:
            if app.provider == 'gitlab' and flow.method == 'device':
                raise OAuthError('GitLab device approval did not issue refreshable credentials. Try browser approval.') from None
            raise
        account, account_id = oauth_account(app, record['access_token'])
        if app.provider == 'gitlab':
            oauth_gitlab_scope(app, flow.role, account_id, data, record['access_token'])
        if flow.owner and flow.owner.casefold() != account.casefold():
            raise OAuthError('Signed in to a different provider account. Reconnect with the configured account.')
        oauth_repository_access(app, flow.role, account, record['access_token'])
        with self.lock:
            if (flow.status != 'working' or time.monotonic() >= flow.end or not self.authorized(flow.session)):
                if flow.status == 'working':
                    flow.finish('failed', 'Approval expired. Start a new connection.')
                return
            path = oauth_file(self.installation.root, flow.connection, create=True)
            with FileLock(str(self.installation.work / '.gitmirror.lock'), timeout=0):
                if not self.authorized(flow.session):
                    flow.finish('failed', 'Dashboard authorization changed. Start a new connection.')
                    return
                with FileLock(str(path.with_suffix('.lock')), timeout=0):
                    if path.exists():
                        old = oauth_record(self.installation.root, flow.connection)
                        oauth_binding(old, app, flow.role, account)
                        if old['account_id'] != account_id:
                            raise OAuthError('The provider account identity changed. Verify your login and app.')
                    store_oauth(path, {**record, 'status': 'connected', 'provider': app.provider, 'role': flow.role,
                                      'api_url': app.base, 'account': account, 'account_id': account_id,
                                      'client_id': app.client_id, 'last_checked': time.time()})
            flow.result = self.installation.oauth_status(flow.connection)
            flow.finish('connected')

    def cancel(self, identifier, browser, session, origin):
        with self.lock:
            flow = self.get(identifier, browser, session, origin)
            if flow.status != 'connected':
                flow.finish('cancelled', 'Connection cancelled.')
            return flow.public()

    def disconnect(self, identifier):
        with self.lock:
            for flow in self.flows.values():
                if flow.connection == identifier and flow.status in ('starting', 'awaiting', 'working'):
                    flow.finish('cancelled', 'Connection cancelled.')
        path = oauth_file(self.installation.root, identifier)
        with FileLock(str(self.installation.work / '.gitmirror.lock'), timeout=0):
            with FileLock(str(path.with_suffix('.lock')), timeout=0):
                record = oauth_record(self.installation.root, identifier)
                store_oauth(path, oauth_without_tokens(record, 'disconnected'))
        try:
            app = oauth_app(self.installation.root, record['provider'], record['api_url'])
            tokens = [record[key] for key in ('refresh_token', 'access_token') if oauth_secret(record.get(key))]
            if app.client_id == record['client_id'] and tokens:
                if app.provider == 'github':
                    status, _ = oauth_http(f'{app.base}/credentials/revoke', method='POST', payload={'credentials': tokens})
                    if status == 202:
                        return 'Provider revocation requested.'
                elif app.provider == 'gitlab':
                    client = {'client_id': app.client_id, **({'client_secret': app.secret} if app.secret else {})}
                    statuses = [oauth_http(f'{app.issuer}/oauth/revoke', private_ips=app.private_ips,
                                           form={**client, 'token': token})[0] for token in tokens]
                    if all(status == 200 for status in statuses):
                        return 'Provider revocation requested.'
        except OAuthError:
            pass
        return 'Credentials removed here. Revoke the grant in the provider’s application settings.'


def retry_notice(target, attempt, delay, reason):
    print(f'    RETRY {attempt}/{len(RETRY_DELAYS)} in {delay}s [{target}]: {reason}', flush=True)
    time.sleep(delay)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        old, new = urlparse(request.full_url), urlparse(new_url)
        try:
            safe = (request.get_method() == 'GET' and old.scheme == new.scheme == 'https'
                    and old.hostname == new.hostname and (old.port or 443) == (new.port or 443)
                    and new.username is None and new.password is None)
        except ValueError:
            safe = False
        if not safe:
            raise urllib.error.HTTPError(request.full_url, code, 'Unsafe API redirect', headers, fp)
        return super().redirect_request(request, fp, code, message, headers, new_url)


API_OPENER = urllib.request.build_opener(SameOriginRedirect)


def api(url, headers, method='GET', payload=None):
    """Retry only idempotent reads for transient HTTP/network errors.

    Never retry POST: a timed-out repository creation may already have succeeded.
    A later run will discover it safely by name.
    """
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    h = dict(headers, Accept='application/json', **({'Content-Type':'application/json'} if data is not None else {}))
    retryable = method == 'GET'
    credential = h.pop('_oauth', None)
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            if credential:
                status, response = credential.request(url, h, method, payload)
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, 'Provider API request failed', {}, None)
                return status, response
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with API_OPENER.open(req, timeout=45) as res:
                raw = res.read()
                return res.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            transient = e.code in (408, 429, 500, 502, 503, 504)
            if retryable and transient and attempt < len(RETRY_DELAYS):
                retry_notice('API', attempt + 1, RETRY_DELAYS[attempt], f'HTTP {e.code}')
                continue
            raise SyncError(f'Provider API returned HTTP {e.code}') from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OAuthNetworkError) as e:
            if retryable and attempt < len(RETRY_DELAYS):
                retry_notice('API', attempt + 1, RETRY_DELAYS[attempt], type(e).__name__)
                continue
            raise SyncError(f'Provider API request failed ({type(e).__name__})') from e
        except (json.JSONDecodeError, UnicodeError) as e:
            raise SyncError('Provider API returned invalid JSON') from e


def cmd(args, *, check=True, cwd=None, timeout=420, input_text=None, env=None, retry=True,
        before_retry=None):
    """Retry Git commands only when the failure looks like a transport issue."""
    # Git's stdin protocols require LF even on Windows.
    input_bytes = None if input_text is None else input_text.encode('utf-8', 'surrogateescape')
    attempts = len(RETRY_DELAYS) + 1 if retry else 1
    for attempt in range(attempts):
        if attempt and before_retry:
            before_retry()
        try:
            p = subprocess.run(args, capture_output=True, cwd=cwd, input=input_bytes,
                               env=SSH_ENV if env is None else env, timeout=timeout)
            p.stdout = p.stdout.decode('utf-8', 'surrogateescape').replace('\r\n', '\n')
            p.stderr = p.stderr.decode('utf-8', 'surrogateescape').replace('\r\n', '\n')
        except subprocess.TimeoutExpired as e:
            if args[0] == 'git' and attempt + 1 < attempts:
                retry_notice('Git', attempt + 1, RETRY_DELAYS[attempt], 'command timeout')
                continue
            raise SyncError(f'{args[0]} timed out after {timeout} seconds') from e
        except OSError as e:
            raise SyncError(f'Command failed to run: {args[0]} ({e})') from e
        if check and p.returncode:
            err = redact_dashboard_log((p.stderr or p.stdout or '').strip(), LOG_SECRETS)[-1400:]
            transient = args[0] == 'git' and any(x in err.lower() for x in TRANSIENT_GIT_ERRORS)
            if transient and attempt + 1 < attempts:
                retry_notice('Git', attempt + 1, RETRY_DELAYS[attempt],
                             err.splitlines()[0][:120] if err else 'SSH network failure')
                continue
            raise SyncError(f'{args[0]} failed (exit={p.returncode})\n{err}')
        return p


def git(*args, repo=None, check=True, timeout=420, input_text=None, env=None, retry=True,
        before_retry=None):
    a = ['git']
    if os.name == 'nt':
        a += ['-c', 'core.longpaths=true']
    if repo is not None:
        a += ['-C', str(repo)]
    a += list(args)
    return cmd(a, check=check, timeout=timeout, input_text=input_text, env=env, retry=retry,
               before_retry=before_retry)


# Git references and LFS backups

def parse_refs(text, prefix='refs'):
    result = {}
    for line in text.split('\n'):
        bits = line.split('\t', 1)
        if len(bits) != 2:
            continue
        oid, ref = bits
        if ref.endswith('^{}') or not re.fullmatch('[0-9a-f]{40,64}', oid):
            continue
        if ref.startswith((f'{prefix}/heads/', f'{prefix}/tags/')):
            result['refs/' + ref[len(prefix) + 1:]] = oid
    return result


def remote_refs(url):
    return parse_refs(git('ls-remote', '--heads', '--tags', '--', url, timeout=100).stdout)


def verify_remote_head(url, branch):
    expected = f'ref: refs/heads/{branch}\tHEAD'
    result = git('ls-remote', '--symref', '--', url, 'HEAD', timeout=100)
    if expected not in result.stdout.split('\n'):
        raise SyncError(f'Destination Git HEAD is not confirmed at {branch}; refusing to continue')


def local_refs(repo, prefix='refs'):
    out = git('for-each-ref', '--format=%(objectname)%09%(refname)',
              f'{prefix}/heads/', f'{prefix}/tags/', repo=repo).stdout
    return parse_refs(out, prefix)


def count_changes(src, dst):
    a = sum(k not in dst for k in src)
    b = sum(k in dst and v != dst[k] for k, v in src.items())
    c = sum(k not in src for k in dst)
    return a, b, c


def ensure_remote(repo, remote, url):
    if git('remote', 'get-url', remote, repo=repo, check=False).returncode:
        git('remote', 'add', remote, url, repo=repo)
    else:
        git('remote', 'set-url', remote, url, repo=repo)


@contextmanager
def lfs_snapshot(repo, refs, *, scratch=None):
    """Share Git objects with an isolated repository containing only these refs."""
    try:
        with tempfile.TemporaryDirectory(prefix='.lfs-check-', dir=scratch or repo) as directory:
            snapshot = Path(directory)
            object_format = git('rev-parse', '--show-object-format', repo=repo).stdout.strip()
            git('init', '--bare', '--template=', f'--object-format={object_format}', directory)
            env = {**SSH_ENV, 'GIT_OBJECT_DIRECTORY': str(repo.resolve() / 'objects')}
            # LFS can borrow objects from Git alternates, masking a missing remote object.
            env.pop('GIT_ALTERNATE_OBJECT_DIRECTORIES', None)
            updates = ''.join(f'create {ref} {oid}\n' for ref, oid in refs.items())
            git('update-ref', '--stdin', repo=snapshot, input_text=updates, env=env)
            yield snapshot, env
    except OSError as exc:
        raise SyncError(f'Cannot verify Git LFS objects: {exc}') from exc


def lfs_objects(repo, refs, *, scratch=None):
    """List LFS objects in the full history of these refs, excluding other backups."""
    if not refs:
        return {}
    with lfs_snapshot(repo, refs, scratch=scratch) as (snapshot, env):
        inventory = git('lfs', 'ls-files', '--all', '--json', '--include=', '--exclude=',
                        repo=snapshot, timeout=1200, env=env).stdout
    try:
        objects = {}
        for item in json.loads(inventory)['files'] or []:
            oid, size = item['oid'], item['size']
            if (not re.fullmatch(r'[0-9a-f]{64}', oid) or type(size) is not int
                    or size <= 0 or objects.setdefault(oid, size) != size):
                raise ValueError('Invalid Git LFS object')
        return objects
    except (ValueError, KeyError, TypeError) as exc:
        raise SyncError('Could not read the Git LFS object inventory') from exc


def verify_lfs_objects(media_dir, objects):
    for oid, size in objects.items():
        path = media_dir / oid[:2] / oid[2:4] / oid
        try:
            with path.open('rb') as file:
                if os.fstat(file.fileno()).st_size != size or hashlib.file_digest(file, 'sha256').hexdigest() != oid:
                    raise SyncError(f'Corrupt Git LFS object: {oid}')
        except OSError as exc:
            raise SyncError(f'Missing or unreadable Git LFS object: {oid}') from exc


def fetch_backup_lfs(repo, remote, refs, objects):
    if not objects:
        return
    git('lfs', 'fetch', '--all', remote, *refs, repo=repo, timeout=1800)
    verify_lfs_objects(lfs_media_dir(repo), objects)


def lfs_media_dir(repo):
    for line in git('lfs', 'env', repo=repo).stdout.splitlines():
        if line.startswith('LocalMediaDir='):
            return Path(line.removeprefix('LocalMediaDir='))
    raise SyncError('Git LFS did not report its local object directory')


def verify_archived_lfs(repo):
    out = git('for-each-ref', '--format=%(refname)%09%(objectname)', 'refs/archive/', repo=repo)
    refs = dict(line.split('\t', 1) for line in out.stdout.split('\n') if line)
    objects = lfs_objects(repo, refs)
    if objects:
        verify_lfs_objects(lfs_media_dir(repo), objects)


def verify_destination_lfs(repo, remote, refs, objects, *, full=False):
    if not objects:
        return
    # Scope evidence to this destination and its LFS endpoint/configuration.
    config = git('config', '--get-regexp', rf'^(lfs\.|remote\.{re.escape(remote)}\.|url\.)',
                 repo=repo, check=False)
    if config.returncode not in (0, 1):
        raise SyncError('Cannot inspect Git LFS configuration')
    tracked = git('rev-parse', '--verify', 'HEAD:.lfsconfig', repo=repo, check=False)
    identity = hashlib.sha256(f'{remote}\n{config.stdout}\n{tracked.stdout}'.encode(
        'utf-8', 'surrogateescape')).hexdigest()
    path = repo / f'gitmirror-lfs-{remote}.json'
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        record = {}
    history = record.get('objects') if isinstance(record, dict) and record.get('identity') == identity else None
    if not isinstance(history, dict):
        history = {}
    now = time.time()
    fresh = {}
    if not full:
        for oid, size in objects.items():
            entry = history.get(oid)
            if (isinstance(entry, list) and len(entry) == 2 and entry[0] == size
                    and type(entry[1]) in (int, float)
                    and now - LFS_VERIFICATION_MAX_AGE < entry[1] <= now):
                fresh[oid] = entry
    pending = {oid: size for oid, size in objects.items() if oid not in fresh}
    if not pending:
        print(f'    Git LFS: reused recent read-back evidence for {len(fresh)} objects')
        return
    # Invalidate the pending evidence before the network call, including on interruption.
    try:
        store_json(path, {'identity': identity, 'objects': fresh})
    except OSError as exc:
        raise SyncError('Cannot update the Git LFS verification record') from exc
    media_dir = lfs_media_dir(repo) if fresh else None
    with lfs_snapshot(repo, refs) as (snapshot, env):
        storage = snapshot / 'lfs'
        # Retain named remotes and their auth settings. Set storage locally so it
        # cannot leak to the other side of Git LFS's standalone file transport.
        git('config', '--local', '--add', 'include.path', str(repo.resolve() / 'config'), repo=snapshot)
        git('config', '--local', '--add', 'lfs.storage', str(storage), repo=snapshot)
        head = git('symbolic-ref', '--quiet', 'HEAD', repo=repo, check=False)
        if head.returncode == 0:
            git('symbolic-ref', 'HEAD', head.stdout.strip(), repo=snapshot, env=env)
        # Seed only recently verified bytes. Git LFS skips these and retrieves the rest.
        for oid in fresh:
            relative = Path(oid[:2], oid[2:4], oid)
            target = storage / 'objects' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(media_dir / relative, target)
            except OSError:
                shutil.copyfile(media_dir / relative, target)
        git('-c', 'lfs.remote.searchall=false', '-c', 'lfs.remote.autodetect=false',
            'lfs', 'fetch', '--all', remote, *refs, repo=snapshot, env=env, timeout=1800)
        verify_lfs_objects(storage / 'objects', pending)
    checked_at = time.time()
    fresh.update({oid: [size, checked_at] for oid, size in pending.items()})
    try:
        store_json(path, {'identity': identity, 'objects': fresh})
    except OSError as exc:
        raise SyncError('Cannot save the Git LFS verification record') from exc
    print(f'    Git LFS: read back {len(pending)} objects; reused evidence for {len(objects) - len(pending)}')


def archive_source_old_tips(repo, previous, provider):
    """Keep each previously observed source head/tag object reachable forever."""
    for sha in set(previous.values()):
        ref = f'refs/archive/{provider}/{sha}'
        if git('show-ref', '--verify', '--quiet', ref, repo=repo, check=False).returncode:
            git('update-ref', ref, sha, repo=repo)


def backup_destination_refs(repo, platform, url, refs):
    """Fetch destination heads/tags into immutable, timestamped local refs before overwriting."""
    if not refs:
        return
    prefix = f'refs/archive/{RUN_ID}/{platform}'
    staging = f'refs/gitmirror-staging/{RUN_ID}/{platform}'
    git('fetch', '--atomic', '--no-tags', '--no-prune', '--refmap=', url,
        f'+refs/heads/*:{staging}/heads/*',
        f'+refs/tags/*:{staging}/tags/*', repo=repo, timeout=1200)
    saved = local_refs(repo, staging)
    if saved != refs:
        missing, changed, unexpected = count_changes(refs, saved)
        raise SyncError(f'Destination backup ref mismatch ({platform}): '
                        f'{missing} missing, {changed} changed, {unexpected} unexpected')
    staged = [f'{staging}/{ref.removeprefix("refs/")}' for ref in saved]
    objects = lfs_objects(repo, saved)
    if objects:
        ensure_remote(repo, platform, url)
        fetch_backup_lfs(repo, platform, staged, objects)
    # Publish only a complete snapshot and verify the staging values in the same transaction.
    updates = []
    for ref, oid in saved.items():
        suffix = ref.removeprefix('refs/')
        updates.extend((f'create {prefix}/{suffix} {oid}', f'delete {staging}/{suffix} {oid}'))
    git('update-ref', '--stdin', repo=repo, input_text='\n'.join(updates) + '\n')
    print(f'    Backup verified {len(saved)} destination refs and {len(objects)} LFS objects under {prefix}')


def push_snapshot(repo, remote, source, previous, *, url=None, before_push=None):
    """Only change refs covered by the verified destination snapshot."""
    changed = sorted(ref for ref in source.keys() | previous.keys()
                     if source.get(ref) != previous.get(ref))
    if not changed:
        return
    push_urls = git('remote', 'get-url', '--push', '--all', remote, repo=repo).stdout.splitlines()
    fetch_url = git('remote', 'get-url', remote, repo=repo).stdout.strip()
    if push_urls != [fetch_url]:
        raise SyncError('Push requires one URL matching the destination used for backup')
    url = fetch_url if url is None else url
    if remote_refs(url) != previous:
        raise SyncError('Destination changed since the verified backup (stale info)')
    leases = [f'--force-with-lease={ref}:{previous.get(ref, "")}' for ref in changed]
    refspecs = [f'{source.get(ref, "")}:{ref}' for ref in changed]
    arguments = ('--no-follow-tags', *leases, '--', remote, *refspecs)
    env = {**SSH_ENV, 'LC_ALL': 'C', 'LANGUAGE': 'C'}
    if before_push:
        before_push()
    try:
        result = git('push', '--atomic', *arguments, repo=repo, env=env,
                     check=False, retry=False, timeout=1800)
        if result.returncode and 'the receiving end does not support --atomic push' in result.stderr:
            if len(changed) > 1:
                raise SyncError('Destination lacks atomic push support; multiple refs were left untouched')
            if remote_refs(url) != previous:
                raise SyncError('Destination changed before the single-ref push')
            if before_push:
                before_push()
            result = git('push', '--no-atomic', *arguments, repo=repo, env=env,
                         check=False, retry=False, timeout=1800)
    except SourceStateError:
        raise
    except SyncError as exc:
        if remote_refs(url) != previous:
            raise SyncError(f'Push failed and destination refs changed; inspect the verified backup ({exc})') from exc
        raise
    actual = remote_refs(url)
    if result.returncode:
        state = 'destination refs changed; inspect the verified backup' if actual != previous else 'destination refs unchanged'
        detail = redact_dashboard_log((result.stderr or result.stdout).strip(), LOG_SECRETS)[-900:]
        raise SyncError(f'Push failed ({state}): {detail}')
    if actual != source:
        raise SyncError('Push did not produce the complete snapshot; inspect the verified backup')


# Provider APIs

def desired_visibility(desired_private):
    return 'private' if desired_private else 'public'


def scope_id(spec):
    identity = json.dumps([spec['provider'], spec['api_url'], spec['owner'].casefold()])
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return f'{spec["provider"]}-{digest}'


@dataclass(frozen=True)
class Destination:
    id: int
    url: str
    visibility: str
    default_branch: str | None


@dataclass(frozen=True)
class Provider:
    kind: str
    base: str
    owner: str | None
    headers: dict = field(repr=False)

    @property
    def label(self):
        return PROVIDER_LABELS[self.kind]

    @classmethod
    def from_config(cls, spec, *, refresh=True):
        kind = spec['provider']
        if 'oauth_id' in spec:
            root = spec['_oauth_root']
            app = oauth_app(root, kind, spec['api_url'])
            credential = OAuthCredential(root, spec['oauth_id'], app, spec['_oauth_role'], spec['owner'], refresh)
            return cls(kind, app.base, spec['owner'], {'_oauth': credential})
        secret = token(spec)
        if kind == 'gitlab':
            headers = {'PRIVATE-TOKEN': secret}
        elif kind in FORGE_PROVIDERS:
            headers = {'Authorization': f'token {secret}'}
        else:
            headers = {'Authorization': f'Bearer {secret}', 'X-GitHub-Api-Version': '2022-11-28'}
        return cls(kind, spec['api_url'], spec.get('owner'), headers)

    def authenticate(self):
        if credential := self.headers.get('_oauth'):
            credential.check_account()
            if self.kind == 'github' and credential.role == 'destination':
                github_installation(self.owner, credential.role, lambda path: api(self.base + path, self.headers))
            return self
        status, info = api(f'{self.base}/user', self.headers)
        field = 'username' if self.kind == 'gitlab' else 'login'
        login = info.get(field) if isinstance(info, dict) else None
        if status != 200 or not isinstance(login, str) or not login:
            raise SyncError(f'Cannot validate {self.label} account')
        if self.owner and login.casefold() != self.owner.casefold():
            raise SyncError(f'Authenticated {self.label} account differs from the configured owner')
        return replace(self, owner=self.owner or login)

    def repo_url(self, name):
        if self.kind == 'gitlab':
            return f'{self.base}/projects/{urllib.parse.quote(f"{self.owner}/{name}", safe="")}'
        owner, repo = (urllib.parse.quote(part, safe='') for part in (self.owner, name))
        return f'{self.base}/repos/{owner}/{repo}'

    def repo_state(self, info):
        """Normalize hosting metadata without assuming an unknown repository is public."""
        if not isinstance(info, dict):
            raise SyncError(f'Invalid {self.label} repository metadata')
        if self.kind == 'gitlab':
            path = info.get('path_with_namespace')
            owner, _, name = path.rpartition('/') if isinstance(path, str) else ('', '', '')
            visibility = info.get('visibility')
            url = info.get('ssh_url_to_repo')
        else:
            account = info.get('owner')
            owner = account.get('login') if isinstance(account, dict) else None
            name = info.get('name')
            url = info.get('ssh_url')
            private = info.get('private')
            visibility = None
            if type(private) is bool:
                visibility = info.get('visibility', desired_visibility(private))
                if (visibility == 'public') == private:
                    visibility = None
        if visibility not in ('public', 'private', 'internal'):
            raise SyncError(f'{self.label} visibility changed or is unconfirmed')
        if (type(info.get('id')) is not int or info['id'] <= 0
                or not all(isinstance(value, str) and value for value in (owner, name, url))):
            raise SyncError(f'{self.label} repository identity changed or is invalid')
        if credential := self.headers.get('_oauth'):
            credential.check_ssh(url)
        branch = info.get('default_branch')
        if self.kind == 'gitea':
            if not valid_ssh_url(url):
                raise SyncError('Gitea did not return a valid SSH repository URL')
            if 'default_branch' not in info or (branch is not None and not isinstance(branch, str)):
                raise SyncError('Gitea did not confirm the default branch')
        return {'id': info['id'], 'name': name, 'owner': owner, 'ssh_url': url,
                'private': visibility != 'public', 'visibility': visibility, 'default_branch': branch}

    @staticmethod
    def source_size_bytes(kind, metadata):
        """Reported repository size, not a measurement of LFS or physical disk usage."""
        if kind == 'gitlab':
            statistics = metadata.get('statistics')
            value = statistics.get('repository_size') if isinstance(statistics, dict) else None
        else:
            value = metadata.get('size')
        if type(value) is not int or value < 0 or value > 10**15:
            return None
        return value if kind == 'gitlab' else value * 1024

    def discover(self):
        repos = {}
        query = {
            'github': '/user/repos?affiliation=owner&visibility=all&per_page=100',
            'gitlab': '/projects?owned=true&statistics=true&per_page=100',
        }.get(self.kind, '/user/repos?limit=50')
        github_oauth = self.kind == 'github' and '_oauth' in self.headers
        if github_oauth:
            installation = github_installation(self.owner, 'source', lambda path: api(self.base + path, self.headers))
            query = f'/user/installations/{installation}/repositories?per_page=100'
        seen = set()
        for page in range(1, 101):
            status, items = api(f'{self.base}{query}&page={page}', self.headers)
            if github_oauth and isinstance(items, dict):
                items = items.get('repositories')
            if status != 200 or not isinstance(items, list):
                raise SyncError(f'{self.label} returned an invalid repository list')
            for item in items:
                record = self.repo_state(item)
                record['size_bytes'] = self.source_size_bytes(self.kind, item)
                if record['id'] in seen:
                    raise SyncError(f'{self.label} discovery returned duplicate repository IDs')
                seen.add(record['id'])
                if record['owner'].casefold() != self.owner.casefold():
                    continue
                name = record['name'].casefold()
                if name in repos:
                    raise SyncError(f'{self.label} discovery returned duplicate repository names')
                repos[name] = record
            # Gitea and Forgejo may cap pages below the requested length.
            if not items or (self.kind not in FORGE_PROVIDERS and len(items) < 100):
                break
        else:
            raise SyncError(f'{self.label} discovery did not finish after 100 pages')
        if not repos:
            raise SyncError(f'Zero {self.label} repositories detected: aborting; no destinations touched')
        return [repos[name] for name in sorted(repos)]

    def confirm_source(self, record):
        name = record['name']
        try:
            status, info = api(self.repo_url(name), {**self.headers, 'Cache-Control': 'no-cache'})
        except SyncError as exc:
            raise SourceStateError(f'Cannot recheck {self.label} state for {name}; stopping this sync') from exc
        if status != 200:
            raise SourceStateError(f'{self.label} state for {name} is unavailable; stopping this sync')
        try:
            current = self.repo_state(info)
        except SyncError as exc:
            raise SourceStateError(str(exc)) from exc
        for fields, description in ((('id', 'owner', 'name', 'ssh_url'), 'repository identity'),
                                    (('private', 'visibility'), 'visibility'),
                                    (('default_branch',), 'default branch')):
            if any(current[key] != record[key] for key in fields):
                raise SourceStateError(f'{self.label} {description} changed for {name}; stopping this sync')

    def read_destination(self, name, *, create=False):
        repo_url = self.repo_url(name)
        headers = {**self.headers, 'Cache-Control': 'no-cache'}
        status, obj = api(repo_url, headers)
        created = status == 404 and create
        if status == 404:
            if not create:
                return None
            if self.kind == 'gitlab':
                url = f'{self.base}/projects'
                payload = {'name': name, 'path': name, 'visibility': 'private', 'builds_access_level': 'disabled'}
            else:
                url = f'{self.base}/user/repos'
                payload = {'name': name, 'private': True, 'auto_init': False}
            status, obj = api(url, self.headers, method='POST', payload=payload)
            if status != 201:
                raise SyncError(f'Cannot create private {self.label} destination for {name} (HTTP {status})')
            if self.kind == 'gitea':
                new = self.gitea_destination(name, obj)
                status, obj = api(repo_url, headers)
                if status != 200 or new.visibility != 'private' or self.gitea_destination(name, obj) != new:
                    raise SyncError(f'New Gitea destination could not be read back as the same private repository for {name}')
        if self.kind == 'gitea':
            if status != 200:
                raise SyncError(f'Cannot read Gitea destination for {name} (HTTP {status})')
            return self.gitea_destination(name, obj)
        if status not in (200, 201) or not isinstance(obj, dict):
            raise SyncError(f'Cannot read {self.kind} destination for {name}')
        if self.kind == 'gitlab':
            url, visibility = obj.get('ssh_url_to_repo'), obj.get('visibility')
        else:
            url, private = obj.get('ssh_url'), obj.get('private')
            visibility = obj.get('visibility', desired_visibility(private)) if type(private) is bool else None
            if (visibility == 'public') == private:
                visibility = None
        if not isinstance(url, str) or not url or visibility not in ('private', 'public', 'internal'):
            raise SyncError(f'Cannot verify {self.kind} destination URL or visibility for {name}')
        if credential := self.headers.get('_oauth'):
            credential.check_ssh(url)
        if created and visibility != 'private':
            raise SyncError(f'New {self.kind} destination is not confirmed private for {name}')
        default_branch = obj.get('default_branch')
        if (type(obj.get('id')) is not int or 'default_branch' not in obj
                or (default_branch is not None and not isinstance(default_branch, str))):
            raise SyncError(f'Cannot verify {self.kind} destination identity or default branch for {name}')
        return Destination(obj['id'], url, visibility, default_branch or None)

    def gitea_destination(self, name, info):
        repo = self.repo_state(info)
        if repo['owner'].casefold() != self.owner.casefold() or repo['name'].casefold() != name.casefold():
            raise SyncError(f'Gitea destination owner or repository name does not match {name}')
        if info.get('archived') is True or info.get('mirror') is True:
            raise SyncError(f'Gitea destination {name} is archived or managed as a pull mirror')
        return Destination(repo['id'], repo['ssh_url'], repo['visibility'], repo['default_branch'] or None)

    def update_destination(self, name, destination, **change):
        """Write once and confirm the result even if the response was ambiguous."""
        expected = replace(destination, **change)
        if self.kind == 'gitlab':
            url, method = f'{self.base}/projects/{destination.id}', 'PUT'
        else:
            url, method = self.repo_url(name), 'PATCH'
        payload = change
        if self.kind in FORGE_PROVIDERS and 'visibility' in change:
            payload = {'private': change['visibility'] == 'private'}

        attribute = next(iter(change))
        error = None
        try:
            status, _ = api(url, self.headers, method=method, payload=payload)
            if status != 200:
                error = f'HTTP {status}'
        except SyncError as exc:
            error = str(exc)
        try:
            actual = self.read_destination(name)
        except SyncError as exc:
            raise SyncError(f'{self.kind}: {attribute} update not verified for {name}; '
                            f'write issue={error or "none"}; read issue={exc}') from exc
        if actual != expected:
            raise SyncError(f'{self.kind}: {attribute} update not verified for {name}; write issue={error or "none"}')
        print(f'    {self.kind}: {attribute} verified as {getattr(expected, attribute)}')
        return expected


def valid_ssh_url(value):
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\s\x00-\x1f\x7f\\]', value):
        return False
    if value.startswith('ssh://'):
        try:
            parsed = urlparse(value)
            return bool(parsed.hostname and parsed.port != 0 and parsed.username
                        and re.fullmatch(r'[A-Za-z0-9_.-]+', parsed.username)
                        and parsed.password is None and re.fullmatch(r'/[A-Za-z0-9_./+-]+', parsed.path)
                        and not any(c in value for c in '?#'))
        except ValueError:
            return False
    return bool(re.fullmatch(r'[A-Za-z0-9_.-]+@(?:[A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\]):/?[A-Za-z0-9_./+-]+', value))


def git_address(url):
    """Compare expanded Git endpoints, including equivalent SSH URL spellings."""
    expanded = git('ls-remote', '--get-url', '--', url).stdout.strip()
    try:
        parsed = urlparse(expanded)
        if parsed.hostname:
            port = parsed.port or {'ssh': 22, 'http': 80, 'https': 443, 'git': 9418}.get(parsed.scheme)
            return parsed.hostname.casefold(), port, parsed.path.strip('/').removesuffix('.git').casefold()
    except ValueError as exc:
        raise SyncError('Cannot compare malformed Git endpoint') from exc
    match = re.fullmatch(r'[^@]+@([^:]+):(.+)', expanded)
    if match:
        host, path = match.groups()
        return host.casefold(), 22, path.strip('/').removesuffix('.git').casefold()
    return os.path.normcase(expanded.rstrip('/'))


def reject_source_as_destination(source, record, provider, destination):
    if destination is None:
        return
    same_instance = source.kind == provider.kind and source.base == provider.base
    if ((same_instance and record['id'] == destination.id)
            or git_address(record['ssh_url']) == git_address(destination.url)):
        raise SyncError('Source and destination resolve to the same repository')


# GitLab branch protection and recovery journal

@contextmanager
def protection_scope(settings, spec):
    """Select the journal and API for one GitLab account while retaining v1 paths."""
    global GL_API, JOURNAL_DIR
    if spec['provider'] != 'gitlab':
        yield
        return
    old_api, old_journal = GL_API, JOURNAL_DIR
    GL_API = spec['api_url']
    JOURNAL_DIR = ROOT / 'gitlab-protection-journal'
    if settings['version'] == 2:
        JOURNAL_DIR /= scope_id(spec)
    try:
        yield
    finally:
        GL_API, JOURNAL_DIR = old_api, old_journal

# GitLab protected branch support. Only exact, project-owned rules are
# modified. Wildcard/group rules and protected tags are never bypassed.
# A write-ahead journal prevents a killed process from leaving permissions
# temporarily relaxed without detection/recovery on the next --apply run.


def gl_list_all(project_id, resource, headers):
    items = []
    for page in range(1, 102):
        status, obj = api(f'{GL_API}/projects/{project_id}/{resource}?per_page=100&page={page}', headers)
        if status != 200 or not isinstance(obj, list):
            raise SyncError(f'Cannot read GitLab {resource} (project {project_id})')
        items.extend(obj)
        if len(obj) < 100:
            return items
    raise SyncError(f'Abnormal GitLab pagination for {resource}')


def gl_rule_url(project_id, branch_name):
    return f'{GL_API}/projects/{project_id}/protected_branches/{urllib.parse.quote(branch_name, safe="")}'


def gl_rule(project_id, branch_name, headers):
    status, obj = api(gl_rule_url(project_id, branch_name), headers)
    if status == 404:
        return None
    if status != 200 or not isinstance(obj, dict):
        raise SyncError(f'Cannot inspect protection on {branch_name} in GitLab project {project_id}')
    return obj


def gl_branch_exists(project_id, branch_name, headers):
    url = f'{GL_API}/projects/{project_id}/repository/branches/{urllib.parse.quote(branch_name, safe="")}'
    status, obj = api(url, headers)
    if status == 404:
        return False
    if status != 200 or not isinstance(obj, dict):
        raise SyncError(f'Cannot determine whether GitLab branch {branch_name} exists')
    return True


def gl_patch_force(project_id, branch_name, enabled, headers):
    _, obj = api(gl_rule_url(project_id, branch_name), headers, method='PATCH',
                 payload={'allow_force_push': bool(enabled)})
    if not isinstance(obj, dict) or obj.get('allow_force_push') is not bool(enabled):
        raise SyncError(f'GitLab did not confirm allow_force_push={enabled} on {branch_name}')
    check = gl_rule(project_id, branch_name, headers)
    if not check or check.get('allow_force_push') is not bool(enabled):
        raise SyncError(f'GitLab did not persist allow_force_push={enabled} on {branch_name}')


def rule_backup_payload(rule):
    """Recreate only straightforward role-based branch protections exactly.

    Unsupported custom permissions are blocked instead of guessed. This is
    used only to restore a deleted rule if Git fails before deleting its branch.
    """
    if rule.get('inherited') or rule.get('code_owner_approval_required'):
        raise SyncError(f'Complex GitLab branch protection needs manual handling: {rule.get("name")}')
    payload = {'name': rule['name'], 'allow_force_push': bool(rule.get('allow_force_push'))}
    for key, field in (
        ('push_access_levels', 'push_access_level'),
        ('merge_access_levels', 'merge_access_level'),
        ('unprotect_access_levels', 'unprotect_access_level'),
    ):
        levels = rule.get(key)
        if not isinstance(levels, list) or len(levels) != 1:
            raise SyncError(f'Complex protection ACL on {rule["name"]}: {key}')
        entry = levels[0]
        if not isinstance(entry.get('access_level'), int) or any(
            entry.get(x) is not None for x in ('user_id', 'group_id', 'deploy_key_id', 'member_role_id')
        ):
            raise SyncError(f'Custom GitLab protection ACL on {rule["name"]}: {key}')
        payload[field] = entry['access_level']
    return payload


def journal_path(project_id):
    return JOURNAL_DIR / f'{project_id}.json'


def load_journal(project_id):
    path = journal_path(project_id)
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SyncError(f'Invalid protection recovery journal {path}: {e}') from e
    if not isinstance(entries, list):
        raise SyncError(f'Invalid protection recovery journal format: {path}')
    return entries


def store_json(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(temp, 'w', encoding='utf-8') as file:
            json.dump(value, file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    if os.name != 'nt':
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def store_journal(project_id, entries):
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    store_json(journal_path(project_id), entries)


def add_journal(project_id, action, name, **kwargs):
    entries = load_journal(project_id)
    entry = {'action': action, 'name': name, 'id': uuid.uuid4().hex, **kwargs}
    entries.append(entry)
    store_journal(project_id, entries)
    return entry


def finish_journal(project_id, entry_id):
    store_journal(project_id, [x for x in load_journal(project_id) if x.get('id') != entry_id])


def restore_gl_permissions(project_id, headers):
    """Complete pending restores, including actions from interrupted runs."""
    entries = list(load_journal(project_id))
    for entry in reversed(entries):
        name = entry['name']
        action = entry['action']
        if action == 'force':
            current = gl_rule(project_id, name, headers)
            if not current:
                raise SyncError(f'Recovery blocked: protection {name} was removed unexpectedly')
            if current.get('allow_force_push') is not False:
                gl_patch_force(project_id, name, False, headers)
            print(f'    GitLab restored force-push protection: {name}')
        elif action == 'unprotect-delete':
            # If the branch was already deleted, the rule must remain absent.
            if gl_branch_exists(project_id, name, headers):
                current = gl_rule(project_id, name, headers)
                if current is None:
                    status, _ = api(f'{GL_API}/projects/{project_id}/protected_branches',
                                    headers, method='POST', payload=entry['payload'])
                    if status != 201 or not gl_rule(project_id, name, headers):
                        raise SyncError(f'Failed to restore protection on surviving branch {name}')
                print(f'    GitLab restored protection on surviving branch: {name}')
            else:
                print(f'    GitLab branch deleted, obsolete protection not recreated: {name}')
        else:
            raise SyncError(f'Unknown protection journal action: {action}')
        finish_journal(project_id, entry['id'])


def recover_all_gl_permissions(headers):
    if not JOURNAL_DIR.exists():
        return
    for path in sorted(JOURNAL_DIR.glob('*.json')):
        if not path.stem.isdigit():
            raise SyncError(f'Unrecognized GitLab journal file: {path}')
        project_id = int(path.stem)
        if load_journal(project_id):
            print(f'Recovering GitLab branch protection changes for project {project_id}...')
            restore_gl_permissions(project_id, headers)


def protection_matches(pattern, name):
    return fnmatch.fnmatchcase(name, pattern)


def plan_gl_permissions(project_id, default_branch, src, old, headers):
    """Read-only protection plan, using only exact project-owned rules."""
    protected_branches = gl_list_all(project_id, 'protected_branches', headers)
    protected_tags = gl_list_all(project_id, 'protected_tags', headers)
    for ref in set(src) | set(old):
        if not ref.startswith('refs/tags/') or src.get(ref) == old.get(ref):
            continue
        tag = ref[len('refs/tags/'):]
        affected = [r['name'] for r in protected_tags if protection_matches(r['name'], tag)]
        if affected:
            raise SyncError(f'Protected GitLab tag {tag} differs; refusing to bypass tag rules: {affected}')

    changes = [(ref[len('refs/heads/'):], ref not in src)
               for ref in set(src) | set(old)
               if ref.startswith('refs/heads/') and src.get(ref) != old.get(ref)]
    changes.sort()
    operations = []
    for branch, deleted in changes:
        matches = [r for r in protected_branches if protection_matches(r['name'], branch)]
        if not matches:
            continue
        if len(matches) != 1 or matches[0]['name'] != branch or matches[0].get('inherited'):
            raise SyncError(f'Cannot safely automate wildcard/inherited GitLab protection on {branch}')
        rule = matches[0]
        if deleted:
            if branch == default_branch:
                raise SyncError(f'Refusing deletion of GitLab default branch {branch}; align defaults first')
            payload = rule_backup_payload(rule)
            operations.append(('unprotect-delete', branch, payload))
        elif not rule.get('allow_force_push'):
            operations.append(('force', branch, None))

    return operations


def prepare_gl_permissions(project_id, default_branch, src, old, headers):
    """Apply prevalidated, journaled permission edits after backup succeeds."""
    operations = plan_gl_permissions(project_id, default_branch, src, old, headers)
    for action, branch, payload in operations:
        if action == 'force':
            add_journal(project_id, action, branch)
            gl_patch_force(project_id, branch, True, headers)
            print(f'    GitLab temporarily enabled force push: {branch}')
        else:
            add_journal(project_id, action, branch, payload=payload)
            status, _ = api(gl_rule_url(project_id, branch), headers, method='DELETE')
            if status != 204:
                raise SyncError(f'GitLab did not unprotect branch {branch} (HTTP {status})')
            print(f'    GitLab temporarily unprotected stale branch for deletion: {branch}')


# Independent local mirrors and archives

BACKUP_MARKER = '.gitmirror-volume.json'
BACKUP_GENERATION = re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\.git')
BACKUP_MANIFEST = 'gitmirror-backup.json'


def backup_volume(path):
    try:
        stat = path.stat()
        marker = json.loads((path / BACKUP_MARKER).read_text(encoding='utf-8'))
        if (not path.is_dir() or not isinstance(marker, dict) or marker.get('version') != 1
                or not isinstance(marker.get('id'), str) or not re.fullmatch(r'[0-9a-f]{32}', marker['id'])):
            raise ValueError('Invalid backup volume marker')
    except (OSError, ValueError) as exc:
        raise SyncError(f'Local backup volume unavailable or uninitialized at {path}; '
                        'mount it and use init-backup for a new volume') from exc
    return marker['id'], stat.st_dev, stat.st_ino


def local_destinations(settings, identifiers=None):
    configured = settings['destinations']
    keys = dict.fromkeys(identifiers or (name for name, spec in configured.items() if spec['provider'] == 'local'))
    if not keys:
        raise SyncError('Configure a local destination first')
    if any(key not in configured or configured[key]['provider'] != 'local' for key in keys):
        raise SyncError('Select existing local destination IDs')
    return {key: configured[key] for key in keys}


def initialize_backup(settings, identifiers=None):
    failed = False
    for name, spec in local_destinations(settings, identifiers).items():
        path = spec['path']
        try:
            if not path.is_dir():
                raise SyncError(f'Local backup directory must already exist on the intended volume: {path}')
            with FileLock(str(path / '.gitmirror-backup.lock'), timeout=0):
                if not (path / BACKUP_MARKER).exists():
                    store_json(path / BACKUP_MARKER, {'version': 1, 'id': uuid.uuid4().hex})
                backup_volume(path)
            print(f'{name}: Local backup volume ready at {path}')
        except (OSError, SyncError, Timeout) as exc:
            print(f'{name}: FAILED: {exc}')
            failed = True
    return 2 if failed else 0


def backup_refs(repo):
    out = git('for-each-ref', '--format=%(refname)%09%(objectname)', repo=repo).stdout
    return dict(line.split('\t', 1) for line in out.split('\n') if line)


def current_backup(directory):
    pointer = directory / 'CURRENT'
    try:
        manifest = json.loads(pointer.read_text(encoding='utf-8'))
        generation = manifest.get('generation') if isinstance(manifest, dict) else None
        if not isinstance(generation, str) or not BACKUP_GENERATION.fullmatch(generation):
            raise ValueError('Invalid generation name')
        repo = directory / 'generations' / generation
        if not repo.is_dir() or repo.is_symlink():
            raise ValueError('Backup generation missing')
        return repo
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        raise SyncError(f'Cannot read the current backup at {directory}') from exc


def backup_manifest(repo):
    try:
        manifest = json.loads((repo / BACKUP_MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SyncError(f'Invalid local backup manifest at {repo}') from exc
    if (not isinstance(manifest, dict) or manifest.get('version') != 1
            or not isinstance(manifest.get('name'), str) or not isinstance(manifest.get('refs'), dict)):
        raise SyncError(f'Invalid local backup manifest at {repo}')
    return manifest


def linked_backup_path(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def verify_backup_payload_paths(repo):
    def fail(error):
        raise error

    for base in (repo / 'objects', repo / 'lfs'):
        if linked_backup_path(base):
            raise SyncError(f'Linked path in the local backup: {base}')
        for root, dirs, files in os.walk(base, onerror=fail) if base.exists() else ():
            for name in dirs + files:
                path = Path(root) / name
                if linked_backup_path(path):
                    raise SyncError(f'Linked path in the local backup: {path}')


def verify_local_generation(repo, manifest=None, *, objects=None):
    if manifest is None:
        manifest = backup_manifest(repo)
    verify_backup_payload_paths(repo)
    if ((repo / 'objects' / 'info' / 'alternates').exists()
            or any((repo / 'objects' / 'pack').glob('*.promisor'))):
        raise SyncError('The local backup depends on an external Git object store')
    refs = backup_refs(repo)
    if not refs or refs != manifest['refs']:
        raise SyncError(f'Git refs do not match the local backup manifest at {repo}')
    head = f'refs/heads/{manifest.get("default_branch")}'
    if head not in refs or git('symbolic-ref', 'HEAD', repo=repo).stdout.strip() != head:
        raise SyncError(f'Default branch does not match the local backup manifest at {repo}')
    git('fsck', '--full', '--no-dangling', repo=repo, timeout=1800,
        env={**SSH_ENV, 'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': 'file'})
    if objects is None:
        objects = lfs_objects(repo, refs, scratch=tempfile.gettempdir())
    verify_lfs_objects(repo / 'lfs' / 'objects', objects)
    return manifest


def fsync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def flush_snapshot(repo):
    for root, _, files in os.walk(repo, topdown=False):
        for name in files:
            path = Path(root) / name
            if os.name != 'nt':
                with path.open('rb') as file:
                    os.fsync(file.fileno())
                continue
            # Windows needs a writable handle, including for Git's read-only object files.
            mode = path.stat().st_mode
            read_only = not mode & stat.S_IWRITE
            if read_only:
                path.chmod(mode | stat.S_IWRITE)
            try:
                with path.open('r+b') as file:
                    os.fsync(file.fileno())
            finally:
                if read_only:
                    path.chmod(mode)
        fsync_directory(root)


def clone_file(source, target):
    if not sys.platform.startswith('linux'):
        return False
    import fcntl

    try:
        with open(source, 'rb') as original, open(target, 'xb') as cloned:
            # Linux _IOW(0x94, 9, int), as defined by <linux/fs.h>.
            fcntl.ioctl(cloned.fileno(), getattr(fcntl, 'FICLONE', 0x40049409), original.fileno())
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV, errno.ENOSYS, errno.EBADF):
            raise
        Path(target).unlink(missing_ok=True)
        return False
    return True


class BackupCopy:
    """Use independent inodes with copy-on-write where supported."""

    def __init__(self):
        self.unsupported = set()
        self.cloned = self.copied = 0

    def __call__(self, source, target):
        source, target = Path(source), Path(target)
        if source.is_symlink() or not source.is_file():
            raise SyncError(f'Backup payload must be a regular file: {source}')
        devices = source.stat().st_dev, target.parent.stat().st_dev
        if devices not in self.unsupported and clone_file(source, target):
            self.cloned += 1
        else:
            self.unsupported.add(devices)
            shutil.copyfile(source, target)
            self.copied += 1
        shutil.copymode(source, target)
        return target


def init_bare_backup(working, staged):
    fmt = git('rev-parse', '--show-object-format', repo=working).stdout.strip()
    git('init', '--bare', '--template=', f'--object-format={fmt}', str(staged))
    git('config', 'lfs.storage', 'lfs', repo=staged)
    if os.name == 'nt':
        git('config', 'core.longpaths', 'true', repo=staged)


def copy_lfs_payload(staged, objects, copy, *media_dirs):
    for oid in objects:
        relative = Path(oid[:2], oid[2:4], oid)
        source = next((directory / relative for directory in media_dirs
                       if directory is not None and (directory / relative).is_file()), None)
        if source is None:
            raise SyncError(f'Missing LFS object {oid} for the verified backup')
        target = staged / 'lfs' / 'objects' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy(source, target)


def preserve_local_backup(path, volume, scope, source, record, working, refs, objects, *,
                          identifier='local', retention=None):
    """Publish a verified, self-contained generation with a single pointer replacement."""
    def confirm_volume():
        if backup_volume(path) != volume:
            raise SyncError('Local backup volume changed during sync')

    directory = path / scope / str(record['id'])
    branch = record['default_branch']
    try:
        confirm_volume()
        with FileLock(str(path / '.gitmirror-backup.lock'), timeout=0):
            confirm_volume()
            previous = current_backup(directory)
            archived = {}
            if previous:
                old = verify_local_generation(previous)
                if old.get('source_id') != record['id']:
                    raise SyncError('Existing local backup belongs to another source repository')
                before = local_refs(previous)
                if before == refs and old['default_branch'] == branch and old['name'] == record['name']:
                    source.confirm_source(record)
                    confirm_volume()
                    if retention and retention['automatic']:
                        cleanup_repository(directory, retention, apply=True, confirm_volume=confirm_volume)
                    print(f'  {identifier}: verified current backup {previous}')
                    return previous
                archived = {key: oid for key, oid in old['refs'].items() if key.startswith('refs/archive/')}
                archived.update({f'refs/archive/{previous.stem}/{ref.removeprefix("refs/")}': oid
                                 for ref, oid in before.items()})

            generations = directory / 'generations'
            for location in (directory.parent, directory, generations):
                location.mkdir(mode=0o700, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.partial-', dir=generations) as scratch:
                staged = Path(scratch) / 'mirror.git'
                init_bare_backup(working, staged)
                copy = BackupCopy()
                if previous:
                    # Keep the Git pack/loose objects for incremental negotiation, without
                    # inheriting repository config, locks, hooks, or LFS cache leftovers.
                    shutil.copytree(previous / 'objects', staged / 'objects', dirs_exist_ok=True,
                                    ignore=shutil.ignore_patterns('info'), copy_function=copy)
                    updates = ''.join(f'create {ref} {oid}\n' for ref, oid in archived.items())
                    if updates:
                        git('update-ref', '--stdin', repo=staged, input_text=updates)
                git('fetch', '--atomic', '--prune', '--no-tags', '--no-write-fetch-head', '--refmap=',
                    working.as_uri(), '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*',
                    repo=staged, timeout=1800, env={**SSH_ENV, 'GIT_ALLOW_PROTOCOL': 'file'})
                git('symbolic-ref', 'HEAD', f'refs/heads/{branch}', repo=staged)
                expected = {**refs, **archived}
                required = lfs_objects(staged, expected)
                copy_lfs_payload(staged, required, copy, previous / 'lfs' / 'objects' if previous else None,
                                 lfs_media_dir(working) if objects else None)
                manifest = {
                    'version': 1, 'source_id': record['id'], 'name': record['name'],
                    'default_branch': branch, 'refs': expected, 'created_ns': time.time_ns(),
                }
                store_json(staged / BACKUP_MANIFEST, manifest)
                verify_local_generation(staged, manifest, objects=required)
                flush_snapshot(staged)
                source.confirm_source(record)
                confirm_volume()
                published = generations / f'{RUN_ID}.git'
                staged.rename(published)
                for location in (generations, directory, directory.parent, path):
                    fsync_directory(location)
                confirm_volume()
                store_json(directory / 'CURRENT', {'generation': published.name})
            confirm_volume()
            print(f'  {identifier}: verified and published {published} ({copy.cloned} reflinks, {copy.copied} file copies)')
            if retention and retention['automatic']:
                cleanup_repository(directory, retention, apply=True, confirm_volume=confirm_volume)
            return published
    except (OSError, Timeout) as exc:
        raise SyncError(f'Could not complete local backup at {path}: {exc}') from exc


def backup_directories(path, scope):
    parent = path / scope
    if parent.is_symlink():
        raise SyncError(f'Backup scope is an unexpected symbolic link: {parent}')
    return sorted(entry for entry in parent.glob('*')
                  if entry.name.isascii() and entry.name.isdecimal() and entry.is_dir() and not entry.is_symlink())


def backup_generations(directory):
    parent = directory / 'generations'
    if parent.is_symlink():
        raise SyncError(f'Generation directory is an unexpected symbolic link: {parent}')
    return sorted(repo for repo in parent.glob('*.git')
                  if BACKUP_GENERATION.fullmatch(repo.name) and repo.is_dir() and not repo.is_symlink())


def backup_matches(directory, manifest, only):
    return not only or only == directory.name or only.casefold() == manifest['name'].casefold()


def verify_local_backups(settings, only, identifiers=None, *, activity=None):
    confirmed = 0
    failed = False
    for name, spec in local_destinations(settings, identifiers).items():
        path = spec['path']
        try:
            volume = backup_volume(path)
            for entry in backup_directories(path, scope_id(settings['source'])):
                repo = current_backup(entry)
                if repo is None:
                    continue
                manifest = backup_manifest(repo)
                if not backup_matches(entry, manifest, only):
                    continue
                try:
                    verify_local_generation(repo, manifest)
                except (OSError, SyncError) as exc:
                    if activity:
                        activity.backup(name, entry.name, repo, 'failed', str(exc))
                    raise
                if activity:
                    activity.backup(name, entry.name, repo, 'verified')
                print(f'{name}: Verified {manifest["name"]} at {repo}')
                confirmed += 1
            if backup_volume(path) != volume:
                raise SyncError('Local backup volume changed during verification')
        except (OSError, SyncError) as exc:
            print(f'{name}: FAILED: {exc}')
            failed = True
    if not confirmed:
        print('No matching local backup could be verified on the configured volumes')
    return 2 if failed or not confirmed else 0


def verify_backup_selection(settings, identifiers, only, generation, *, activity=None):
    name, repo, manifest = select_restore_backup(settings, identifiers, only, generation, listing=False)
    try:
        verify_local_generation(repo, manifest)
    except (SyncError, OSError) as exc:
        if activity:
            activity.backup(name, repo.parent.parent.name, repo, 'failed', str(exc))
        raise
    if activity:
        activity.backup(name, repo.parent.parent.name, repo, 'verified')
    print(f'{name}: Verified {manifest["name"]} at {repo}')
    return 0


def generation_created(repo, manifest):
    value = manifest.get('created_ns')
    if type(value) is int and value > 0:
        return value
    # v2 backups written before timestamps were added have UTC names.
    timestamp = dt.datetime.strptime(repo.name[:16], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
    return int(timestamp.timestamp()) * 1_000_000_000


def cleanup_repository(directory, policy, *, apply, confirm_volume):
    current = current_backup(directory)
    if current is None:
        return
    current_manifest = verify_local_generation(current)
    current_time = generation_created(current, current_manifest)
    older = []
    for repo in backup_generations(directory):
        if repo == current:
            continue
        try:
            manifest = backup_manifest(repo)
            created = generation_created(repo, manifest)
        except (SyncError, ValueError) as exc:
            print(f'Keeping unreadable generation {repo}: {exc}')
            continue
        if created < current_time:
            older.append((created, repo, manifest))
    older.sort(key=lambda item: item[0], reverse=True)

    days = policy['days']
    cutoff = time.time_ns() - days * 86_400 * 1_000_000_000 if days is not None else None
    for created, repo, manifest in older[policy['keep'] - 1:]:
        if cutoff is not None and created >= cutoff:
            continue
        covered = {}
        for ref, oid in manifest['refs'].items():
            if not isinstance(ref, str):
                raise SyncError(f'Invalid reference in generation {repo}')
            saved_ref = ref if ref.startswith('refs/archive/') else f'refs/archive/{repo.stem}/{ref.removeprefix("refs/")}'
            covered[saved_ref] = oid
        if (manifest.get('source_id') != current_manifest.get('source_id')
                or not covered or any(current_manifest['refs'].get(ref) != oid for ref, oid in covered.items())):
            print(f'Keeping {repo}: the current backup does not cover its complete history')
            continue
        if backup_refs(repo) != manifest['refs']:
            print(f'Keeping {repo}: its refs differ from its manifest')
            continue
        if apply:
            confirm_volume()
            if current_backup(directory) != current or repo.resolve() != repo:
                raise SyncError('Backup location or CURRENT changed during cleanup')
            print(f'Removing generation {repo}; current backup is verified and covers its history')
            shutil.rmtree(repo, onerror=unlock_backup_file)
            fsync_directory(repo.parent)
        else:
            print(f'Would remove generation {repo}; current backup is verified and covers its history')


def unlock_backup_file(function, path, error_info):
    error = error_info[1]
    if os.name != 'nt' or not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def report_backup_usage(name, path):
    total = sum(file.lstat().st_size for file in path.rglob('*') if file.is_file() and not file.is_symlink())
    print(f'{name}: {total:,} logical bytes stored; {shutil.disk_usage(path).free:,} volume bytes free')


def cleanup_backups(settings, identifiers, *, apply):
    failed = False
    for name, spec in local_destinations(settings, identifiers).items():
        path = spec['path']
        try:
            volume = backup_volume(path)

            def confirm_volume():
                if backup_volume(path) != volume:
                    raise SyncError('Local backup volume changed during cleanup')

            policy = spec['retention']
            if not policy:
                print(f'{name}: retention disabled')
            else:
                lock = FileLock(str(path / '.gitmirror-backup.lock'), timeout=0) if apply else nullcontext()
                with lock:
                    confirm_volume()
                    for directory in backup_directories(path, scope_id(settings['source'])):
                        cleanup_repository(directory, policy, apply=apply, confirm_volume=confirm_volume)
            report_backup_usage(name, path)
        except (OSError, SyncError, Timeout) as exc:
            print(f'{name}: cleanup FAILED: {exc}')
            failed = True
    return 2 if failed else 0


def select_restore_backup(settings, identifiers, only, generation, *, listing):
    selected = []
    failed = False
    for name, spec in local_destinations(settings, identifiers).items():
        path = spec['path']
        try:
            backup_volume(path)
            for directory in backup_directories(path, scope_id(settings['source'])):
                if only and only.isascii() and only.isdecimal() and only != directory.name:
                    continue
                try:
                    current = current_backup(directory)
                except SyncError:
                    current = None
                    print(f'{name}: cannot read CURRENT for repository {directory.name}; use an explicit generation')
                for repo in backup_generations(directory):
                    if not listing and (repo != current if generation == 'current' else repo.stem != generation):
                        continue
                    try:
                        manifest = backup_manifest(repo)
                    except SyncError as exc:
                        print(f'{name}: {exc}')
                        continue
                    if not backup_matches(directory, manifest, only):
                        continue
                    if listing:
                        suffix = ' (current)' if repo == current else ''
                        print(f'{name}: {manifest["name"]} [{directory.name}]  {repo.stem}{suffix}')
                    else:
                        selected.append((name, repo, manifest))
        except (OSError, SyncError) as exc:
            print(f'{name}: {exc}')
            failed = True
    if listing:
        return 2 if failed else 0
    if len(selected) != 1:
        raise SyncError('Select one available repository and generation; use restore --list and specify --destination when needed')
    return selected[0]


def restore_backup(settings, identifiers, only, generation, output_path, include_archive, *, activity=None):
    name, repo, manifest = select_restore_backup(settings, identifiers, only, generation, listing=False)
    output = Path(output_path).expanduser().absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise SyncError('Restore requires a new output directory under an existing parent')
    if any(output.resolve().is_relative_to(spec['path']) for spec in local_destinations(settings).values()):
        raise SyncError('Restore output must be outside all configured backup volumes')

    try:
        verify_local_generation(repo, manifest)
    except (SyncError, OSError) as exc:
        if activity:
            activity.backup(name, repo.parent.parent.name, repo, 'failed', str(exc))
        raise
    expected = {ref: oid for ref, oid in manifest['refs'].items()
                if include_archive or not ref.startswith('refs/archive/')}
    with tempfile.TemporaryDirectory(prefix='.gitmirror-restore-', dir=output.parent) as scratch:
        staged = Path(scratch) / 'mirror.git'
        init_bare_backup(repo, staged)
        refspecs = ['refs/heads/*:refs/heads/*', 'refs/tags/*:refs/tags/*']
        if include_archive:
            refspecs.append('refs/archive/*:refs/archive/*')
        git('fetch', '--atomic', '--no-tags', '--no-write-fetch-head', '--refmap=',
            repo.as_uri(), *refspecs, repo=staged, timeout=1800)
        git('symbolic-ref', 'HEAD', f'refs/heads/{manifest["default_branch"]}', repo=staged)
        required = lfs_objects(staged, expected)
        copy_lfs_payload(staged, required, BackupCopy(), repo / 'lfs' / 'objects')
        verify_local_generation(staged, {**manifest, 'refs': expected}, objects=required)
        flush_snapshot(staged)
        if os.name == 'nt':
            staged.rename(output)
        else:
            output.mkdir(mode=0o700)  # Fail if another process occupied the path.
            try:
                staged.replace(output)
            except OSError:
                try:
                    output.rmdir()
                except OSError:
                    pass
                raise
        fsync_directory(output.parent)
    print(f'{name}: Restored and verified {manifest["name"]} from {repo.stem} at {output}')
    if activity:
        activity.backup(name, repo.parent.parent.name, repo, 'verified')
    return 0


# Synchronization

def describe_destination_plan(platform, destination, src, source_default, private, headers, *, identifier=None):
    label = identifier or platform
    if destination is None:
        print(f'  {label}: would create a private project, then mirror {len(src)} refs')
        if not private:
            print(f'    {label}: WOULD publish after Git and LFS verification')
        return
    old = remote_refs(destination.url)
    add, change, delete = count_changes(src, old)
    print(f'  {label}: visibility={destination.visibility}, add={add}, rewrite/update={change}, delete={delete} refs')
    if destination.default_branch != source_default:
        print(f'    {label}: WOULD verify {source_default}, then set it as default before deleting old branches')
    target = desired_visibility(private)
    if destination.visibility != target:
        when = 'before Git sync' if private else 'after Git and LFS verification'
        print(f'    {label}: WOULD change visibility {destination.visibility} -> {target} ({when})')
    if platform == 'gitlab' and src != old:
        for action, branch, _ in plan_gl_permissions(destination.id, source_default, src, old, headers):
            operation = 'temporarily allow force push' if action == 'force' else 'unprotect for deletion'
            print(f'    GitLab WOULD {operation}: {branch}')


def sync_destination(record, source, repo, provider, destination, src, source_lfs, full_lfs, *, identifier=None):
    name, source_default = record['name'], record['default_branch']
    platform, headers = provider.kind, provider.headers
    remote = identifier or platform
    url = destination.url
    old = remote_refs(url)
    refs_changed = src != old
    if refs_changed and old:
        backup_destination_refs(repo, remote, url, old)
    if refs_changed or source_lfs:
        ensure_remote(repo, remote, url)

    def confirm_destination():
        if provider.read_destination(name) != destination:
            raise SyncError(f'{platform} destination state changed during sync')

    def confirm_transfer():
        # Check source last so a provider read or Git ref preflight cannot delay this check.
        confirm_destination()
        source.confirm_source(record)

    def upload_lfs():
        confirm_transfer()
        git('-c', 'lfs.allowincompletepush=false', 'lfs', 'push', '--all',
            remote, *src, repo=repo, timeout=1800, before_retry=confirm_transfer)

    def push_refs(desired, previous):
        if desired == previous:
            return
        removes_branch = any(ref.startswith('refs/heads/') for ref in previous.keys() - desired.keys())

        def before_push():
            if removes_branch:
                verify_remote_head(url, source_default)
            confirm_transfer()

        if platform != 'gitlab':
            push_snapshot(repo, remote, desired, previous, url=url, before_push=before_push)
            return
        project_id = destination.id
        try:
            confirm_destination()
            prepare_gl_permissions(project_id, destination.default_branch, desired, previous, headers)
            push_snapshot(repo, remote, desired, previous, url=url, before_push=before_push)
        finally:
            restore_gl_permissions(project_id, headers)

    if source_lfs and refs_changed:
        upload_lfs()

    if destination.default_branch != source_default or not any(ref.startswith('refs/heads/') for ref in old):
        # Seed only the target branch. Retain every existing branch and tag until the switch is confirmed.
        default_ref = f'refs/heads/{source_default}'
        staged = {**old, default_ref: src[default_ref]}
        push_refs(staged, old)
        if remote_refs(url) != staged:
            raise SyncError('Destination refs changed before the default branch switch')
        current = provider.read_destination(name)
        if current not in (destination, replace(destination, default_branch=source_default)):
            raise SyncError('Destination state changed before the default branch switch')
        destination = current
        if destination.default_branch != source_default:
            source.confirm_source(record)
            destination = provider.update_destination(name, destination, default_branch=source_default)
        verify_remote_head(url, source_default)
        old = staged

    push_refs(src, old)
    try:
        verify_destination_lfs(repo, remote, src, source_lfs, full=full_lfs)
    except SyncError:
        if full_lfs or refs_changed or not source_lfs:
            raise
        # Equal refs can still reference missing LFS objects.
        upload_lfs()
        verify_destination_lfs(repo, remote, src, source_lfs)
    if remote_refs(url) != src:
        raise SyncError('Destination refs do not match the source snapshot')

    verify_remote_head(url, source_default)
    confirm_transfer()
    if destination.visibility != desired_visibility(record['private']):
        # Private destinations are only published after all ref, default and LFS checks.
        provider.update_destination(name, destination, visibility='public')
    print(f'  {remote}: SYNCED (Git refs, default branch, Git LFS and visibility verified)')


def sync_repository(record, source, providers, volumes, settings, apply, *, full_lfs=False, activity=None):
    name = record['name']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise SyncError(f'Unsupported repository name: {name}')
    source_url = record.get('ssh_url')
    if not isinstance(source_url, str) or not source_url or record['id'] <= 0:
        raise SyncError(f'{source.label} repository ID or SSH URL invalid; refusing destination updates')
    priv = record['private']
    source_default = record.get('default_branch')
    gh = remote_refs(source_url)
    default_ref = f'refs/heads/{source_default}'
    if not isinstance(source_default, str) or not source_default or default_ref not in gh:
        raise SyncError(f'{source.label} default branch is unconfirmed in advertised refs; refusing destination updates')
    source.confirm_source(record)
    heads = sum(ref.startswith('refs/heads/') for ref in gh)
    print(f'  source: {heads} branches, {len(gh) - heads} tags; privacy={desired_visibility(priv)}')

    destinations = {}
    resolved_urls = set()
    successes = failures = 0
    active = len(providers) + len(volumes)

    def observe(identifier, state, detail=None):
        if activity:
            activity.result(name, identifier, state, detail)

    if not apply:
        for identifier, (path, _) in volumes.items():
            print(f'  {identifier}: would verify and preserve {len(gh)} refs at {path}')
            successes += 1
            observe(identifier, 'checked')
    for identifier, provider in providers.items():
        try:
            destination = provider.read_destination(name, create=apply)
            reject_source_as_destination(source, record, provider, destination)
            if destination is not None:
                address = git_address(destination.url)
                if address in resolved_urls:
                    raise SyncError('Another selected destination resolves to the same repository')
                resolved_urls.add(address)
            if not apply:
                with protection_scope(settings, settings['destinations'][identifier]):
                    describe_destination_plan(provider.kind, destination, gh, source_default, priv,
                                              provider.headers, identifier=identifier)
                successes += 1
                observe(identifier, 'checked')
                continue
            if priv and destination.visibility != 'private':
                source.confirm_source(record)
                destination = provider.update_destination(name, destination, visibility='private')
            destinations[identifier] = destination
        except SourceStateError as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            observe(identifier, 'blocked', str(exc))
            return successes, active - successes
        except (SyncError, OSError) as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            failures += 1
            observe(identifier, 'blocked', str(exc))
    if not apply or (not destinations and not volumes):
        return successes, failures

    scope = scope_id(settings['source'])
    repo = ROOT / 'work' / scope / f'{record["id"]}.git' if settings['version'] == 2 else ROOT / f'{name}.git'
    repo.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not repo.exists():
        git('init', '--bare', str(repo))
        os.chmod(repo, 0o700)
    if git('rev-parse', '--is-bare-repository', repo=repo).stdout.strip() != 'true':
        raise SyncError(f'Repository path is not a bare Git repo: {repo}')
    ensure_remote(repo, 'origin', source_url)
    # Only fetch heads/tags: never prune local recovery refs under refs/archive/.
    previous = local_refs(repo)
    previous_lfs = lfs_objects(repo, previous)
    fetch_backup_lfs(repo, 'origin', previous, previous_lfs)
    archive_source_old_tips(repo, previous, source.kind)
    git('fetch', '--atomic', '--prune', '--no-tags', '--refmap=', 'origin',
        '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*', repo=repo, timeout=1200)
    src = local_refs(repo)
    if default_ref not in src:
        raise SourceStateError(f'{source.label} default branch is absent from the fetched snapshot; refusing all pushes')

    source_lfs = previous_lfs if src == previous else lfs_objects(repo, src)
    if src != previous:
        fetch_backup_lfs(repo, 'origin', src, source_lfs)
    if full_lfs:
        verify_archived_lfs(repo)

    for identifier, (path, volume) in volumes.items():
        try:
            retention = settings['destinations'][identifier]['retention']
            if os.getenv('GITMIRROR_WEB_JOB'):
                retention = None
            generation = preserve_local_backup(path, volume, scope, source, record, repo, src, source_lfs,
                                                identifier=identifier, retention=retention)
            if activity and generation is not None:
                activity.backup(identifier, str(record['id']), generation, 'verified')
            successes += 1
            observe(identifier, 'succeeded')
        except SourceStateError as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            observe(identifier, 'blocked', str(exc))
            return successes, active - successes
        except (SyncError, OSError) as exc:
            print(f'  {identifier}: FAILED: {exc}')
            failures += 1
            observe(identifier, 'failed', str(exc))
    for identifier, destination in destinations.items():
        try:
            with protection_scope(settings, settings['destinations'][identifier]):
                sync_destination(record, source, repo, providers[identifier], destination, src, source_lfs,
                                 full_lfs, identifier=identifier)
            successes += 1
            observe(identifier, 'succeeded')
        except SourceStateError as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            observe(identifier, 'blocked', str(exc))
            return successes, active - successes
        except (SyncError, OSError) as exc:
            print(f'  {identifier}: FAILED without deleting project: {exc}')
            failures += 1
            observe(identifier, 'failed', str(exc))
    return successes, failures


def selected_platforms(settings, platform, identifiers=None):
    configured = settings['destinations']
    if identifiers:
        keys = list(dict.fromkeys(identifiers))
        if any(key not in configured for key in keys):
            raise SyncError('Select configured destination IDs with --destination')
        return keys
    if platform in ('both', 'all'):
        return list(configured)
    matches = [key for key, spec in configured.items() if spec['provider'] == platform]
    if not matches:
        raise SyncError(f'{platform} is not a configured destination')
    return matches


def run(settings, *, apply=False, only=None, platform='all', full_lfs=False, identifiers=None, activity=None):
    configure_runtime(settings, apply=apply)
    for executable in ('git', 'ssh', 'git-lfs'):
        if shutil.which(executable) is None:
            raise SyncError(f'Missing executable: {executable}')
    platforms = selected_platforms(settings, platform, identifiers)
    source = Provider.from_config(settings['source'], refresh=apply)
    if source.kind == 'gitea' or 'oauth_id' in settings['source']:
        source = source.authenticate()
    providers = {}
    volumes = {}
    mounted = set()
    successes = failures = 0
    for target in platforms:
        spec = settings['destinations'][target]
        try:
            if spec['provider'] == 'local':
                path = spec['path']
                volume = backup_volume(path)
                if volume[0] in mounted:
                    raise SyncError('Another selected destination uses the same backup volume marker')
                mounted.add(volume[0])
                volumes[target] = (path, volume)
                continue
            provider = Provider.from_config(spec, refresh=apply).authenticate()
            if apply and provider.kind == 'gitlab':
                with protection_scope(settings, spec):
                    recover_all_gl_permissions(provider.headers)
            providers[target] = provider
        except (SyncError, OSError) as exc:
            print(f'{target}: BLOCKED: {exc}')
            failures += 1
            if activity:
                activity.result('', target, 'blocked', str(exc))
    active = len(providers) + len(volumes)
    if not active:
        print(f'SUMMARY: repositories=0, successful/checked destinations=0, failed={failures}')
        return 1

    repos = source.discover()
    if activity:
        activity.inventory(repos)
    if settings.get('repositories'):
        include = {name.casefold() for name in settings['repositories']}
        repos = [repo for repo in repos if repo['name'].casefold() in include]
        if not repos:
            raise SyncError('None of the configured repository names were found at the source')
    print(f'{source.label}: {source.owner} -> {", ".join(platforms)}')
    print(f'Mode: {"APPLY" if apply else "DRY RUN"} | Discovered: {len(repos)} | Run: {RUN_ID}')
    if only:
        repos = [repo for repo in repos if repo['name'].casefold() == only.casefold()]
        if not repos:
            raise SyncError(f'{only} not found among accessible {source.label} repositories; no changes made')
    for n, repo in enumerate(repos, 1):
        print(f'\n[{n}/{len(repos)}] {repo["name"]}')
        if activity:
            activity.progress(repo['name'], n, len(repos))
        error = 'Stopped before processing this destination'
        try:
            ok, bad = sync_repository(repo, source, providers, volumes, settings, apply,
                                      full_lfs=full_lfs, activity=activity)
            successes += ok
            failures += bad
        except (SyncError, OSError) as exc:
            print(f'  FAILED safely: {exc}')
            failures += active
            error = str(exc)
        if activity:
            activity.unreported(repo['name'], platforms, error)
    print(f'\nSUMMARY: repositories={len(repos)}, successful/checked destinations={successes}, '
          f'failed={failures}, mode={"apply" if apply else "dry-run"}')
    print('Missing source repositories are never deleted from destinations.')
    return 0 if failures == 0 else 1


MANAGED_SYSTEM_STATE = Path('/var/lib/gitmirror-managed/work')
MANAGED_SYSTEM_ETC = Path('/etc/gitmirror-managed')


def execute_sync(settings, *, apply=False, only=None, platform='all', full_lfs=False, identifiers=None,
                 activity=None):
    if not apply:
        return run(settings, only=only, platform=platform, identifiers=identifiers, activity=activity)
    storage = settings['storage']['path']
    if managed_writes_blocked(settings):
        raise SyncError('Managed synchronization is paused for maintenance')
    storage.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(storage / '.gitmirror.lock'), timeout=0):
            with managed_sync_lock(settings):
                if managed_writes_blocked(settings):
                    raise SyncError('Managed synchronization is paused for maintenance')
                return run(settings, apply=True, only=only, platform=platform, full_lfs=full_lfs,
                           identifiers=identifiers, activity=activity)
    except Timeout:
        print('Another GitMirror process is running; no changes made.', file=sys.stderr)
        return 2


def managed_writes_blocked(settings=None):
    if settings and settings['storage']['path'] == MANAGED_SYSTEM_STATE:
        if any((MANAGED_SYSTEM_ETC / marker).exists() for marker in ('maintenance-pending', 'migration-pending')):
            return True
    guard = os.getenv('GITMIRROR_MAINTENANCE_GUARD')
    return bool(guard and Path(guard).exists())


@contextmanager
def managed_sync_lock(settings):
    if settings['storage']['path'] != MANAGED_SYSTEM_STATE:
        yield
        return
    if not sys.platform.startswith('linux'):
        raise SyncError('The managed synchronization lock requires Linux')
    import fcntl

    path = MANAGED_SYSTEM_ETC / 'sync.lock'
    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
            raise SyncError('Managed synchronization lock directory must be protected and root-owned')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise SyncError('Managed synchronization lock is unavailable; repair the installation') from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
            raise SyncError('Managed synchronization lock must be a protected root-owned file')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Timeout(str(path)) from exc
        except OSError as exc:
            raise SyncError('Could not acquire the managed synchronization lock') from exc
        try:
            if not os.path.samestat(info, path.lstat()):
                raise SyncError('Managed synchronization lock changed while opening it')
        except OSError as exc:
            raise SyncError('Managed synchronization lock disappeared while opening it') from exc
        yield
    finally:
        os.close(descriptor)


# Read-only diagnostics


def doctor(config, *, network=False, ssh=False, only=None, platform='all', identifiers=None):
    results = []

    def report(status, item, detail):
        results.append(status)
        print(f'{status} {item} - {detail}')

    report('OK' if sys.version_info >= (3, 11) else 'ERROR', 'Python', sys.version.split()[0])
    tool_env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_TERMINAL_PROMPT': '0'}
    for label, arguments in (('Git', ('git', '--version')), ('Git LFS', ('git', 'lfs', 'version')),
                             ('SSH', ('ssh', '-V'))):
        try:
            process = cmd(arguments, check=False, retry=False, timeout=10, env=tool_env)
            version = (process.stdout or process.stderr).strip().splitlines()
            if process.returncode or not version:
                raise SyncError('Version unavailable')
            report('OK', label, version[0])
        except SyncError:
            report('ERROR', label, 'version probe failed; check that the tool is installed and on PATH')

    try:
        settings = load_config(config)
    except (ConfigError, OSError, ValueError):
        report('ERROR', 'configuration', 'check that the YAML file exists and follows config.example.yml')
        return 2
    report('OK', 'configuration', 'YAML is valid')
    storage = settings['storage']['path']
    if managed_writes_blocked(settings):
        report('WARNING', 'managed sync', 'applied runs are paused; check installer status')
    try:
        ancestor = next(path for path in (storage, *storage.parents) if path.exists())
        if not ancestor.is_dir():
            raise OSError('Storage or parent is not a directory')
        if not os.access(ancestor, os.R_OK | os.W_OK | os.X_OK):
            report('ERROR', 'storage access', 'the directory or nearest existing parent failed the access check')
        elif storage != ancestor:
            report('WARNING', 'storage access', 'nearest parent passes; the backup directory does not exist yet')
        elif os.name == 'nt':
            report('WARNING', 'storage access',
                   'the existing path passes a preliminary check; effective Windows ACLs remain unverified')
        else:
            report('OK', 'storage access', 'directory passes the permission check')
        free = shutil.disk_usage(ancestor).free
        report('OK' if free else 'ERROR', 'storage space', f'{free:,} bytes free on the backup volume')
    except (OSError, StopIteration):
        report('ERROR', 'storage', 'cannot inspect the backup directory or volume')

    platforms = selected_platforms(settings, platform, identifiers)
    for target in platforms:
        spec = settings['destinations'][target]
        if spec['provider'] != 'local':
            continue
        label = 'local backup volume' if target == 'local' else f'{target} backup volume'
        try:
            path = spec['path']
            backup_volume(path)
            free = shutil.disk_usage(path).free
            report('OK' if free else 'ERROR', label, f'{free:,} bytes free')
        except (SyncError, OSError):
            report('ERROR', label, 'mount the configured volume and check its initialization')
    if network or ssh:
        configure_runtime(settings, apply=False)
        doctor_network(settings, platforms, ssh, only, report)
    else:
        report('WARNING', 'provider and SSH checks', 'skipped; pass --network or --ssh to opt in')
    return 2 if 'ERROR' in results else 0


def doctor_network(settings, platforms, ssh, only, report):
    def connect(spec, name):
        try:
            provider = Provider.from_config(spec, refresh=False)
        except (SyncError, ValueError):
            report('ERROR', f'{name} credentials', 'configured token is missing or unreadable')
            return None
        try:
            provider = provider.authenticate()
        except (SyncError, ValueError):
            report('ERROR', f'{name} API', 'identity request failed; check credentials, owner and network access')
            return None
        report('OK', f'{name} API', 'authenticated identity request succeeded')
        return provider

    source_kind = settings['source']['provider']
    source = connect(settings['source'], source_kind)
    providers = [(target, provider) for target in platforms
                 if settings['destinations'][target]['provider'] != 'local'
                 if (provider := connect(settings['destinations'][target], target)) is not None]
    if not ssh:
        return
    try:
        if source is None:
            raise SyncError('Source authentication failed')
        record = next(repo for repo in source.discover()
                      if only is None or repo['name'].casefold() == only.casefold())
    except (SyncError, StopIteration, ValueError):
        report('ERROR', 'SSH', 'cannot select an accessible source repository')
        return

    for index, (name, provider) in enumerate([(source_kind, source), *providers]):
        try:
            if index == 0:
                url = record['ssh_url']
            else:
                destination = provider.read_destination(record['name'])
                if destination is None:
                    report('WARNING', f'{name} SSH', 'matching destination is absent; read access was not checked')
                    continue
                url = destination.url
            if not re.match(r'^(ssh://|[^/@:\s]+@[^/:\s]+:)', url):
                raise SyncError('SSH URL unavailable')
            git('ls-remote', '--heads', '--tags', '--', url, timeout=30, retry=False,
                env={**SSH_ENV, 'GIT_ALLOW_PROTOCOL': 'ssh'})
            report('OK', f'{name} SSH', 'authenticated Git read succeeded')
        except (SyncError, ValueError):
            report('ERROR', f'{name} SSH', 'Git read failed; check trusted host keys and SSH key access')


# Command-line interface

def add_destination_selector(parser, *, providers=False):
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--destination', action='append', help='Select a destination ID. Repeatable.')
    if providers:
        selection.add_argument('--platform', choices=['all', 'both', *PROVIDER_APIS, 'local'], default='all')


# Dashboard activity journal, shared by independent CLI and web processes

class DashboardJournal:
    def __init__(self, settings, *, initialize=False):
        self.directory = settings['storage']['path'] / 'web-dashboard'
        self.logs = self.directory / 'logs'
        self.path = self.directory / 'activity.sqlite3'
        self.change = self.directory / 'revision'
        self.scope = scope_id(settings['source'])
        if initialize:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.logs.mkdir(exist_ok=True, mode=0o700)
            if os.name != 'nt':
                self.directory.chmod(0o700)
                self.logs.chmod(0o700)
            with closing(self.connect()) as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS operations (
                        id TEXT PRIMARY KEY, scope TEXT NOT NULL, started REAL NOT NULL,
                        ended REAL, heartbeat REAL NOT NULL, operation TEXT NOT NULL,
                        origin TEXT NOT NULL, mode TEXT NOT NULL, repository TEXT,
                        destinations TEXT NOT NULL, state TEXT NOT NULL, exit_code INTEGER,
                        error TEXT, progress TEXT
                    );
                    CREATE INDEX IF NOT EXISTS started_operations ON operations(scope, started DESC);
                    CREATE TABLE IF NOT EXISTS results (
                        run TEXT NOT NULL, repository TEXT NOT NULL, destination TEXT NOT NULL,
                        state TEXT NOT NULL, detail TEXT, at REAL NOT NULL,
                        PRIMARY KEY (run, repository, destination)
                    );
                    CREATE INDEX IF NOT EXISTS recent_results ON results(repository, at DESC);
                    CREATE TABLE IF NOT EXISTS inventory (
                        scope TEXT PRIMARY KEY, checked REAL NOT NULL, repositories TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS verification (
                        scope TEXT NOT NULL, destination TEXT NOT NULL, repository TEXT NOT NULL,
                        generation TEXT NOT NULL, stamp TEXT NOT NULL, state TEXT NOT NULL,
                        at REAL NOT NULL, detail TEXT,
                        PRIMARY KEY(scope, destination, repository, generation)
                    );
                    CREATE TABLE IF NOT EXISTS schedules (
                        scope TEXT NOT NULL, id TEXT NOT NULL, due REAL NOT NULL, lease REAL NOT NULL,
                        PRIMARY KEY(scope, id)
                    );
                    CREATE TABLE IF NOT EXISTS dashboard_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        at REAL NOT NULL, username TEXT NOT NULL,
                        action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS dashboard_audit_at ON dashboard_audit(at DESC);
                """)
            if os.name != 'nt':
                self.path.chmod(0o600)
            self.touch()

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw' if self.path.exists() else self.path,
                             uri=self.path.exists(), timeout=3)
        db.row_factory = sqlite3.Row
        return db

    def read(self, sql, args=()):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute(sql, args)]

    def write(self, sql, args):
        with closing(self.connect()) as db, db:
            db.execute(sql, args)
        self.touch()

    def touch(self):
        self.change.touch(mode=0o600, exist_ok=True)

    def revision(self):
        try:
            return self.change.stat().st_mtime_ns
        except OSError:
            return 0

    def audit(self, username, action, target='', detail=''):
        if not all(isinstance(item, str) and len(item) <= 200 for item in
                   (username, action, target, detail)):
            raise ValueError('Invalid audit entry')
        self.write('INSERT INTO dashboard_audit (at,username,action,target,detail) VALUES (?,?,?,?,?)',
                   (time.time(), username, action, target, detail))

    def begin(self, identifier, operation, mode, origin, repository, destinations, *, state='running'):
        now = time.time()
        self.write('INSERT INTO operations (id,scope,started,heartbeat,operation,origin,mode,repository,'
                   'destinations,state) VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (identifier, self.scope, now, now, operation, origin, mode, repository,
                    json.dumps(destinations), state))

    def finish(self, identifier, code, *, error=None, only_unfinished=False):
        where = " AND state IN ('queued','running')" if only_unfinished else ''
        self.write(f'UPDATE operations SET ended=?, state=?, exit_code=?, error=COALESCE(error,?) WHERE id=?{where}',
                   (time.time(), 'succeeded' if code == 0 else 'interrupted' if code == 130 else 'failed',
                    code, error, identifier))

    def operations(self):
        rows = self.read('SELECT * FROM operations WHERE scope=? ORDER BY started DESC LIMIT 100', (self.scope,))
        for row in rows:
            row['destinations'] = json.loads(row['destinations'])
            row['progress'] = json.loads(row['progress']) if row['progress'] else None
            if row['state'] in ('running', 'queued') and row['heartbeat'] < time.time() - 75:
                row['state'] = 'interrupted'
                row['error'] = 'Activity lease expired; inspect the operation before retrying'
        return rows


def redact_dashboard_log(message, values=()):
    for value in values:
        if value:
            message = message.replace(value, '[redacted]').replace(urllib.parse.quote(value, safe=''), '[redacted]')
    message = re.sub(r'(?i)\b((?:https?|ssh|git)://)[^/\s@]+@', r'\1[redacted]@', message)
    message = re.sub(r'(?i)\b(authorization|private[-_]token|(?:access_|oauth_)?token|password)(\s*[:=]\s*)'
                     r'(?:(?:bearer|basic|token)\s+)?[^\s&,;]+', r'\1\2[redacted]', message)
    message = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]', '', message)
    return message


def dashboard_secret_values(settings):
    values = set()
    for spec in (settings['source'], *settings['destinations'].values()):
        if 'oauth_id' in spec:
            try:
                record = oauth_record(spec['_oauth_root'], spec['oauth_id'])
                values.update(record[key] for key in ('access_token', 'refresh_token') if oauth_secret(record.get(key)))
            except OAuthError:
                pass
        elif spec['provider'] != 'local':
            try:
                values.update(token(spec).splitlines())
            except (SyncError, UnicodeError):
                pass
    return sorted(values, key=len, reverse=True)


class ObservedOutput:
    """Record complete redacted lines, with bounded log size and normal terminal output."""

    def __init__(self, stream, activity, log):
        self.stream, self.activity, self.log = stream, activity, log
        self.pending = ''

    def write(self, text):
        self.pending += text
        lines = self.pending.split('\n')
        self.pending = lines.pop()
        for line in lines:
            self.emit(line + '\n')
        return len(text)

    def emit(self, text):
        safe = self.activity.redact(text)
        if re.search(r'(?i)\b(failed|blocked|another gitmirror process)\b', safe):
            self.activity.last_error = safe.strip()[:2048]
        self.stream.write(safe)
        if self.log is None:
            return
        try:
            if self.log.tell() < 1_048_576:
                self.log.write(safe[:8192])
                self.log.flush()
                if time.monotonic() - self.activity.log_changed > 1:
                    self.activity.log_changed = time.monotonic()
                    self.activity.journal.touch()
        except OSError:
            self.log = None

    def flush(self):
        if self.pending:
            self.emit(self.pending)
            self.pending = ''
        self.stream.flush()


class DashboardOperation:
    def __init__(self, journal, identifier, settings, operation, mode, origin):
        self.journal, self.id, self.origin = journal, identifier, origin
        self.values = dashboard_secret_values(settings) if operation in ('sync', 'doctor') else ()
        self.log_changed = 0
        self.last_error = None
        self.seen, self.unavailable = set(), set()
        self.stop = threading.Event()

    def redact(self, message):
        return redact_dashboard_log(message, self.values)

    def write(self, sql, args):
        try:
            self.journal.write(sql, args)
        except (OSError, sqlite3.Error):
            # Telemetry failure must never interrupt repository safety checks.
            pass

    def heartbeat(self):
        while not self.stop.wait(20):
            self.write('UPDATE operations SET heartbeat=? WHERE id=?', (time.time(), self.id))

    def inventory(self, records):
        entries = [{key: repo.get(key) for key in ('id', 'name', 'default_branch', 'visibility', 'size_bytes')} for repo in records]
        self.write('INSERT OR REPLACE INTO inventory VALUES (?,?,?)',
                   (self.journal.scope, time.time(), json.dumps(entries)))

    def progress(self, repository, index, total):
        value = json.dumps({'repository': repository, 'index': index, 'total': total})
        self.write('UPDATE operations SET progress=? WHERE id=?', (value, self.id))

    def result(self, repository, destination, state, detail=None):
        self.seen.add((repository, destination))
        if not repository:
            self.unavailable.add(destination)
        self.write('INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?)',
                   (self.id, repository, destination, state, self.redact(detail)[:2048] if detail else None, time.time()))

    def unreported(self, repository, destinations, detail):
        for destination in destinations:
            if (repository, destination) not in self.seen:
                self.result(repository, destination, 'blocked', 'Destination unavailable for this run'
                            if destination in self.unavailable else detail)

    def backup(self, destination, repository, generation, state, detail=None):
        self.write('INSERT OR REPLACE INTO verification VALUES (?,?,?,?,?,?,?,?)',
                   (self.journal.scope, destination, repository, generation.stem, generation_stamp(generation),
                    state, time.time(), self.redact(detail)[:2048] if detail else None))


def generation_stamp(generation):
    try:
        return hashlib.sha256((generation / BACKUP_MANIFEST).read_bytes()).hexdigest()
    except OSError:
        return ''


def observe_dashboard_operation(settings, operation, mode, origin, repository, destinations, execute, *, enabled):
    if not enabled:
        return execute(None)
    journal = DashboardJournal(settings)
    if not journal.path.is_file():
        return execute(None)
    web_id = os.getenv('GITMIRROR_WEB_JOB', '')
    try:
        attached = (re.fullmatch(r'[a-f0-9]{32}', web_id)
                    and journal.read("SELECT id FROM operations WHERE id=? AND scope=? AND state='queued'",
                                     (web_id, journal.scope)))
        identifier = web_id if attached else uuid.uuid4().hex
        if attached:
            origin = 'web'
            journal.write("UPDATE operations SET state='running', heartbeat=? WHERE id=?", (time.time(), identifier))
        else:
            journal.begin(identifier, operation, mode, origin, repository, destinations or [])
    except (OSError, sqlite3.Error):
        return execute(None)

    activity = DashboardOperation(journal, identifier, settings, operation, mode, origin)
    heartbeat = threading.Thread(target=activity.heartbeat, daemon=True)
    heartbeat.start()
    code, error, log = 2, None, None
    try:
        try:
            log = (journal.logs / f'{identifier}.log').open('x', encoding='utf-8', errors='replace')
            if os.name != 'nt':
                os.fchmod(log.fileno(), 0o600)
        except OSError:
            if log:
                log.close()
            log = None
        out, err = ObservedOutput(sys.stdout, activity, log), ObservedOutput(sys.stderr, activity, log)
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = execute(activity)
            except (ConfigError, SyncError, OSError, Timeout) as exc:
                error = activity.redact(str(exc))[:2048]
                print(f'GitMirror: {error}', file=sys.stderr)
            except KeyboardInterrupt:
                code = 130
            finally:
                out.flush()
                err.flush()
        return code
    finally:
        activity.stop.set()
        heartbeat.join()
        if log:
            log.close()
        try:
            detail = (error or activity.last_error or f'GitMirror exit {code}') if code else None
            if attached:
                # The launcher publishes a terminal state after the child exits and releases its queue lock.
                journal.write('UPDATE operations SET exit_code=?, error=? WHERE id=?', (code, detail, identifier))
            else:
                journal.finish(identifier, code, error=detail)
        except (OSError, sqlite3.Error):
            pass


def wait_for_dashboard_schedule(settings, delay, *, monitored):
    journal = DashboardJournal(settings)
    if not monitored or not journal.path.is_file():
        time.sleep(delay)
        return
    identifier, due = uuid.uuid4().hex, time.time() + delay
    until = time.monotonic() + delay
    try:
        while (remaining := until - time.monotonic()) > 0:
            try:
                journal.write('INSERT OR REPLACE INTO schedules VALUES (?,?,?,?)',
                              (journal.scope, identifier, due, time.time() + 75))
            except (OSError, sqlite3.Error):
                pass
            time.sleep(min(remaining, 30))
    finally:
        try:
            journal.write('DELETE FROM schedules WHERE scope=? AND id=?', (journal.scope, identifier))
        except (OSError, sqlite3.Error):
            pass


def dashboard_destination(spec, identifier):
    return {'id': identifier, 'provider': spec['provider'], 'owner': spec.get('owner'),
            'path': str(spec['path']) if spec['provider'] == 'local' else None,
            'api_url': spec.get('api_url'), 'retention': spec.get('retention')}


def logical_backup_sizes(path, scope, directories):
    def unreadable(error):
        raise error

    by_repo = {directory.name: 0 for directory in directories}
    total = 0
    for root, subdirs, files in os.walk(path, onerror=unreadable):
        folder = Path(root)
        subdirs[:] = [name for name in subdirs if not linked_backup_path(folder / name)]
        parts = folder.relative_to(path).parts
        for name in files:
            info = (folder / name).lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if len(parts) >= 2 and parts[0] == scope and parts[1] in by_repo:
                    by_repo[parts[1]] += info.st_size
    return total, by_repo


def dashboard_backups(settings, journal, sizes):
    verified = {(row['destination'], row['repository'], row['generation']): row for row in
                journal.read('SELECT * FROM verification WHERE scope=?', (journal.scope,))}
    output = []
    for identifier, spec in settings['destinations'].items():
        if spec['provider'] != 'local':
            continue
        path = spec['path']
        item = {'id': identifier, 'path': str(path), 'free': None, 'used': None, 'error': None, 'repositories': []}
        output.append(item)
        try:
            volume = backup_volume(path)
            item['free'] = shutil.disk_usage(path).free
            directories = backup_directories(path, journal.scope)
            if identifier not in sizes or sizes[identifier][0] != volume or sizes[identifier][1] < time.monotonic() - 60:
                total, directory_bytes = logical_backup_sizes(path, journal.scope, directories)
                sizes[identifier] = volume, time.monotonic(), total, directory_bytes
            item['used'] = sizes[identifier][2]
            for directory in directories:
                status = {'id': directory.name, 'name': directory.name, 'error': None,
                          'logical_bytes': sizes[identifier][3].get(directory.name), 'generations': []}
                item['repositories'].append(status)
                try:
                    current = current_backup(directory)
                except SyncError:
                    current = None
                    status['error'] = 'CURRENT is invalid; select an older generation'
                for generation in reversed(backup_generations(directory)):
                    row = verified.get((identifier, directory.name, generation.stem))
                    stamp = generation_stamp(generation)
                    if row and row['stamp'] != stamp:
                        row = None
                    integrity = row['state'] if row else 'unverified'
                    created = None
                    try:
                        manifest = backup_manifest(generation)
                        status['name'] = manifest['name']
                        created = generation_created(generation, manifest) / 1e9
                    except (SyncError, OSError, ValueError):
                        integrity = 'failed'
                    status['generations'].append({'id': generation.stem, 'current': generation == current,
                                                  'created': created, 'integrity': integrity,
                                                  'verified': row['at'] if row else None})
            if backup_volume(path) != volume:
                raise SyncError('Backup volume changed during inspection')
        except (SyncError, OSError) as exc:
            sizes.pop(identifier, None)
            item['used'] = None
            item['error'] = redact_dashboard_log(str(exc))
    return output


# Guided Linux installation and managed configuration

@dataclass(frozen=True)
class ManagedConfiguration:
    identifier: str
    path: Path

    @property
    def config(self):
        return self.path / 'config.yml'

    @property
    def ssh_config(self):
        return self.path / 'ssh.config'


def private_text(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def scrypt_password(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=16384, r=8, p=1)


def valid_admin_password(password):
    """An unrestricted 6+ character password; rate limiting and optional TOTP protect login."""
    return isinstance(password, str) and 6 <= len(password) <= 1024


def totp_at(secret, counter):
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, int(counter).to_bytes(8, 'big'), hashlib.sha1).digest()
    offset = digest[-1] & 0x0f
    number = int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff
    return f'{number % 1000000:06d}'


def totp_counter(secret, code, now=None):
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        return None
    current = int((time.time() if now is None else now) // 30)
    try:
        for counter in (current - 1, current, current + 1):
            if counter >= 0 and hmac.compare_digest(totp_at(secret, counter), code):
                return counter
    except (binascii.Error, ValueError, TypeError):
        pass
    return None


def private_security(root):
    path = root / 'security.json'
    try:
        state = oauth_private_file(path)
    except FileNotFoundError:
        return {'enabled': False}
    if not isinstance(state, dict) or type(state.get('enabled')) is not bool:
        raise SyncError('Dashboard 2FA settings are invalid')
    return state


def security_verify(root, code):
    """Accept a fresh TOTP or one disposable recovery code, atomically."""
    path = root / 'security.json'
    with FileLock(str(root / '.security.lock'), timeout=25):
        state = private_security(root)
        if not state['enabled']:
            return True
        current = totp_counter(state.get('secret', ''), code)
        if current is not None and current > state.get('last_counter', -1):
            state['last_counter'] = current
            store_oauth(path, state)
            return True
        if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{16}', code):
            digest = hashlib.sha256(code.encode('ascii')).hexdigest()
            hashes = state.get('recovery', [])
            matched = next((i for i, value in enumerate(hashes) if hmac.compare_digest(value, digest)), None)
            if matched is not None:
                state['recovery'] = hashes[:matched] + hashes[matched + 1:]
                store_oauth(path, state)
                return True
        return False




DASHBOARD_ROLES = ('administrator', 'operator', 'viewer')
DASHBOARD_USERNAME = re.compile(r'[a-z][a-z0-9_.-]{2,31}\Z')


class DashboardUsers:
    """Separate managed-dashboard identities. No users or secrets in the Git configuration."""

    def __init__(self, managed):
        self.managed = managed
        self.path = managed.root / 'dashboard-users.json'
        self.lock = managed.root / '.dashboard-users.lock'

    def ensure(self, revision):
        """One-time, non-destructive upgrade of the original administrator and global TOTP."""
        if self.path.is_file() or revision is None:
            return
        with FileLock(str(self.lock), timeout=25):
            if self.path.is_file():
                return
            original = oauth_private_file(revision.path / 'admin.json')
            legacy_2fa = private_security(self.managed.root)
            record = {'role': 'administrator', 'salt': original['salt'],
                      'password': original['password'], 'generation': 0,
                      'totp': legacy_2fa}
            store_oauth(self.path, {'version': 1, 'users': {'admin': record}})

    def _all(self):
        data = oauth_private_file(self.path)
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('users'), dict):
            raise SyncError('Dashboard user database is invalid')
        return data

    def get(self, username):
        if not isinstance(username, str) or not DASHBOARD_USERNAME.fullmatch(username):
            return None
        try:
            user = self._all()['users'].get(username)
        except FileNotFoundError:
            return None
        if not isinstance(user, dict) or user.get('role') not in DASHBOARD_ROLES:
            return None
        return user

    def verify(self, username, password):
        user = self.get(username)
        if not isinstance(password, str) or len(password) > 1024:
            return False
        try:
            if user:
                salt, digest = bytes.fromhex(user['salt']), bytes.fromhex(user['password'])
            else:
                salt, digest = b'\0' * 32, b'\0' * 64
            valid = hmac.compare_digest(scrypt_password(password, salt), digest)
            return bool(user and valid)
        except (ValueError, TypeError, UnicodeError):
            return False

    def public(self):
        data = self._all()
        return [{'username': name, 'role': user['role'],
                 'totp': bool(user.get('totp', {}).get('enabled')),
                 'generation': user.get('generation', 0)}
                for name, user in sorted(data['users'].items())]

    def _mutate(self, change):
        with FileLock(str(self.lock), timeout=25):
            data = self._all()
            result = change(data['users'])
            if not any(item.get('role') == 'administrator' for item in data['users'].values()):
                raise ConfigError('At least one administrator must remain')
            store_oauth(self.path, data)
            return result

    @staticmethod
    def _password(user, password):
        if not valid_admin_password(password):
            raise ConfigError('Password must contain 6 to 1024 characters')
        salt = secrets.token_bytes(32)
        user['salt'], user['password'] = salt.hex(), scrypt_password(password, salt).hex()
        user['generation'] = user.get('generation', 0) + 1

    def create(self, username, role, password):
        if not isinstance(username, str):
            raise ConfigError('Choose a username')
        username = username.strip().lower()
        if not DASHBOARD_USERNAME.fullmatch(username) or role not in DASHBOARD_ROLES:
            raise ConfigError('Use a 3 to 32 character username and a valid role')
        if not valid_admin_password(password):
            raise ConfigError('Password must contain 6 to 1024 characters')
        def change(users):
            if username in users or len(users) >= 32:
                raise ConfigError('Username already exists or user limit reached')
            user = {'role': role, 'totp': {'enabled': False}, 'generation': 0}
            self._password(user, password)
            users[username] = user
        self._mutate(change)
        return username

    def manage(self, username, action, value=None, *, actor=None):
        if not isinstance(username, str) or not DASHBOARD_USERNAME.fullmatch(username):
            raise ConfigError('Invalid username')
        if action not in ('role', 'password', 'delete', 'reset_2fa'):
            raise ConfigError('Unknown user operation')
        def change(users):
            if username not in users:
                raise ConfigError('Unknown user')
            if action == 'delete':
                if username == actor:
                    raise ConfigError('You cannot delete your own account')
                del users[username]
                return
            user = users[username]
            if action == 'role':
                if value not in DASHBOARD_ROLES:
                    raise ConfigError('Unknown role')
                if username == actor and value != 'administrator':
                    raise ConfigError('You cannot remove your own administrator role')
                user['role'] = value
            elif action == 'password':
                self._password(user, value)
                return
            elif action == 'reset_2fa':
                user['totp'] = {'enabled': False}
            user['generation'] = user.get('generation', 0) + 1
        self._mutate(change)

    def start_totp(self, username):
        secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')
        def change(users):
            state = users[username].get('totp', {})
            if state.get('enabled'):
                raise ConfigError('Two-factor authentication is already enabled')
            users[username]['totp'] = {'enabled': False, 'pending_secret': secret,
                                        'pending_until': time.time() + 600}
        self._mutate(change)
        return secret

    def confirm_totp(self, username, code):
        def change(users):
            state = users[username].get('totp', {})
            if state.get('enabled') or state.get('pending_until', 0) <= time.time():
                raise ConfigError('Authenticator setup has expired')
            secret = state.get('pending_secret', '')
            if totp_counter(secret, code) is None:
                raise ConfigError('Invalid six-digit authenticator code')
            recovery = [secrets.token_urlsafe(12) for _ in range(8)]
            users[username]['totp'] = {
                'enabled': True, 'secret': secret, 'last_counter': -1,
                'recovery': [hashlib.sha256(code.encode('ascii')).hexdigest() for code in recovery]}
            users[username]['generation'] = users[username].get('generation', 0) + 1
            return recovery
        return self._mutate(change)

    def cancel_totp(self, username):
        def change(users):
            if not users[username].get('totp', {}).get('enabled'):
                users[username]['totp'] = {'enabled': False}
        self._mutate(change)

    def verify_totp(self, username, code):
        """Single-use TOTPs and recovery codes, with a cross-process file lock."""
        def change(users):
            state = users.get(username, {}).get('totp', {})
            if not state.get('enabled'):
                return False
            counter = totp_counter(state.get('secret', ''), code)
            if counter is not None and counter > state.get('last_counter', -1):
                state['last_counter'] = counter
                return True
            if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_-]{16}', code):
                digest = hashlib.sha256(code.encode('ascii')).hexdigest()
                matches = state.get('recovery', [])
                for index, item in enumerate(matches):
                    if hmac.compare_digest(item, digest):
                        del matches[index]
                        return True
            return False
        return self._mutate(change)

    def disable_totp(self, username):
        def change(users):
            users[username]['totp'] = {'enabled': False}
            users[username]['generation'] = users[username].get('generation', 0) + 1
        self._mutate(change)



def ssh_host_key_line(line):
    parts = line.split()
    if (len(parts) < 3 or not re.fullmatch(r'[A-Za-z0-9.\[\]:,_|+=-]+', parts[0])
            or not re.fullmatch(r'(?:ssh-(?:rsa|ed25519)|ecdsa-sha2-nistp(?:256|384|521))', parts[1])
            or not re.fullmatch(r'[A-Za-z0-9+/]+=*', parts[2])):
        raise ConfigError('Enter OpenSSH known_hosts lines with a host and its public key')
    return parts[:3]


def verified_ssh_host(data):
    host, port = data.get('host'), data.get('port', 22)
    fingerprint = data.get('fingerprint')
    if (not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,251}', host)
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(fingerprint, str) or not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint)):
        raise ConfigError('Enter an SSH hostname, a valid port and an official SHA256 fingerprint')
    result = subprocess.run(['ssh-keyscan', '-T', '4', '-p', str(port), '-t', 'ed25519,ecdsa,rsa', host],
                            capture_output=True, timeout=8, encoding='utf-8', errors='replace')
    for line in result.stdout.splitlines():
        try:
            _, kind, encoded = ssh_host_key_line(line)
            key = base64.b64decode(encoded, validate=True)
        except (ConfigError, binascii.Error):
            continue
        actual = 'SHA256:' + base64.b64encode(hashlib.sha256(key).digest()).decode('ascii').rstrip('=')
        if secrets.compare_digest(actual, fingerprint):
            address = host if port == 22 else f'[{host}]:{port}'
            return f'{address} {kind} {encoded}'
    raise SyncError('No SSH host key matched that fingerprint')


class ManagedInstallation:
    """Keep validated configurations and their secrets in versioned, private bundles."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.versions = self.root / 'configurations'
        self.work = self.root / 'work'
        self.volumes = self.root / 'backups'
        self.restores = self.root / 'restores'
        self.bootstrap = self.root / 'setup-code'
        self.pending_admin = self.root / 'initial-admin.json'
        self.identity = self.root / 'identity'
        self.pointer = self.root / 'active.json'
        self.previous = self.root / 'previous.json'

    def initialize(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with FileLock(str(self.root / '.configuration.lock'), timeout=0):
            for path in (self.versions, self.work, self.volumes, self.restores):
                path.mkdir(mode=0o700, exist_ok=True)
            if not self.identity.is_file():
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.identity)],
                               check=True, timeout=20, capture_output=True)
            if self.active() is None and not self.bootstrap.exists() and not self.pending_admin.exists():
                private_text(self.bootstrap, secrets.token_urlsafe(24) + '\n')

    def active(self):
        if not self.pointer.is_file():
            return None
        try:
            identifier = json.loads(self.pointer.read_text('utf-8'))['configuration']
            if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
                raise ValueError
            folder = self.versions / identifier
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError
            return ManagedConfiguration(identifier, folder)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SyncError('Managed configuration pointer is unreadable') from exc

    def public_key(self):
        if not self.identity.with_suffix('.pub').is_file():
            raise SyncError('Managed installation is not initialized')
        return self.identity.with_suffix('.pub').read_text('utf-8').strip()

    def placeholder(self):
        return {'version': 2, 'source': {'provider': 'github', 'owner': 'unconfigured',
                                         'api_url': PROVIDER_APIS['github']},
                'destinations': {}, 'storage': {'path': self.work}, 'schedule': {'interval_hours': 12}}

    def settings(self, revision):
        raw = yaml.safe_load(revision.config.read_text('utf-8'))
        def describe(spec, identifier):
            result = dashboard_destination(spec, identifier)
            if 'oauth_id' in spec:
                result['connection'] = self.oauth_status(spec['oauth_id'], spec)
            return result

        return {'source': describe(raw['source'], 'source'),
                'destinations': [describe(spec, spec['id']) for spec in raw['destinations']],
                'schedule': raw['schedule'], 'repositories': raw.get('repositories', []),
                'known_hosts': (revision.path / 'known_hosts').read_text('utf-8'),
                'public_key': self.public_key()}

    def credentials(self, revision):
        path = revision.path / 'admin.json' if revision else self.pending_admin
        content = oauth_private_file(path)
        salt, digest = bytes.fromhex(content['salt']), bytes.fromhex(content['password'])
        if len(salt) != 32 or len(digest) != 64:
            raise SyncError('Managed admin authentication data is invalid')
        return salt, digest

    def set_initial_password(self, password):
        if not valid_admin_password(password):
            raise ConfigError('Password must contain 6 to 1024 characters')
        with FileLock(str(self.root / '.configuration.lock'), timeout=0):
            if self.active() or self.pending_admin.exists() or not self.bootstrap.is_file():
                raise SyncError('Initial password has already been configured')
            salt = secrets.token_bytes(32)
            private_text(self.pending_admin, json.dumps({
                'salt': salt.hex(), 'password': scrypt_password(password, salt).hex()}))
            self.bootstrap.unlink()

    def authorize_setup(self, code):
        if self.active():
            raise SyncError('Setup is already complete')
        expected = self.bootstrap.read_text('utf-8').strip()
        if (not isinstance(code, str) or len(code) > 128
                or not secrets.compare_digest(code.encode('utf-8'), expected.encode('utf-8'))):
            raise PermissionError('Incorrect setup code')

    def oauth_status(self, identifier, spec=None):
        if spec:
            result = {'id': identifier, 'status': 'reconnect_required', 'account': spec['owner'],
                      'provider': spec['provider'], 'api_url': spec['api_url']}
        try:
            record = oauth_record(self.root, identifier)
            result = oauth_public(record, identifier)
            app = oauth_app(self.root, record['provider'], record['api_url'])
            result.update(permissions_url=app.permissions_url, ssh_host=app.ssh_endpoint[0], ssh_port=app.ssh_port)
            if record['client_id'] != app.client_id:
                result['status'] = 'reconnect_required'
        except (OAuthError, ValueError, TypeError, KeyError):
            if spec is None:
                raise OAuthError('Cannot read OAuth connection. Check the application registration.') from None
            result['status'] = 'reconnect_required'
        return result

    def provider(self, row, label, old, revision):
        if not isinstance(row, dict) or not isinstance(row.get('provider'), str):
            raise ConfigError(f'{label} requires a provider')
        provider = row['provider']
        fields = ('provider', 'path', 'retention') if provider == 'local' else ('provider', 'owner', 'api_url')
        clean = {key: row[key] for key in fields if key in row and row[key] is not None}
        if provider == 'local':
            return clean
        clean.setdefault('api_url', PROVIDER_APIS.get(provider))
        if provider == 'gitea':
            if not clean['api_url']:
                raise ConfigError(f'{label}.api_url must be an explicit HTTPS Gitea API URL')
            clean['api_url'] = clean['api_url'].rstrip('/') if isinstance(clean['api_url'], str) else clean['api_url']
        credential = row.get('token', '')
        if not isinstance(credential, str) or len(credential) > 4096 or any(c.isspace() for c in credential):
            raise ConfigError(f'{label} needs a single-line API token')
        role = 'source' if label == 'source' else 'destination'
        connection = row.get('oauth_id')
        if (not connection and row.get('auth_method') != 'token' and not credential and old):
            connection = old.get('oauth_id')
        if connection:
            if credential or row.get('auth_method') == 'token':
                raise ConfigError(f'{label} must use exactly one authorization method')
            try:
                app = oauth_app(self.root, provider, clean['api_url'])
                record = oauth_record(self.root, connection)
                oauth_binding(record, app, role)
                if (not isinstance(clean.get('owner'), str) or
                        clean['owner'].casefold() != record['account'].casefold()):
                    raise OAuthError('OAuth account differs from the configured owner')
                if record['status'] != 'connected' and (not old or connection != old.get('oauth_id')):
                    raise OAuthError(OAUTH_REAUTHORIZE)
            except OAuthError as exc:
                raise ConfigError(f'{label}: {exc}') from exc
            return {**clean, 'owner': record['account'], 'api_url': app.base, 'oauth_id': connection}
        if (not credential and old and all(clean.get(key) == old.get(key)
                                          for key in ('provider', 'owner', 'api_url')) and 'oauth_id' not in old):
            credential = token(old)
        if not credential:
            raise ConfigError(f'{label} requires an API token')
        secrets_path = revision.path / f'{label}.token'
        private_text(secrets_path, credential)
        clean['token_file'] = str(secrets_path)
        return clean

    def save(self, payload, *, bootstrap=False):
        with FileLock(str(self.root / '.configuration.lock'), timeout=0):
            active = self.active()
            if bootstrap:
                if not self.pending_admin.exists():
                    self.authorize_setup(payload.get('code'))
            elif not active:
                raise SyncError('Complete the initial setup first')
            old = load_config(active.config) if active else None
            if active and payload.get('revision') != active.identifier:
                raise SyncError('Configuration changed in another session; reload before saving')
            password = payload.get('password', '')
            if not isinstance(password, str) or (password and not valid_admin_password(password)):
                raise ConfigError('Admin password must contain 6 to 1024 characters')
            if bootstrap and not self.pending_admin.exists() and not password:
                raise ConfigError('Set an admin password to finish setup')
            if bootstrap and self.pending_admin.exists() and password:
                raise ConfigError('Initial admin password is already configured')
            if password and not bootstrap:
                current = payload.get('current_password')
                salt, verifier = self.credentials(active)
                if not isinstance(current, str) or len(current) > 1024 or not secrets.compare_digest(
                        scrypt_password(current, salt), verifier):
                    raise PermissionError('Current admin password is incorrect')

            with FileLock(str(self.work / 'web-dashboard' / '.action.lock'), timeout=0):
                with FileLock(str(self.work / '.gitmirror.lock'), timeout=0):
                    folder = self.versions / uuid.uuid4().hex
                    folder.mkdir(mode=0o700)
                    revision = ManagedConfiguration(folder.name, folder)
                    committed = False
                    try:
                        self.write_revision(revision, payload, old, active, password)
                        if active:
                            store_json(self.previous, {'configuration': active.identifier})
                        store_json(self.pointer, {'configuration': revision.identifier})
                        committed = True
                    finally:
                        # A durability error after replacing active.json may still have published the bundle.
                        if not committed and self.active() != revision:
                            shutil.rmtree(folder)
            if bootstrap:
                self.bootstrap.unlink(missing_ok=True)
                self.pending_admin.unlink(missing_ok=True)
            return revision.identifier

    def write_revision(self, revision, payload, old, active, password):
        targets = payload.get('destinations')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
            raise ConfigError('Configure between 1 and 20 destinations')
        source = self.provider(payload.get('source'), 'source', old['source'] if old else None, revision)
        if source['provider'] == 'local':
            raise ConfigError('Select a hosting provider for the source')
        destinations, ids = [], set()
        for row in targets:
            identifier = row.get('id') if isinstance(row, dict) else None
            if not isinstance(identifier, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', identifier):
                raise ConfigError('Each destination needs a lowercase ID')
            if identifier in ids:
                raise ConfigError('Use a unique ID for each destination')
            ids.add(identifier)
            spec = self.provider(row, f'destination-{identifier}', old['destinations'].get(identifier) if old else None,
                                 revision)
            spec['id'] = identifier
            if spec['provider'] == 'local':
                spec['path'] = spec.get('path') or str(self.volumes / identifier)
            destinations.append(spec)
        schedule = payload.get('schedule')
        if not isinstance(schedule, dict) or type(schedule.get('enabled')) is not bool:
            raise ConfigError('Choose whether to enable the schedule')
        repositories = payload.get('repositories', [])
        raw = {'version': 2, 'source': source, 'destinations': destinations, 'storage': {'path': str(self.work)},
               'schedule': {'enabled': schedule['enabled'], 'interval_hours': schedule.get('interval_hours')},
               'repositories': repositories}
        private_text(revision.config, yaml.safe_dump(raw, sort_keys=False))
        settings = load_config(revision.config)
        hosts = payload.get('known_hosts', '')
        if (not isinstance(hosts, str) or len(hosts) > 24576
                or any(ord(c) < 32 and c not in '\n\r\t' for c in hosts)):
            raise ConfigError('SSH known_hosts entries are invalid')
        lines = []
        for line in hosts.splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            lines.append(' '.join(ssh_host_key_line(line)))
        if not lines:
            raise ConfigError('Add verified SSH known_hosts entries before saving')
        private_text(revision.path / 'known_hosts', '\n'.join(lines) + '\n')
        try:
            subprocess.run(['ssh-keygen', '-l', '-f', str(revision.path / 'known_hosts')],
                           check=True, timeout=10, capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError('SSH known_hosts file must contain valid host keys') from exc
        for spec in (settings['source'], *settings['destinations'].values()):
            if 'oauth_id' in spec:
                app = oauth_app(self.root, spec['provider'], spec['api_url'])
                host, port = app.ssh_endpoint
                entry = host if port == 22 else f'[{host}]:{port}'
                match = subprocess.run(['ssh-keygen', '-F', entry, '-f', str(revision.path / 'known_hosts')],
                                       timeout=10, capture_output=True)
                if match.returncode:
                    raise ConfigError(f'Add a verified SSH host key for {entry} before saving')
        ssh_options = [('IdentityFile', self.identity), ('UserKnownHostsFile', revision.path / 'known_hosts')]
        for _, path in ssh_options:
            if any(c in str(path) for c in '"%\n\r'):
                raise ConfigError('Managed directory contains an unsupported SSH path')
        private_text(revision.ssh_config, 'Host *\n    IdentitiesOnly yes\n    StrictHostKeyChecking yes\n'
                     '    UpdateHostKeys no\n    BatchMode yes\n' +
                     ''.join(f'    {key} "{path.as_posix()}"\n' for key, path in ssh_options))
        if password:
            salt = secrets.token_bytes(32)
            verifier = scrypt_password(password, salt)
        else:
            salt, verifier = self.credentials(active)
        private_text(revision.path / 'admin.json', json.dumps({'salt': salt.hex(), 'password': verifier.hex()}))
        volumes = {key: spec for key, spec in settings['destinations'].items() if spec['provider'] == 'local'}
        for key, spec in volumes.items():
            if spec['path'] == self.volumes / key:
                spec['path'].mkdir(mode=0o700, exist_ok=True)
            if (old and key in old['destinations'] and old['destinations'][key].get('path') == spec['path']
                    and not (spec['path'] / BACKUP_MARKER).exists()):
                raise SyncError(f'Destination {key} is unavailable; reconnect its original backup volume')
            if initialize_backup(settings, [key]):
                raise SyncError(f'Cannot initialize backup destination {key}')
        DashboardJournal(settings, initialize=True)
        fsync_directory(revision.path)
        fsync_directory(self.versions)


def managed_scheduler(installation):
    journal, revision, settings = None, None, None
    identifier = uuid.uuid4().hex
    due = 0
    while True:
        current = installation.active()
        if current != revision:
            if journal:
                journal.write('DELETE FROM schedules WHERE scope=? AND id=?', (journal.scope, identifier))
            revision = current
            settings = load_config(current.config) if current else None
            journal = DashboardJournal(settings) if settings else None
            due = time.time()
        if not settings or not settings['schedule'].get('enabled', False) or managed_writes_blocked(settings):
            time.sleep(8)
            continue
        if time.time() < due:
            journal.write('INSERT OR REPLACE INTO schedules VALUES (?,?,?,?)',
                          (journal.scope, identifier, due, time.time() + 75))
            time.sleep(min(15, max(0, due - time.time())))
            continue
        journal.write('DELETE FROM schedules WHERE scope=? AND id=?', (journal.scope, identifier))
        os.environ['GITMIRROR_MANAGED_SSH_CONFIG'] = str(revision.ssh_config)
        observe_dashboard_operation(settings, 'sync', 'apply', 'scheduled', None, [],
                                    lambda activity: execute_sync(settings, apply=True, activity=activity), enabled=True)
        due = time.time() + settings['schedule']['interval_hours'] * 3600


def validate_managed(installation):
    revision = installation.active()
    if not revision:
        return 0
    settings = load_config(revision.config)
    if settings['storage']['path'] != installation.work:
        raise SyncError('The managed configuration does not use its private work directory')
    installation.credentials(revision)
    for spec in (settings['source'], *settings['destinations'].values()):
        if spec['provider'] != 'local' and 'oauth_id' not in spec:
            token(spec)
    if not revision.ssh_config.is_file():
        raise SyncError('Managed SSH configuration is missing')
    return 0


# Optional self-hosted dashboard

def create_web_app(settings, config_file, *, password, allow_actions=False, assets_dir=None,
                   allowed_hosts=None, restore_dir=None, public_origin=None, session_ttl=43200,
                   managed=None, revision=None, reload_config=None):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
        from starlette.background import BackgroundTask
    except ImportError as exc:
        raise SyncError('Web dashboard requires: pip install "gitmirror-resilient[web]"') from exc
    import asyncio
    import hmac
    from collections import deque

    globals()['_GitMirrorWebRequest'] = Request
    root = Path(assets_dir) if assets_dir else Path(__file__).resolve().parent / 'web'
    if not (root / 'index.html').is_file():
        root = Path(sys.prefix) / 'share' / 'gitmirror' / 'web'
    if not all((root / name).is_file() for name in ('index.html', 'style.css', 'app.js')):
        raise SyncError('Web assets are missing; install GitMirror with the web extra')
    font_files = frozenset(('source-sans-pro-regular.woff2', 'source-sans-pro-semibold.woff2',
                            'source-sans-pro-bold.woff2', 'source-sans-pro-italic.woff2'))
    if not all((root / 'fonts' / name).is_file() for name in font_files):
        raise SyncError('Bundled Source Sans Pro font assets are missing')
    pending_setup = managed is not None and revision is None
    config_digest = None
    if not pending_setup:
        try:
            config_digest = hashlib.sha256(Path(config_file).read_bytes()).digest()
        except OSError as exc:
            raise SyncError('Cannot read dashboard configuration') from exc
    if restore_dir is not None:
        restore_dir = Path(restore_dir).expanduser().resolve()
        if not restore_dir.is_dir() or any(restore_dir.is_relative_to(spec['path'])
                                          for spec in settings['destinations'].values() if spec['provider'] == 'local'):
            raise SyncError('Restore directory must exist outside the configured backup roots')

    if pending_setup:
        salt, password_hash = None, None
    elif managed:
        salt, password_hash = managed.credentials(revision)
    else:
        if not valid_admin_password(password):
            raise SyncError('Web password must contain 6 to 1024 characters')
        salt = secrets.token_bytes(32)
        password_hash = scrypt_password(password, salt)
    users = DashboardUsers(managed) if managed else None
    if users and revision:
        users.ensure(revision)
    journal = DashboardJournal(settings, initialize=True)
    app = FastAPI(title='GitMirror Dashboard', docs_url=None, redoc_url=None, openapi_url=None)
    allowed = {'localhost', '127.0.0.1', '::1', *(h.casefold() for h in allowed_hosts or ())}
    if public_origin:
        permitted = urlsplit_origin(public_origin)
        if not permitted or permitted.scheme != 'https' or permitted.hostname not in allowed:
            raise SyncError('Public origin must be HTTPS and use an allowed hostname')
    sessions, attempts = {}, {}
    mfa_pending = {}
    global_attempts = deque()
    auth_lock = threading.Lock()
    login_slots = threading.BoundedSemaphore(2)
    backup_lock = threading.Lock()
    discovery_lock = threading.Lock()
    backup_cache = (0, None, None)
    sizes = {}
    cookie = 'gm_session'

    def session(request):
        key = request.cookies.get(cookie, '')
        with auth_lock:
            active = sessions.get(key)
            if not active or active[0] <= time.monotonic():
                sessions.pop(key, None)
                return None
        if users and not pending_setup and active[2] == 'auth':
            account = users.get(active[3])
            if not account or account.get('generation', 0) != active[4]:
                with auth_lock:
                    sessions.pop(key, None)
                return None
        return active

    def auth(request, *, csrf=False, bootstrap=False, role=None):
        active = session(request)
        if not active or (active[2] != 'auth' and not bootstrap):
            raise HTTPException(401, 'Sign in required')
        if csrf and not hmac.compare_digest(active[1].encode('ascii'),
                                            request.headers.get('X-CSRF-Token', '').encode('utf-8')):
            raise HTTPException(403, 'Invalid session verification token')
        if role:
            current = users.get(active[3])['role'] if users and not pending_setup else 'administrator'
            allowed_roles = {'administrator': DASHBOARD_ROLES,
                             'operator': ('administrator', 'operator'),
                             'administrator_only': ('administrator',)}[role]
            if current not in allowed_roles:
                raise HTTPException(403, 'Your account does not have permission for this action')
        return active

    def acting(request):
        active = auth(request)
        return active[3] if users and not pending_setup else 'admin'

    def verify_current(request, password):
        name = acting(request)
        if not verify_password(password, revision, name):
            raise HTTPException(403, 'Current password is incorrect')
        return name

    def keep_current_session(request):
        if not users or pending_setup:
            return
        key = request.cookies.get(cookie, '')
        with auth_lock:
            old = sessions.get(key)
            username = old[3] if old else None
        if not username:
            return
        current = users.get(username)
        with auth_lock:
            for candidate, value in list(sessions.items()):
                if value[3] == username and candidate != key:
                    del sessions[candidate]
            if key in sessions:
                old = sessions[key]
                sessions[key] = (old[0], old[1], old[2], username, current['generation'])

    async def body(request, *, limit=16384):
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(415, 'Use application/json')
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise HTTPException(413, 'Request too large')
            data.extend(chunk)
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, RecursionError) as exc:
            raise HTTPException(400, 'Invalid JSON request') from exc

    @app.middleware('http')
    async def security(request: _GitMirrorWebRequest, call_next):
        host = request.headers.get('host', '')
        home = urlsplit_origin(f'{request.url.scheme}://{host}')
        origin = request.headers.get('origin')
        expected = public_origin or f'{request.url.scheme}://{host}'
        if not home or home.hostname not in allowed:
            response = JSONResponse({'detail': 'Unapproved dashboard hostname'}, status_code=400)
        elif origin is not None and origin.lower() != expected.lower():
            response = JSONResponse({'detail': 'Cross-origin request blocked'}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; img-src 'self'; font-src 'self'; frame-ancestors 'none'; "
                                       "base-uri 'none'; form-action 'self'",
        })
        return response

    @app.get('/')
    def home():
        return FileResponse(root / 'index.html', media_type='text/html')

    @app.get('/assets/{filename}')
    def asset(filename: str):
        media = {'style.css': 'text/css', 'app.js': 'text/javascript'}
        if filename not in media:
            raise HTTPException(404, 'Asset not found')
        return FileResponse(root / filename, media_type=media[filename])

    @app.get('/assets/fonts/{filename}')
    def font_asset(filename: str):
        if filename not in font_files:
            raise HTTPException(404, 'Asset not found')
        return FileResponse(root / 'fonts' / filename, media_type='font/woff2',
                            headers={'Cache-Control': 'public, max-age=86400'})

    def reserve_attempt(request):
        ip = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        with auth_lock:
            while global_attempts and global_attempts[0] <= now - 300:
                global_attempts.popleft()
            for key in [key for key, data in attempts.items() if data[-1] <= now - 300]:
                del attempts[key]
            recent = [t for t in attempts.get(ip, ()) if t > now - 300]
            if len(recent) >= 5 or len(global_attempts) >= 30 or not login_slots.acquire(blocking=False):
                raise HTTPException(429, 'Too many attempts. Try again later.', headers={'Retry-After': '300'})
            attempts[ip] = [*recent, now]
            global_attempts.append(now)
        return ip, now

    def issue_session(request, stage='auth', username='admin'):
        now = time.monotonic()
        lifetime = min(session_ttl, 600) if stage == 'bootstrap' else session_ttl
        key = secrets.token_urlsafe(32)
        generation = users.get(username)['generation'] if users and not pending_setup and stage == 'auth' else 0
        with auth_lock:
            ip = request.client.host if request.client else 'unknown'
            attempts.pop(ip, None)
            sessions.pop(request.cookies.get(cookie, ''), None)
            for expired in [k for k, value in sessions.items() if value[0] <= now]:
                del sessions[expired]
            if len(sessions) >= 128:
                sessions.pop(next(iter(sessions)))
            sessions[key] = (now + lifetime, secrets.token_urlsafe(32), stage, username, generation)
        secure = bool(public_origin or request.url.scheme == 'https')
        response = JSONResponse({'authenticated': stage == 'auth', 'first_login': stage == 'bootstrap',
                                 'username': username})
        response.set_cookie(cookie, key, httponly=True, samesite='strict', secure=secure,
                            max_age=lifetime, path='/')
        return response

    def verify_password(value, revision_for_password, username='admin'):
        if not isinstance(value, str) or len(value) > 1024:
            return False
        if users and not pending_setup:
            return users.verify(username, value)
        try:
            check_salt, digest = managed.credentials(revision_for_password) if managed else (salt, password_hash)
            return hmac.compare_digest(scrypt_password(value, check_salt), digest)
        except (OSError, ValueError, UnicodeError, TypeError, SyncError, OAuthError):
            return False

    @app.post('/api/login')
    async def login(request: _GitMirrorWebRequest):
        data = await body(request)
        entered = data.get('password')
        username = data.get('username', 'admin')
        username = username.strip().lower() if isinstance(username, str) else ''
        ip, stamp = reserve_attempt(request)
        try:
            if pending_setup and managed and not managed.pending_admin.is_file():
                try:
                    managed.authorize_setup(entered)
                    valid, stage = username == 'admin', 'bootstrap'
                except (PermissionError, OSError, SyncError, UnicodeError):
                    valid, stage = False, 'bootstrap'
            else:
                valid = await asyncio.to_thread(verify_password, entered, revision, username)
                valid = valid and (managed is not None or username == 'admin')
                stage = 'auth'
        finally:
            login_slots.release()
        if not valid:
            raise HTTPException(401, 'Invalid username or password')
        account = users.get(username) if users and not pending_setup else None
        if stage == 'auth' and account and account.get('totp', {}).get('enabled'):
            challenge = secrets.token_urlsafe(32)
            now = time.monotonic()
            with auth_lock:
                sessions.pop(request.cookies.get(cookie, ''), None)
                if len(mfa_pending) > 128:
                    mfa_pending.clear()
                mfa_pending[challenge] = (now + 180, ip, username, account['generation'])
            response = JSONResponse({'mfa_required': True})
            response.set_cookie('gm_2fa', challenge, httponly=True,
                                secure=bool(public_origin or request.url.scheme == 'https'),
                                samesite='strict', max_age=180, path='/api/login')
            return response
        return issue_session(request, stage, username)

    @app.post('/api/login/2fa')
    async def login_second_factor(request: _GitMirrorWebRequest):
        data = await body(request)
        ip, stamp = reserve_attempt(request)
        try:
            challenge = request.cookies.get('gm_2fa', '')
            with auth_lock:
                pending = mfa_pending.get(challenge)
            user = users.get(pending[2]) if pending and users else None
            if (not pending or pending[0] < time.monotonic() or pending[1] != ip or
                    not user or user['generation'] != pending[3] or
                    not user.get('totp', {}).get('enabled')):
                raise HTTPException(401, 'Start a new sign-in')
            passed = await asyncio.to_thread(users.verify_totp, pending[2], data.get('code'))
            if not passed:
                raise HTTPException(401, 'Invalid authenticator or recovery code')
            with auth_lock:
                mfa_pending.pop(challenge, None)
            response = issue_session(request, username=pending[2])
            response.delete_cookie('gm_2fa', httponly=True,
                                   secure=bool(public_origin or request.url.scheme == 'https'),
                                   samesite='strict', path='/api/login')
            return response
        finally:
            login_slots.release()

    @app.post('/api/setup/password')
    async def setup_password(request: _GitMirrorWebRequest):
        if not pending_setup or not managed:
            raise HTTPException(409, 'Initial setup is already complete')
        active = auth(request, csrf=True, bootstrap=True)
        if len(active) < 3 or active[2] != 'bootstrap':
            raise HTTPException(409, 'Temporary password already replaced')
        data = await body(request)
        try:
            await asyncio.to_thread(managed.set_initial_password, data.get('password'))
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        except (SyncError, OSError, Timeout):
            raise HTTPException(409, 'Initial password setup is unavailable') from None
        with auth_lock:
            key = request.cookies.get(cookie, '')
            if key in sessions:
                sessions[key] = (time.monotonic() + session_ttl, sessions[key][1], 'auth', 'admin', 0)
        response = JSONResponse({'saved': True})
        response.set_cookie(cookie, key, httponly=True, samesite='strict',
                            secure=bool(public_origin or request.url.scheme == 'https'),
                            max_age=session_ttl, path='/')
        return response

    @app.get('/api/session')
    def get_session(request: _GitMirrorWebRequest):
        active = auth(request, bootstrap=True)
        username = active[3]
        account = users.get(username) if users and not pending_setup else None
        return {'csrf': active[1], 'first_login': active[2] == 'bootstrap',
                'username': username, 'role': account['role'] if account else 'administrator'}

    @app.post('/api/logout')
    def logout(request: _GitMirrorWebRequest):
        auth(request, csrf=True)
        with auth_lock:
            sessions.pop(request.cookies.get(cookie, ''), None)
        response = JSONResponse({'ok': True})
        response.delete_cookie(cookie, path='/', secure=bool(public_origin or request.url.scheme == 'https'),
                               httponly=True, samesite='strict')
        return response

    if managed:
        @app.get('/api/security')
        def security_status(request: _GitMirrorWebRequest):
            active = auth(request)
            account = users.get(active[3]) if not pending_setup else None
            state = account.get('totp', {'enabled': False}) if account else {'enabled': False}
            return {'enabled': state['enabled'], 'pending': bool(
                not state['enabled'] and state.get('pending_until', 0) > time.time()),
                'recovery_remaining': len(state.get('recovery', [])) if state['enabled'] else 0}

        @app.post('/api/security/totp/start')
        async def security_start(request: _GitMirrorWebRequest):
            if pending_setup or not (public_origin or request.url.scheme == 'https'):
                raise HTTPException(403, 'Complete setup and use HTTPS before enabling 2FA')
            auth(request, csrf=True)
            data = await body(request)
            username = verify_current(request, data.get('password'))
            try:
                secret = await asyncio.to_thread(users.start_totp, username)
            except (ConfigError, OAuthError, OSError, Timeout) as exc:
                raise HTTPException(409, str(exc)) from None
            uri = ('otpauth://totp/GitMirror:' + urllib.parse.quote(username) +
                   '?secret=' + secret + '&issuer=GitMirror&algorithm=SHA1&digits=6&period=30')
            return {'secret': secret, 'uri': uri, 'expires_in': 600}

        @app.post('/api/security/totp/confirm')
        async def security_confirm(request: _GitMirrorWebRequest):
            if pending_setup:
                raise HTTPException(409, 'Complete initial setup first')
            active = auth(request, csrf=True)
            data = await body(request)
            try:
                recovery = await asyncio.to_thread(users.confirm_totp, active[3], data.get('code'))
            except (ConfigError, OAuthError, OSError, Timeout) as exc:
                raise HTTPException(400, str(exc)) from None
            keep_current_session(request)
            journal.audit(active[3], 'totp.enabled')
            return {'enabled': True, 'recovery_codes': recovery}

        @app.post('/api/security/totp/cancel')
        async def security_cancel(request: _GitMirrorWebRequest):
            active = auth(request, csrf=True)
            await asyncio.to_thread(users.cancel_totp, active[3])
            return {'cancelled': True}

        @app.post('/api/security/totp/disable')
        async def security_disable(request: _GitMirrorWebRequest):
            if pending_setup:
                raise HTTPException(409, 'Complete initial setup first')
            active = auth(request, csrf=True)
            data = await body(request)
            verify_current(request, data.get('password'))
            if not users.get(active[3]).get('totp', {}).get('enabled'):
                raise HTTPException(409, 'Two-factor authentication is not enabled')
            if not await asyncio.to_thread(users.verify_totp, active[3], data.get('code')):
                raise HTTPException(403, 'Invalid authenticator or recovery code')
            await asyncio.to_thread(users.disable_totp, active[3])
            keep_current_session(request)
            journal.audit(active[3], 'totp.disabled')
            return {'enabled': False}

        @app.get('/api/users')
        def list_users(request: _GitMirrorWebRequest):
            auth(request, role='administrator_only')
            return {'users': users.public()}

        @app.post('/api/users')
        async def create_user(request: _GitMirrorWebRequest):
            active = auth(request, csrf=True, role='administrator_only')
            data = await body(request)
            verify_current(request, data.get('current_password'))
            try:
                username = await asyncio.to_thread(users.create, data.get('username'),
                                                   data.get('role'), data.get('password'))
            except (ConfigError, OAuthError, OSError, Timeout) as exc:
                raise HTTPException(400, str(exc)) from None
            journal.audit(active[3], 'user.created', username, data.get('role', ''))
            return {'created': username}

        @app.post('/api/users/{username}/manage')
        async def manage_user(username: str, request: _GitMirrorWebRequest):
            active = auth(request, csrf=True, role='administrator_only')
            data = await body(request)
            verify_current(request, data.get('current_password'))
            operation = data.get('action')
            try:
                await asyncio.to_thread(users.manage, username, operation, data.get('value'), actor=active[3])
            except (ConfigError, OAuthError, OSError, Timeout) as exc:
                raise HTTPException(400, str(exc)) from None
            journal.audit(active[3], 'user.' + operation, username,
                          data.get('value') if operation == 'role' else '')
            if username == active[3]:
                keep_current_session(request)
            return {'saved': True}

        @app.post('/api/users/me/password')
        async def own_password(request: _GitMirrorWebRequest):
            if pending_setup:
                raise HTTPException(409, 'Complete initial setup first')
            active = auth(request, csrf=True)
            data = await body(request)
            verify_current(request, data.get('current_password'))
            try:
                await asyncio.to_thread(users.manage, active[3], 'password', data.get('password'), actor=active[3])
            except (ConfigError, OAuthError, OSError, Timeout) as exc:
                raise HTTPException(400, str(exc)) from None
            keep_current_session(request)
            journal.audit(active[3], 'password.changed')
            return {'saved': True}

        @app.get('/api/audit')
        def audit_history(request: _GitMirrorWebRequest):
            auth(request, role='administrator_only')
            return {'entries': journal.read(
                'SELECT at,username,action,target,detail FROM dashboard_audit ORDER BY id DESC LIMIT 200')}

        oauth_cookie = '__Host-gm_oauth'

        def oauth_authorized(key):
            try:
                current_revision = managed.active()
            except SyncError:
                return False
            if current_revision != revision:
                return False
            if pending_setup and not managed.pending_admin.exists():
                return key == ''
            with auth_lock:
                current = sessions.get(key)
            if not current or current[0] <= time.monotonic() or current[2] != 'auth':
                return False
            account = users.get(current[3]) if not pending_setup else None
            return bool(pending_setup or account and account['role'] == 'administrator' and
                        account['generation'] == current[4])

        flows = OAuthFlows(managed, oauth_authorized)
        app.state.oauth_flows = flows

        def oauth_origin(request):
            host = request.headers.get('host', '')
            origin = public_origin or f'{request.url.scheme}://{host}'
            parsed = urlsplit_origin(origin)
            if not parsed or parsed.scheme != 'https' or parsed.netloc.lower() != host.lower():
                raise HTTPException(403, 'Open the dashboard at its configured HTTPS address for browser authorization')
            return origin

        def oauth_browser(request, *, create=False):
            value = request.cookies.get(oauth_cookie, '')
            if re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
                return value
            if create:
                return secrets.token_urlsafe(32)
            raise HTTPException(403, 'Start authorization in this browser')

        def oauth_actor(request, data=None, *, start=False):
            if not pending_setup or managed.pending_admin.exists():
                auth(request, csrf=True, role='administrator_only')
                key = request.cookies.get(cookie, '')
                if not oauth_authorized(key):
                    raise HTTPException(409, 'Dashboard configuration changed. Reload and sign in again.')
                return key
            if start:
                ip, stamp = reserve_attempt(request)
                try:
                    managed.authorize_setup(data.get('code'))
                    with auth_lock:
                        attempts[ip].remove(stamp)
                        if not attempts[ip]:
                            del attempts[ip]
                        global_attempts.remove(stamp)
                except PermissionError:
                    raise HTTPException(403, 'Incorrect setup code') from None
                except (SyncError, OSError, UnicodeError):
                    raise HTTPException(409, 'Initial setup is unavailable') from None
                finally:
                    login_slots.release()
            return ''

        def oauth_selection(data):
            app = oauth_app(managed.root, data.get('provider'), data.get('api_url'))
            role = data.get('role')
            if role not in ('source', 'destination'):
                raise OAuthError('Choose a source or destination account')
            return app, role

        @app.post('/api/managed/oauth/options')
        async def oauth_options(request: _GitMirrorWebRequest):
            origin, data = oauth_origin(request), await body(request)
            oauth_actor(request, data, start=True)
            try:
                registration, role = await asyncio.to_thread(oauth_selection, data)
            except OAuthError as exc:
                raise HTTPException(422, str(exc)) from None
            methods = ['device'] if registration.device else []
            callback = origin + registration.callback_path
            if registration.redirect == callback:
                methods.append('browser')
            return {'methods': methods, 'callback': callback if registration.provider != 'github' else None,
                    'permissions_url': registration.permissions_url, 'scopes': registration.scopes(role),
                    'full_access': registration.provider == 'codeberg'}

        @app.post('/api/managed/oauth/start')
        async def oauth_start(request: _GitMirrorWebRequest):
            origin, data = oauth_origin(request), await body(request)
            actor = oauth_actor(request, data, start=True)
            browser = oauth_browser(request, create=True)
            try:
                registration, role = await asyncio.to_thread(oauth_selection, data)
                method = data.get('method')
                if ((method == 'device' and not registration.device)
                        or (method == 'browser' and registration.redirect != origin + registration.callback_path)
                        or method not in ('device', 'browser')):
                    raise OAuthError('This method is unavailable. Verify the app registration and exact callback URI.')
                if registration.provider == 'codeberg' and data.get('full_access_confirmed') is not True:
                    raise OAuthError('Confirm the account-wide access requested by Codeberg OAuth')
                owner = data.get('owner') or ''
                identifier = data.get('connection')
                if not isinstance(owner, str) or (owner and not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', owner)):
                    raise OAuthError('Enter a valid provider account name')
                if identifier is not None:
                    record = await asyncio.to_thread(oauth_record, managed.root, identifier)
                    oauth_binding(record, registration, role, owner or None)
                    owner = record['account']
                else:
                    identifier = uuid.uuid4().hex
                flow = OAuthFlow(registration, role, browser, actor, origin, owner, identifier, method,
                                 time.monotonic() + (900 if method == 'device' else 600))
                result = await asyncio.to_thread(flows.start, flow)
            except (OAuthError, OSError) as exc:
                raise HTTPException(422, str(exc) if isinstance(exc, OAuthError) else 'OAuth storage unavailable') from None
            response = JSONResponse(result)
            response.set_cookie(oauth_cookie, browser, secure=True, httponly=True, samesite='lax',
                                max_age=1800, path='/')
            return response

        @app.get('/api/managed/oauth/{provider}/callback')
        def oauth_callback(provider: str, request: _GitMirrorWebRequest):
            origin = oauth_origin(request)
            params = request.query_params
            accepted = False
            if all(len(params.getlist(key)) <= 1 for key in ('state', 'code', 'error', 'iss')):
                accepted = flows.callback(provider, params, request.cookies.get(oauth_cookie, ''), origin)
            return RedirectResponse('/#oauth-' + ('connected' if accepted else 'failed'), status_code=303)

        @app.post('/api/managed/oauth/flow/{identifier}/{operation}')
        def oauth_progress(identifier: str, operation: str, request: _GitMirrorWebRequest):
            origin, browser = oauth_origin(request), oauth_browser(request)
            actor = oauth_actor(request)
            try:
                if operation == 'status':
                    return flows.poll(identifier, browser, actor, origin)
                if operation == 'cancel':
                    return flows.cancel(identifier, browser, actor, origin)
            except OAuthError as exc:
                raise HTTPException(409, str(exc)) from None
            raise HTTPException(404, 'Unknown OAuth operation')

        @app.post('/api/managed/oauth/connection/{identifier}/{operation}')
        async def oauth_manage(identifier: str, operation: str, request: _GitMirrorWebRequest):
            _, data = oauth_origin(request), await body(request)
            oauth_actor(request, data, start=True)

            def manage():
                if operation == 'disconnect':
                    message = flows.disconnect(identifier)
                    return {'connection': managed.oauth_status(identifier), 'message': message}
                if operation == 'check':
                    record = oauth_record(managed.root, identifier)
                    registration = oauth_app(managed.root, record['provider'], record['api_url'])
                    credential = OAuthCredential(managed.root, identifier, registration, record['role'],
                                                 record['account'], refresh=True)
                    credential.check_account()
                    access = oauth_access(managed.root, identifier, registration, credential.role, credential.owner,
                                          refresh=True)
                    oauth_repository_access(registration, credential.role, credential.owner, access)
                    return {'connection': managed.oauth_status(identifier), 'message': 'Provider API access checked.'}
                raise HTTPException(404, 'Unknown OAuth operation')

            try:
                return await asyncio.to_thread(manage)
            except (OAuthError, OSError, Timeout) as exc:
                raise HTTPException(409, str(exc) if isinstance(exc, OAuthError) else
                                    'Account storage is busy. Wait for the sync to finish and retry.') from None

        @app.get('/api/setup')
        def setup_status():
            return {'required': pending_setup, 'password_ready': bool(pending_setup and managed.pending_admin.is_file()),
                    'public_key': managed.public_key() if pending_setup else None,
                    'revision': revision.identifier if revision else None}

        async def save_settings(data, *, initial=False):
            try:
                saved = await asyncio.to_thread(managed.save, data, bootstrap=initial)
            except PermissionError as exc:
                raise HTTPException(403, str(exc) if str(exc).startswith(('Incorrect setup', 'Current admin'))
                                    else 'Managed configuration cannot be written') from exc
            except (ConfigError, UnicodeError) as exc:
                raise HTTPException(400, 'Invalid configuration text' if isinstance(exc, UnicodeError) else str(exc))
            except (SyncError, Timeout, OSError, sqlite3.Error) as exc:
                message = str(exc) if isinstance(exc, SyncError) else 'Configuration is busy or storage is unavailable'
                raise HTTPException(409, message) from exc
            return JSONResponse({'saved': True, 'revision': saved},
                                background=BackgroundTask(reload_config) if reload_config else None)

        @app.post('/api/setup')
        async def setup(request: _GitMirrorWebRequest):
            if not pending_setup or managed.active():
                raise HTTPException(409, 'Setup is already complete')
            data = await body(request, limit=65536)
            if managed.pending_admin.exists():
                auth(request, csrf=True, role='administrator_only')
            else:
                # Compatible with earlier one-step setup clients; the secret is still required.
                try:
                    managed.authorize_setup(data.get('code'))
                except (PermissionError, OSError, SyncError):
                    raise HTTPException(403, 'Incorrect setup code') from None
            return await save_settings(data, initial=True)

        @app.post('/api/managed/ssh-host')
        async def check_ssh_host(request: _GitMirrorWebRequest):
            data = await body(request)
            if not pending_setup or managed.pending_admin.exists():
                auth(request, csrf=True, role='administrator_only')
            reserve_attempt(request)
            try:
                if pending_setup and not managed.pending_admin.exists():
                    managed.authorize_setup(data.get('code'))
                line = await asyncio.to_thread(verified_ssh_host, data)
                return {'known_host': line}
            except PermissionError as exc:
                raise HTTPException(403, 'Incorrect setup code') from exc
            except ConfigError as exc:
                raise HTTPException(400, str(exc)) from exc
            except (OSError, SyncError, subprocess.SubprocessError) as exc:
                raise HTTPException(502, 'Could not verify the SSH host with that fingerprint') from exc
            finally:
                login_slots.release()

        @app.get('/api/managed/settings')
        def managed_settings(request: _GitMirrorWebRequest):
            auth(request, role='administrator_only')
            return {**managed.settings(revision), 'revision': revision.identifier}

        @app.post('/api/managed/settings')
        async def update_settings(request: _GitMirrorWebRequest):
            active = auth(request, csrf=True, role='administrator_only')
            data = await body(request, limit=65536)
            if data.get('password'):
                verify_current(request, data.get('current_password'))
                if active[3] != 'admin':
                    raise HTTPException(403, 'Change your password in Security settings')
            result = await save_settings(data)
            if data.get('password'):
                # Preserve the existing administrator password editor while moving its identity to the account store.
                await asyncio.to_thread(users.manage, 'admin', 'password', data['password'], actor=active[3])
            journal.audit(active[3], 'configuration.updated')
            return result

    def local_data():
        nonlocal backup_cache
        with backup_lock:
            last = journal.read('SELECT MAX(at) AS latest FROM verification WHERE scope=?', (journal.scope,))[0]['latest']
            if backup_cache[0] < time.monotonic() - 12 or backup_cache[1] != last or backup_cache[2] is None:
                backup_cache = time.monotonic(), last, dashboard_backups(settings, journal, sizes)
            return backup_cache[2]

    def inventory():
        rows = journal.read('SELECT checked,repositories FROM inventory WHERE scope=?', (journal.scope,))
        return (rows[0]['checked'], json.loads(rows[0]['repositories'])) if rows else (None, None)

    @app.get('/api/overview')
    def overview(request: _GitMirrorWebRequest):
        auth(request)
        recent = journal.operations()
        counts = journal.read(
            "SELECT state,COUNT(*) AS total,MAX(ended) AS last FROM operations "
            "WHERE scope=? AND operation='sync' AND mode='apply' GROUP BY state", (journal.scope,))
        summary = {row['state']: row for row in counts}
        next_runs = journal.read('SELECT MIN(due) AS next FROM schedules WHERE scope=? AND lease>?',
                                 (journal.scope, time.time()))
        try:
            free = shutil.disk_usage(settings['storage']['path']).free
        except OSError:
            free = None
        checked, repositories = inventory()
        volumes = local_data()
        _, current = backup_health(volumes)
        source_repositories = [{'id': repo['id'], 'name': repo['name'], 'bytes': repo.get('size_bytes')}
                               for repo in repositories or []]
        backup_repositories = {}
        for volume in volumes:
            if volume['error']:
                continue
            for repo in volume['repositories']:
                length = repo.get('logical_bytes')
                if type(length) is int:
                    record = backup_repositories.setdefault(repo['id'],
                        {'id': repo['id'], 'name': repo['name'], 'bytes': 0})
                    record['bytes'] += length
        return {'source': {key: settings['source'][key] for key in ('provider', 'owner', 'api_url')},
                'destinations': [dashboard_destination(s, k) for k, s in settings['destinations'].items()],
                'schedule_hours': settings['schedule']['interval_hours'],
                'storage': {'path': str(settings['storage']['path']), 'free': free},
                'web': {'allow_actions': allow_actions, 'restore_enabled': bool(restore_dir), 'managed': bool(managed),
                        'writes_paused': managed_writes_blocked(settings),
                        'secure': bool(public_origin or request.url.scheme == 'https')},
                'inventory_at': checked, 'total_repositories': None if repositories is None else len(repositories),
                'repository_storage': {'metric': 'provider-reported', 'repositories': source_repositories},
                'backup_storage': {'metric': 'logical-file-bytes', 'repositories': list(backup_repositories.values())},
                'successful': summary.get('succeeded', {}).get('total', 0),
                'failed': summary.get('failed', {}).get('total', 0),
                'last_success': summary.get('succeeded', {}).get('last'),
                'next_run': next_runs[0]['next'], 'backup': current,
                'runs': recent, 'active': [r for r in recent if r['state'] in ('running','queued')],
                'errors': journal.read("SELECT r.repository,r.destination,r.detail,r.at FROM results r "
                                       "JOIN operations o ON o.id=r.run WHERE o.scope=? AND r.detail IS NOT NULL "
                                       "UNION ALL SELECT repository,'',error,ended FROM operations o "
                                       "WHERE scope=? AND error IS NOT NULL AND NOT EXISTS "
                                       "(SELECT 1 FROM results r WHERE r.run=o.id AND r.detail IS NOT NULL) "
                                       "ORDER BY at DESC LIMIT 8", (journal.scope, journal.scope))}

    @app.get('/api/repositories')
    def repositories(request: _GitMirrorWebRequest):
        auth(request)
        at, records = inventory()
        last = journal.read(
            "SELECT repository,destination,state,detail,at,mode FROM (SELECT r.*,o.mode,"
            "ROW_NUMBER() OVER(PARTITION BY r.repository,r.destination ORDER BY r.at DESC) AS newest "
            "FROM results r JOIN operations o ON o.id=r.run WHERE o.scope=? AND r.repository!='' "
            "AND (o.mode='apply' OR NOT EXISTS (SELECT 1 FROM results a JOIN operations p ON p.id=a.run "
            "WHERE a.repository=r.repository AND a.destination=r.destination AND p.scope=o.scope AND p.mode='apply')))"
            " WHERE newest=1", (journal.scope,))
        details = {}
        for result in last:
            details.setdefault(result['repository'].casefold(), []).append(result)
        backups, _ = backup_health(local_data())
        for record in records or []:
            record['destinations'] = details.get(record['name'].casefold(), [])
            record['last_backup'] = backups.get(str(record['id']))
        return {'checked': at, 'repositories': records}

    @app.post('/api/repositories/refresh')
    def refresh_repositories(request: _GitMirrorWebRequest):
        active = auth(request, csrf=True, role='operator')
        if not discovery_lock.acquire(blocking=False):
            raise HTTPException(409, 'Inventory refresh is already running')
        try:
            at, records = inventory()
            if at is not None and at >= time.time() - 30 and all(
                    'size_bytes' in record for record in records):
                return {'checked': at, 'total': len(records)}
            records = Provider.from_config(settings['source']).authenticate().discover()
            entries = [{key: repo.get(key) for key in ('id', 'name', 'default_branch', 'visibility', 'size_bytes')} for repo in records]
            at = time.time()
            journal.write('INSERT OR REPLACE INTO inventory VALUES (?,?,?)', (journal.scope, at, json.dumps(entries)))
            return {'checked': at, 'total': len(entries)}
        except (SyncError, OSError, sqlite3.Error):
            raise HTTPException(502, 'Cannot read repository inventory; check source credentials and connectivity')
        finally:
            discovery_lock.release()

    @app.get('/api/backups')
    def backups(request: _GitMirrorWebRequest):
        auth(request)
        return {'destinations': local_data()}

    @app.get('/api/runs/{run_id}/log')
    def run_log(run_id: str, request: _GitMirrorWebRequest):
        auth(request)
        if not re.fullmatch(r'(?:[a-f0-9]{20}|[a-f0-9]{32})', run_id):
            raise HTTPException(404, 'Unknown run')
        runs = journal.read('SELECT id,state,progress,error FROM operations WHERE id=? AND scope=?',
                            (run_id, journal.scope))
        if not runs:
            raise HTTPException(404, 'Unknown run')
        log = ''
        if len(run_id) == 32:
            try:
                with (journal.logs / (run_id + '.log')).open('rb') as file:
                    file.seek(0, os.SEEK_END)
                    file.seek(max(0, file.tell() - 65536))
                    log = file.read(65536).decode('utf-8', 'replace')
            except FileNotFoundError:
                pass
        run = runs[0]
        run['progress'] = json.loads(run['progress']) if run['progress'] else None
        return {**run, 'log': log,
                'results': journal.read('SELECT repository,destination,state,detail,at FROM results '
                                        'WHERE run=? ORDER BY at LIMIT 1000', (run_id,))}

    @app.get('/api/events')
    async def events(request: _GitMirrorWebRequest):
        auth(request)

        async def stream():
            revision, last = None, time.monotonic()
            while session(request) and not await request.is_disconnected():
                current = journal.revision()
                if revision != current:
                    revision = current
                    yield f'data: {revision}\n\n'
                if time.monotonic() - last >= 15:
                    last = time.monotonic()
                    yield ': keepalive\n\n'
                await asyncio.sleep(2)
            if not session(request):
                yield 'event: expired\ndata: 0\n\n'
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no'})

    def execute(job_id, command, lock):
        code, error = 2, None
        try:
            environment = {**os.environ, 'GITMIRROR_WEB_JOB': job_id, 'PYTHONUNBUFFERED': '1'}
            if revision:
                environment['GITMIRROR_MANAGED_SSH_CONFIG'] = str(revision.ssh_config)
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=environment)
            code = process.wait()
            if code:
                error = 'CLI exited before recording a result'
        except OSError:
            error = 'Could not start or monitor the GitMirror CLI'
        finally:
            lock.release()
        try:
            journal.finish(job_id, code, error=error, only_unfinished=True)
        except (OSError, sqlite3.Error):
            pass

    def start(operation, mode, repository, targets, arguments):
        config = Path(config_file).resolve()
        try:
            unchanged = hashlib.sha256(config.read_bytes()).digest() == config_digest
        except OSError:
            unchanged = False
        if not unchanged:
            raise HTTPException(409, 'Configuration changed; validate it and restart the dashboard')
        job_id = uuid.uuid4().hex
        lock = FileLock(str(journal.directory / '.action.lock'), timeout=0, thread_local=False)
        try:
            lock.acquire()
        except Timeout:
            raise HTTPException(409, 'Another dashboard operation is running')
        try:
            journal.begin(job_id, operation, mode, 'web', repository or None, targets, state='queued')
            cli = [sys.executable, '-u', str(Path(__file__).resolve()), '--config', str(config), *arguments]
            threading.Thread(target=execute, args=(job_id, cli, lock), daemon=True).start()
        except Exception:
            lock.release()
            raise
        return {'id': job_id, 'state': 'queued', 'mode': mode}

    def require_action(request):
        auth(request, csrf=True, role='operator')
        if not allow_actions:
            raise HTTPException(403, 'Start the dashboard with --allow-actions')

    def selected(data, *, local=False):
        targets = data.get('destinations', [])
        if (not isinstance(targets, list) or len(targets) > len(settings['destinations'])
                or any(not isinstance(v, str) or v not in settings['destinations'] for v in targets)
                or (local and (len(targets) != 1 or settings['destinations'][targets[0]]['provider'] != 'local'))):
            raise HTTPException(400, 'Select configured destinations')
        return list(dict.fromkeys(targets))

    def repo_name(data):
        repo = data.get('repo', '')
        if not isinstance(repo, str) or (repo and not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repo)):
            raise HTTPException(400, 'Invalid repository name or ID')
        return repo

    def backup_args(data, targets):
        repository = repo_name(data)
        generation = data.get('generation', 'current')
        if not repository or not isinstance(generation, str) or (
                generation != 'current' and not BACKUP_GENERATION.fullmatch(generation + '.git')):
            raise HTTPException(400, 'Choose a repository and backup generation')
        return repository, generation, ['--generation', generation, '--destination', targets[0], '--monitor']

    @app.post('/api/sync')
    async def start_sync(request: _GitMirrorWebRequest):
        require_action(request)
        data = await body(request)
        if type(data.get('apply')) is not bool:
            raise HTTPException(400, 'An explicit sync mode is required')
        repository, targets = repo_name(data), selected(data)
        applied = data['apply']
        if applied and managed_writes_blocked(settings):
            raise HTTPException(409, 'Applied synchronization is paused for maintenance')
        required = 'SYNC' if repository else 'SYNC ALL'
        if applied and data.get('confirmation') != required:
            raise HTTPException(400, f'Type {required} to confirm this applied sync')
        mode = 'apply' if applied else 'dry-run'
        args = ['sync', '--monitor', f'--{mode}']
        if repository:
            args.append(f'--only={repository}')
        for target in targets:
            args.extend(('--destination', target))
        result = await asyncio.to_thread(start, 'sync', mode, repository, targets, args)
        journal.audit(acting(request), 'sync.' + mode, repository, result['id'])
        return result

    @app.post('/api/managed/check')
    async def check_connections(request: _GitMirrorWebRequest):
        require_action(request)
        if managed is None:
            raise HTTPException(404, 'Managed installation is unavailable')
        data = await body(request)
        repository = repo_name(data)
        ssh = data.get('ssh', True)
        if type(ssh) is not bool:
            raise HTTPException(400, 'Choose API or API and SSH verification')
        args = ['doctor', '--ssh' if ssh else '--network', '--monitor']
        if repository:
            args.append(f'--only={repository}')
        result = await asyncio.to_thread(start, 'doctor', 'check', repository, [], args)
        journal.audit(acting(request), 'connection.checked', repository, result['id'])
        return result

    @app.post('/api/backups/verify')
    async def verify(request: _GitMirrorWebRequest):
        require_action(request)
        data = await body(request)
        targets = selected(data, local=True)
        repository, _, options = backup_args(data, targets)
        args = ['verify-backup', f'--only={repository}', *options]
        result = await asyncio.to_thread(start, 'verify', 'verify', repository, targets, args)
        journal.audit(acting(request), 'backup.verified', repository, result['id'])
        return result

    @app.post('/api/backups/restore')
    async def restore(request: _GitMirrorWebRequest):
        auth(request, csrf=True, role='administrator_only')
        require_action(request)
        if restore_dir is None:
            raise HTTPException(403, 'Configure a restore directory on the server')
        data = await body(request)
        targets = selected(data, local=True)
        repository, _, options = backup_args(data, targets)
        name, archive = data.get('output'), data.get('include_archive', False)
        if data.get('confirmation') != 'RESTORE':
            raise HTTPException(400, 'Type RESTORE to confirm')
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name)
                or name.endswith('.') or type(archive) is not bool
                or re.fullmatch(r'CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]', name.split('.')[0], re.I)):
            raise HTTPException(400, 'Use a new directory name without a path')
        output = restore_dir / name
        if output.exists() or output.is_symlink() or output.resolve().parent != restore_dir:
            raise HTTPException(409, 'Restore output must be a new directory')
        args = ['restore', f'--repo={repository}', f'--output={output}', *options]
        if archive:
            args.append('--include-archive')
        result = await asyncio.to_thread(start, 'restore', 'restore', repository, targets, args)
        journal.audit(acting(request), 'backup.restored', repository, result['id'])
        return result

    return app


def backup_health(volumes):
    last = {}
    counts = {'verified': 0, 'unverified': 0, 'failed': 0}
    for volume in volumes:
        if volume['error']:
            counts['failed'] += 1
            continue
        for repository in volume['repositories']:
            if repository['error']:
                counts['failed'] += 1
            for generation in repository['generations']:
                if generation['current']:
                    counts[generation['integrity']] += 1
                if generation['integrity'] == 'verified' and generation['created']:
                    identifier = repository['id']
                    last[identifier] = max(last.get(identifier, 0), generation['created'])
    used = [volume['used'] for volume in volumes if volume['used'] is not None and not volume['error']]
    counts['logical_bytes'] = sum(used) if used and len(used) == len(volumes) else None
    counts['available_volumes'] = len(used)
    counts['configured_volumes'] = len(volumes)
    return last, counts


def urlsplit_origin(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or not re.fullmatch(r'(?:\[[a-fA-F0-9:]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?', parsed.netloc)
                or parsed.port == 0):
            return None
        return parsed
    except ValueError:
        return None


def serve_web(config_file, *, host='127.0.0.1', port=8787, password_file=None, allow_lan=False,
              allowed_hosts=None, allow_actions=False, restore_dir=None, tls_cert=None, tls_key=None,
              public_origin=None, allow_insecure_http=False, managed_dir=None):
    """Keep the dashboard optional and leave scheduling to the CLI."""
    loopback = host in ('127.0.0.1', '::1', 'localhost')
    if not loopback and not allow_lan:
        raise SyncError('Non-loopback binding requires --allow-lan')
    if not loopback and not (tls_cert or public_origin or allow_insecure_http):
        raise SyncError('Non-loopback binding requires TLS or explicit --allow-insecure-http')
    if bool(tls_cert) != bool(tls_key) or not 1 <= port <= 65535:
        raise SyncError('Provide both TLS files and a valid port')
    if managed_dir:
        if password_file or restore_dir:
            raise SyncError('Managed mode provides its own password and restore directory')
        return serve_managed_web(ManagedInstallation(managed_dir), host, port, allowed_hosts,
                                 tls_cert, tls_key, public_origin)
    if not password_file:
        raise SyncError('Set --password-file to a private file with a strong dashboard password')
    path = Path(password_file).expanduser()
    try:
        info = path.stat()
        if not path.is_file() or info.st_size > 8192 or (os.name != 'nt' and info.st_mode & 0o077):
            raise SyncError('Web password file must be a small regular file restricted to its owner (chmod 600)')
        password = path.read_text(encoding='utf-8').rstrip('\r\n')
    except (OSError, UnicodeError) as exc:
        raise SyncError('Cannot read dashboard password file') from exc
    settings = load_config(config_file)
    try:
        import uvicorn
    except ImportError as exc:
        raise SyncError('Install web dependencies: pip install "gitmirror-resilient[web]"') from exc
    app = create_web_app(settings, Path(config_file).expanduser().resolve(), password=password,
                         allow_actions=allow_actions, allowed_hosts=allowed_hosts, restore_dir=restore_dir,
                         public_origin=public_origin)
    print(f'GitMirror dashboard listening on {"https" if tls_cert else "http"}:{port} (actions '
          f'{"enabled" if allow_actions else "disabled"})')
    uvicorn.run(app, host=host, port=port, log_level='warning', proxy_headers=False,
                access_log=False, ssl_certfile=tls_cert, ssl_keyfile=tls_key, timeout_keep_alive=5)
    return 0


def serve_managed_web(installation, host, port, allowed_hosts, cert, key, public_origin):
    try:
        import uvicorn
    except ImportError as exc:
        raise SyncError('Install GitMirror with the web extra') from exc
    installation.public_key()
    while True:
        revision = installation.active()
        file = revision.config if revision else None
        settings = load_config(file) if revision else installation.placeholder()
        reload_pending = threading.Event()

        def reload_config():
            reload_pending.set()
            server.should_exit = True

        app = create_web_app(settings, file, password=None, allowed_hosts=allowed_hosts,
                             restore_dir=installation.restores, allow_actions=True, public_origin=public_origin,
                             managed=installation, revision=revision, reload_config=reload_config)
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level='warning',
                                               proxy_headers=False, access_log=False, ssl_certfile=cert,
                                               ssl_keyfile=key, timeout_graceful_shutdown=3))
        server.run()
        if not reload_pending.is_set():
            return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='gitmirror', description='Resilient Git mirroring')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', default='config.yml', help='Path to YAML config')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate', help='Validate YAML without connecting to providers')
    web = sub.add_parser('web', help='Start the optional self-hosted dashboard')
    web.add_argument('--host', default='127.0.0.1', help='Bind address (default: localhost)')
    web.add_argument('--port', type=int, default=8787)
    web.add_argument('--allow-lan', action='store_true', help='Allow a non-loopback bind address')
    web.add_argument('--allowed-host', action='append', help='Hostname or LAN IP permitted in Host header')
    web.add_argument('--password-file', required=False, help='Restricted file containing the dashboard password')
    web.add_argument('--allow-actions', action='store_true', help='Enable dashboard operations')
    web.add_argument('--restore-dir', help='Allow restoration under this existing directory')
    web.add_argument('--tls-cert', help='PEM TLS certificate')
    web.add_argument('--tls-key', help='PEM TLS private key')
    web.add_argument('--public-origin', help='HTTPS origin when serving behind a reverse proxy')
    web.add_argument('--allow-insecure-http', action='store_true', help='Allow explicit non-loopback HTTP')
    web.add_argument('--managed-dir', help='Managed installation state directory')
    provision = sub.add_parser('managed-init', help='Initialize managed installation state')
    provision.add_argument('--state-dir', required=True)
    scheduler = sub.add_parser('managed-scheduler', help='Run the managed installation scheduler')
    scheduler.add_argument('--state-dir', required=True)
    managed_check = sub.add_parser('managed-validate', help='Check a managed configuration')
    managed_check.add_argument('--state-dir', required=True)

    initialization = sub.add_parser('init-backup', help='Initialize a configured local backup volume')
    add_destination_selector(initialization)
    verification = sub.add_parser('verify-backup', help='Verify local backups and archives offline')
    verification.add_argument('--only', help='Verify the backup for one repository name')
    verification.add_argument('--generation', help='Verify a specific generation; requires --only')
    verification.add_argument('--monitor', action='store_true', help='Record the operation in dashboard activity')
    add_destination_selector(verification)
    recovery = sub.add_parser('restore', help='List or restore a verified generation offline')
    recovery.add_argument('--list', action='store_true', help='List available generations')
    recovery.add_argument('--repo', help='Repository name or ID')
    recovery.add_argument('--generation', default='current', help='Generation from the listing, or current')
    recovery.add_argument('--include-archive', action='store_true', help='Restore archived refs and their LFS data')
    recovery.add_argument('--output', help='New output directory for the restored bare repository')
    recovery.add_argument('--monitor', action='store_true', help='Record the operation in dashboard activity')
    add_destination_selector(recovery)
    cleanup = sub.add_parser('cleanup', help='Preview or apply configured local backup retention')
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument('--apply', action='store_true', help='Remove verified eligible generations')
    cleanup_mode.add_argument('--dry-run', action='store_true', help='Preview cleanup (default)')
    add_destination_selector(cleanup)
    health = sub.add_parser('doctor', help='Check dependencies, configuration, and backup storage')
    health.add_argument('--network', action='store_true', help='Check provider credentials and API access')
    health.add_argument('--ssh', action='store_true', help='Check provider credentials and SSH Git read access')
    health.add_argument('--only', help='Repository to sample for the SSH check')
    health.add_argument('--monitor', action='store_true', help='Record diagnostics in dashboard activity')
    add_destination_selector(health, providers=True)
    for name in ('sync', 'run'):
        command = sub.add_parser(name)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='Make changes (default: dry-run)')
        mode.add_argument('--dry-run', action='store_true', help='Plan changes without writing')
        command.add_argument('--only', help='Only one repository')
        command.add_argument('--monitor', action='store_true', help='Record dry-runs in dashboard activity')
        add_destination_selector(command, providers=True)
        command.add_argument('--verify-lfs', action='store_true', help='Recheck all archived and mirrored LFS objects (requires --apply)')
    args = parser.parse_args(argv)
    if args.command in ('sync', 'run') and args.verify_lfs and not args.apply:
        parser.error('--verify-lfs requires --apply')
    if args.command == 'restore' and not args.list and (not args.repo or not args.output):
        parser.error('restore requires --repo and --output, or --list')
    if args.command == 'restore' and args.list and args.output:
        parser.error('--list cannot be combined with --output')
    if args.command == 'verify-backup' and args.generation and not args.only:
        parser.error('--generation requires --only')
    try:
        if args.command == 'web':
            return serve_web(args.config, host=args.host, port=args.port,
                             password_file=args.password_file, allow_lan=args.allow_lan,
                             allowed_hosts=args.allowed_host, allow_actions=args.allow_actions,
                             restore_dir=args.restore_dir, tls_cert=args.tls_cert, tls_key=args.tls_key,
                             public_origin=args.public_origin, allow_insecure_http=args.allow_insecure_http,
                             managed_dir=args.managed_dir)
        if args.command == 'managed-init':
            ManagedInstallation(args.state_dir).initialize()
            return 0
        if args.command == 'managed-scheduler':
            return managed_scheduler(ManagedInstallation(args.state_dir))
        if args.command == 'managed-validate':
            return validate_managed(ManagedInstallation(args.state_dir))
        if args.command == 'doctor':
            def diagnose(activity):
                return doctor(args.config, network=args.network, ssh=args.ssh, only=args.only, platform=args.platform,
                              identifiers=args.destination)
            if not args.monitor:
                return diagnose(None)
            return observe_dashboard_operation(load_config(args.config), 'doctor', 'check', 'cli', args.only,
                                              args.destination, diagnose, enabled=True)
        cfg = load_config(args.config)
        if args.command == 'validate':
            print(f'Config valid: {Path(args.config).expanduser()}')
            return 0
        if args.command == 'init-backup':
            return initialize_backup(cfg, args.destination)
        if args.command in ('verify-backup', 'restore', 'cleanup'):
            configure_runtime(cfg, apply=False)
            SSH_ENV['GIT_ALLOW_PROTOCOL'] = 'file'
            SSH_ENV['GIT_NO_LAZY_FETCH'] = '1'
            if args.command == 'cleanup':
                return cleanup_backups(cfg, args.destination, apply=args.apply)
            if args.command == 'restore' and args.list:
                return select_restore_backup(cfg, args.destination, args.repo, args.generation, listing=True)
            origin = 'cron' if os.getenv('GITMIRROR_TRIGGER') == 'cron' else 'cli'
            if args.command == 'verify-backup':
                def verify(activity):
                    if args.generation:
                        return verify_backup_selection(cfg, args.destination, args.only, args.generation, activity=activity)
                    return verify_local_backups(cfg, args.only, args.destination, activity=activity)
                return observe_dashboard_operation(cfg, 'verify', 'verify', origin, args.only, args.destination,
                                                  verify, enabled=args.monitor)
            def recover(activity):
                return restore_backup(cfg, args.destination, args.repo, args.generation, args.output,
                                      args.include_archive, activity=activity)
            return observe_dashboard_operation(cfg, 'restore', 'restore', origin, args.repo, args.destination,
                                              recover, enabled=True)
        def execute(activity):
            return execute_sync(cfg, apply=args.apply, only=args.only, platform=args.platform,
                                full_lfs=args.verify_lfs, identifiers=args.destination, activity=activity)
        origin = ('scheduled' if args.command == 'run'
                  else 'cron' if os.getenv('GITMIRROR_TRIGGER') == 'cron' else 'cli')
        monitored = args.apply or args.monitor
        mode = 'apply' if args.apply else 'dry-run'
        while True:
            result = observe_dashboard_operation(cfg, 'sync', mode, origin, args.only,
                                                 args.destination, execute, enabled=monitored)
            if args.command == 'sync' or result == 130:
                return result
            print(f'Sync cycle finished with status {result}', flush=True)
            wait_for_dashboard_schedule(cfg, cfg['schedule']['interval_hours'] * 3600, monitored=monitored)
    except (ConfigError, SyncError, OSError, Timeout, subprocess.SubprocessError) as exc:
        print(f'GitMirror: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
