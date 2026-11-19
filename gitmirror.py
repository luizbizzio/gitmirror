"""GitMirror mirrors Git history across hosts and verifies local Git and LFS backups."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
import datetime as dt
import errno
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
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
}

class ConfigError(ValueError):
    pass


def _mapping(data, name):
    if not isinstance(data, dict):
        raise ConfigError(f'{name} must be a YAML mapping')
    return data


def _token_source(cfg, label):
    if 'token' in cfg:
        raise ConfigError(f'{label}: plaintext token in config is not supported; use token_env or token_file')
    keys = [k for k in ('token_env', 'token_file') if k in cfg]
    if len(keys) != 1 or not isinstance(cfg[keys[0]], str) or not cfg[keys[0]].strip():
        raise ConfigError(f'{label}: specify exactly one token_env or token_file')


def _url(value, name):
    try:
        parsed = urlparse(value) if isinstance(value, str) else None
        valid = (parsed and parsed.scheme == 'https' and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f'{name} must be an HTTPS API URL without credentials or query parameters')
    return value.rstrip('/')


def provider_config(spec, label, *, owner_optional=False):
    cfg = dict(_mapping(spec, label))
    provider = cfg.get('provider')
    if not isinstance(provider, str) or provider not in PROVIDER_APIS:
        raise ConfigError(f'{label}.provider must be github, gitlab or codeberg')
    owner = cfg.get('owner')
    if not (owner_optional and owner is None):
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', owner) or owner in ('.', '..'):
            raise ConfigError(f'{label}.owner must be a provider account name')
    _token_source(cfg, label)
    cfg['api_url'] = _url(cfg.get('api_url', PROVIDER_APIS[provider]), f'{label}.api_url')
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
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f'Cannot load config {file}: {exc}') from exc
    raw = _mapping(raw, 'config')
    version = raw.get('version')
    if type(version) is not int or version not in (1, 2):
        raise ConfigError('version must be 1 or 2')
    source = provider_config(raw.get('source'), 'source')
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
            cfg = provider_config(cfg, label, owner_optional=version == 1 and platform == 'gitlab')
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
    return {'version': version, 'source': source, 'destinations': dests,
            'storage': {'path': work_path}, 'retry': retry, 'schedule': schedule}


# Runtime, credentials, and transports

HOME = Path.home()
ROOT = HOME / 'gitmirror-data'
GL_API = 'https://gitlab.com/api/v4'
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
SSH_ENV = dict(os.environ)
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
    global JOURNAL_DIR, RETRY_DELAYS, RUN_ID, SSH_ENV
    ROOT = settings['storage']['path']
    GL_API = PROVIDER_APIS['gitlab']
    JOURNAL_DIR = ROOT / 'gitlab-protection-journal'
    RETRY_DELAYS = tuple(settings['retry']['delays_seconds'])
    RUN_ID = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    SSH_ENV = dict(os.environ)
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_NAMESPACE',
                'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
        SSH_ENV.pop(key, None)
    ssh = 'ssh -o BatchMode=yes -o ConnectTimeout=20'
    if apply:
        SSH_ENV.setdefault('GIT_SSH_COMMAND', ssh)
    else:
        SSH_ENV['GIT_SSH_COMMAND'] = (ssh + ' -o StrictHostKeyChecking=yes -o UpdateHostKeys=no'
                                     ' -o ControlPath=none -o AddKeysToAgent=no -o PermitLocalCommand=no')
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


def retry_notice(target, attempt, delay, reason):
    print(f'    RETRY {attempt}/{len(RETRY_DELAYS)} in {delay}s [{target}]: {reason}', flush=True)
    time.sleep(delay)


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        old, new = urlparse(request.full_url), urlparse(new_url)
        if (request.get_method() != 'GET' or old.scheme != new.scheme
                or old.hostname != new.hostname or old.port != new.port
                or new.username is not None or new.password is not None):
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
    for attempt in range(len(RETRY_DELAYS) + 1):
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
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
            raise SyncError(f'API HTTP {e.code}: {url}') from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if retryable and attempt < len(RETRY_DELAYS):
                retry_notice('API', attempt + 1, RETRY_DELAYS[attempt], type(e).__name__)
                continue
            raise SyncError(f'API unavailable: {url}: {e}') from e
        except json.JSONDecodeError as e:
            raise SyncError(f'API invalid JSON: {url}: {e}') from e


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
            raise SyncError(f'Command timed out: {args[0]} ({e})') from e
        except OSError as e:
            raise SyncError(f'Command failed to run: {args[0]} ({e})') from e
        if check and p.returncode:
            err = (p.stderr or p.stdout or '').strip()[-1400:]
            transient = args[0] == 'git' and any(x in err.lower() for x in TRANSIENT_GIT_ERRORS)
            if transient and attempt + 1 < attempts:
                retry_notice('Git', attempt + 1, RETRY_DELAYS[attempt],
                             err.splitlines()[0][:120] if err else 'SSH network failure')
                continue
            raise SyncError(f'Command failed (exit={p.returncode}): {" ".join(args[:4])}\n{err}')
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
        raise SyncError(f'Push failed ({state}): {(result.stderr or result.stdout).strip()[-900:]}')
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
        return {'github': 'GitHub', 'gitlab': 'GitLab', 'codeberg': 'Codeberg'}[self.kind]

    @classmethod
    def from_config(cls, spec):
        kind = spec['provider']
        secret = token(spec)
        if kind == 'gitlab':
            headers = {'PRIVATE-TOKEN': secret}
        elif kind == 'codeberg':
            headers = {'Authorization': f'token {secret}'}
        else:
            headers = {'Authorization': f'Bearer {secret}', 'X-GitHub-Api-Version': '2022-11-28'}
        return cls(kind, spec['api_url'], spec.get('owner'), headers)

    def authenticate(self):
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
        if type(info.get('id')) is not int or not all(isinstance(value, str) and value
                                                     for value in (owner, name, url)):
            raise SyncError(f'{self.label} repository identity changed or is invalid')
        branch = info.get('default_branch')
        return {'id': info['id'], 'name': name, 'owner': owner, 'ssh_url': url,
                'private': visibility != 'public', 'visibility': visibility, 'default_branch': branch}

    def discover(self):
        repos = {}
        query = {
            'github': '/user/repos?affiliation=owner&visibility=all&per_page=100',
            'gitlab': '/projects?owned=true&per_page=100',
            'codeberg': '/user/repos?limit=50',
        }[self.kind]
        seen = set()
        for page in range(1, 101):
            status, items = api(f'{self.base}{query}&page={page}', self.headers)
            if status != 200 or not isinstance(items, list):
                raise SyncError(f'{self.label} returned an invalid repository list')
            for item in items:
                record = self.repo_state(item)
                if record['id'] in seen:
                    raise SyncError(f'{self.label} discovery returned duplicate repository IDs')
                seen.add(record['id'])
                if record['owner'].casefold() != self.owner.casefold():
                    continue
                name = record['name'].casefold()
                if name in repos:
                    raise SyncError(f'{self.label} discovery returned duplicate repository names')
                repos[name] = record
            # A Forgejo instance can cap page size below the requested limit.
            if not items or (self.kind != 'codeberg' and len(items) < 100):
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
        status, obj = api(self.repo_url(name), {**self.headers, 'Cache-Control': 'no-cache'})
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
                raise SyncError(f'Cannot create private {self.kind} destination for {name}')
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
        if status == 201 and visibility != 'private':
            raise SyncError(f'New {self.kind} destination is not confirmed private for {name}')
        default_branch = obj.get('default_branch')
        if (type(obj.get('id')) is not int or 'default_branch' not in obj
                or (default_branch is not None and not isinstance(default_branch, str))):
            raise SyncError(f'Cannot verify {self.kind} destination identity or default branch for {name}')
        return Destination(obj['id'], url, visibility, default_branch or None)

    def update_destination(self, name, destination, **change):
        """Write once and confirm the result even if the response was ambiguous."""
        expected = replace(destination, **change)
        if self.kind == 'gitlab':
            url, method = f'{self.base}/projects/{destination.id}', 'PUT'
        else:
            url, method = self.repo_url(name), 'PATCH'
        payload = change
        if self.kind == 'codeberg' and 'visibility' in change:
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
                    return
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


def verify_local_backups(settings, only, identifiers=None):
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
                verify_local_generation(repo, manifest)
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


def restore_backup(settings, identifiers, only, generation, output_path, include_archive):
    name, repo, manifest = select_restore_backup(settings, identifiers, only, generation, listing=False)
    output = Path(output_path).expanduser().absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise SyncError('Restore requires a new output directory under an existing parent')
    if any(output.resolve().is_relative_to(spec['path']) for spec in local_destinations(settings).values()):
        raise SyncError('Restore output must be outside all configured backup volumes')

    verify_local_generation(repo, manifest)
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


def sync_repository(record, source, providers, volumes, settings, apply, *, full_lfs=False):
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
    if not apply:
        for identifier, (path, _) in volumes.items():
            print(f'  {identifier}: would verify and preserve {len(gh)} refs at {path}')
            successes += 1
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
                continue
            if priv and destination.visibility != 'private':
                source.confirm_source(record)
                destination = provider.update_destination(name, destination, visibility='private')
            destinations[identifier] = destination
        except SourceStateError as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            return successes, active - successes
        except (SyncError, OSError) as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            failures += 1
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
            preserve_local_backup(path, volume, scope, source, record, repo, src, source_lfs,
                                  identifier=identifier, retention=settings['destinations'][identifier]['retention'])
            successes += 1
        except SourceStateError as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            return successes, active - successes
        except (SyncError, OSError) as exc:
            print(f'  {identifier}: FAILED: {exc}')
            failures += 1
    for identifier, destination in destinations.items():
        try:
            with protection_scope(settings, settings['destinations'][identifier]):
                sync_destination(record, source, repo, providers[identifier], destination, src, source_lfs,
                                 full_lfs, identifier=identifier)
            successes += 1
        except SourceStateError as exc:
            print(f'  {identifier}: BLOCKED: {exc}')
            return successes, active - successes
        except (SyncError, OSError) as exc:
            print(f'  {identifier}: FAILED without deleting project: {exc}')
            failures += 1
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


def run(settings, *, apply=False, only=None, platform='all', full_lfs=False, identifiers=None):
    configure_runtime(settings, apply=apply)
    for executable in ('git', 'ssh', 'git-lfs'):
        if shutil.which(executable) is None:
            raise SyncError(f'Missing executable: {executable}')
    platforms = selected_platforms(settings, platform, identifiers)
    source = Provider.from_config(settings['source'])
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
            provider = Provider.from_config(spec).authenticate()
            if apply and provider.kind == 'gitlab':
                with protection_scope(settings, spec):
                    recover_all_gl_permissions(provider.headers)
            providers[target] = provider
        except (SyncError, OSError) as exc:
            print(f'{target}: BLOCKED: {exc}')
            failures += 1
    active = len(providers) + len(volumes)
    if not active:
        print(f'SUMMARY: repositories=0, successful/checked destinations=0, failed={failures}')
        return 1

    repos = source.discover()
    print(f'{source.label}: {source.owner} -> {", ".join(platforms)}')
    print(f'Mode: {"APPLY" if apply else "DRY RUN"} | Discovered: {len(repos)} | Run: {RUN_ID}')
    if only:
        repos = [repo for repo in repos if repo['name'].casefold() == only.casefold()]
        if not repos:
            raise SyncError(f'{only} not found among accessible {source.label} repositories; no changes made')
    for n, repo in enumerate(repos, 1):
        print(f'\n[{n}/{len(repos)}] {repo["name"]}')
        try:
            ok, bad = sync_repository(repo, source, providers, volumes, settings, apply, full_lfs=full_lfs)
            successes += ok
            failures += bad
        except (SyncError, OSError) as exc:
            print(f'  FAILED safely: {exc}')
            failures += active
    print(f'\nSUMMARY: repositories={len(repos)}, successful/checked destinations={successes}, '
          f'failed={failures}, mode={"apply" if apply else "dry-run"}')
    print('Missing source repositories are never deleted from destinations.')
    return 0 if failures == 0 else 1


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
            provider = Provider.from_config(spec)
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


# Optional self-hosted web dashboard. The sync engine stays in this one Python file.
def create_web_app(settings, config_file, *, password, allow_actions=False,
                   assets_dir=None, allowed_hosts=None):
    """Build an authenticated, local-first dashboard without a second sync engine."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import FileResponse, StreamingResponse
    except ImportError as exc:
        raise SyncError('Web dashboard requires: pip install "gitmirror-resilient[web]"') from exc
    import asyncio
    import base64
    from contextlib import closing
    import hmac
    import secrets
    import sqlite3
    import threading

    # FastAPI resolves postponed endpoint annotations against module globals.
    globals()['_GitMirrorWebRequest'] = Request
    root = Path(assets_dir) if assets_dir else Path(__file__).resolve().parent / 'web'
    if not (root / 'index.html').is_file():
        root = Path(sys.prefix) / 'share' / 'gitmirror' / 'web'
    if not all((root / name).is_file() for name in ('index.html', 'style.css', 'app.js')):
        raise SyncError('Web assets are missing; install the web extra or run from the repository checkout')
    if not isinstance(password, str) or len(password) < 12:
        raise SyncError('Web password must contain at least 12 characters')

    state_dir = settings['storage']['path'] / 'web-dashboard'
    logs_dir = state_dir / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'nt':
        os.chmod(state_dir, 0o700)
        os.chmod(logs_dir, 0o700)
    database = state_dir / 'runs.sqlite3'
    secret = secrets.token_bytes(32)
    password_hash = hashlib.sha256(password.encode('utf-8')).digest()
    active_lock = threading.Lock()
    active = {'id': None}
    attempts_lock = threading.Lock()
    attempts = {}
    repo_cache = {'at': 0, 'rows': None}
    repo_cache_lock = threading.Lock()
    allowed = {'localhost', '127.0.0.1', '::1'}
    allowed.update(str(name).casefold() for name in (allowed_hosts or ()))
    app = FastAPI(title='GitMirror Dashboard', docs_url=None, redoc_url=None, openapi_url=None)

    def connection():
        db = sqlite3.connect(database, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    with closing(connection()) as db:
        db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started REAL NOT NULL, '
                   'ended REAL, mode TEXT NOT NULL, repository TEXT, destinations TEXT NOT NULL, '
                   'state TEXT NOT NULL, exit_code INTEGER, error TEXT)')
        db.execute("UPDATE runs SET state='interrupted', ended=? WHERE state='running'",
                   (time.time(),))
        db.commit()
    if os.name != 'nt':
        os.chmod(database, 0o600)

    def write_db(sql, params):
        with closing(connection()) as db:
            db.execute(sql, params)
            db.commit()

    def read_db(sql, params=()):
        with closing(connection()) as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def rows():
        output = read_db('SELECT * FROM runs ORDER BY started DESC LIMIT 40')
        for record in output:
            record['destinations'] = json.loads(record['destinations'])
        return output

    def session(request):
        cookie = request.cookies.get('gm_session', '')
        try:
            payload, signature = cookie.rsplit('.', 1)
            expected = hmac.new(secret, payload.encode('ascii'), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return None
            raw = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
            timestamp, nonce = raw.decode('ascii').split(':', 1)
            if not nonce or not re.fullmatch(r'[a-f0-9]{32}', nonce):
                return None
            age = time.time() - int(timestamp)
            if not 0 <= age < 12 * 3600:
                return None
            return payload
        except (ValueError, UnicodeError, TypeError):
            return None

    def authenticated(request):
        value = session(request)
        if not value:
            raise HTTPException(status_code=401, detail='Sign in required')
        return value

    def csrf_check(request):
        payload = authenticated(request)
        expected = hmac.new(secret, ('csrf:' + payload).encode('ascii'), hashlib.sha256).hexdigest()
        actual = request.headers.get('X-CSRF-Token', '')
        if not hmac.compare_digest(expected, actual):
            raise HTTPException(status_code=403, detail='Invalid session verification token')
        origin = request.headers.get('origin')
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if (parsed.scheme not in ('http', 'https')
                    or parsed.netloc.casefold() != request.headers.get('host', '').casefold()):
                raise HTTPException(status_code=403, detail='Cross-origin action blocked')

    @app.middleware('http')
    async def security(request: _GitMirrorWebRequest, call_next):
        host = (request.url.hostname or '').casefold()
        if host not in allowed:
            from fastapi.responses import JSONResponse
            return JSONResponse({'detail': 'Unapproved dashboard hostname'}, status_code=400)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.get('/')
    async def home():
        return FileResponse(root / 'index.html', media_type='text/html')

    @app.get('/assets/{filename}')
    async def asset(filename: str):
        media = {'style.css': 'text/css', 'app.js': 'text/javascript'}
        if filename not in media:
            raise HTTPException(status_code=404, detail='Asset not found')
        return FileResponse(root / filename, media_type=media[filename])

    @app.post('/api/login')
    async def login(request: _GitMirrorWebRequest):
        ip = request.client.host if request.client else 'unknown'
        now = time.monotonic()
        with attempts_lock:
            recent = [t for t in attempts.get(ip, ()) if now - t < 300]
            if len(recent) >= 5:
                raise HTTPException(status_code=429, detail='Too many attempts. Try again later.')
            attempts[ip] = recent
        try:
            data = await request.json()
        except ValueError:
            data = {}
        entered = data.get('password') if isinstance(data, dict) else None
        valid = isinstance(entered, str) and hmac.compare_digest(
            hashlib.sha256(entered.encode('utf-8')).digest(), password_hash)
        if not valid:
            with attempts_lock:
                attempts[ip].append(now)
            raise HTTPException(status_code=401, detail='Invalid password')
        with attempts_lock:
            attempts.pop(ip, None)
        payload = base64.urlsafe_b64encode(
            f'{int(time.time())}:{secrets.token_hex(16)}'.encode('ascii')).decode('ascii').rstrip('=')
        signed = hmac.new(secret, payload.encode('ascii'), hashlib.sha256).hexdigest()
        from fastapi.responses import JSONResponse
        response = JSONResponse({'authenticated': True})
        response.set_cookie('gm_session', payload + '.' + signed, httponly=True,
                            samesite='strict', max_age=43200, path='/',
                            secure=request.url.scheme == 'https')
        return response

    @app.get('/api/session')
    async def get_session(request: _GitMirrorWebRequest):
        payload = authenticated(request)
        verification = hmac.new(secret, ('csrf:' + payload).encode('ascii'), hashlib.sha256).hexdigest()
        return {'authenticated': True, 'csrf': verification, 'allow_actions': allow_actions}

    @app.post('/api/logout')
    async def logout(request: _GitMirrorWebRequest):
        csrf_check(request)
        from fastapi.responses import JSONResponse
        response = JSONResponse({'ok': True})
        response.delete_cookie('gm_session', path='/')
        return response

    def overview_data():
        recent = rows()
        with active_lock:
            running = active['id']
        destinations = []
        for identifier, spec in settings['destinations'].items():
            destinations.append({'id': identifier, 'provider': spec['provider'],
                                 'owner': spec.get('owner'), 'path': str(spec['path']) if spec['provider'] == 'local' else None,
                                 'api_url': spec.get('api_url')})
        try:
            usage = shutil.disk_usage(settings['storage']['path'])
            free = usage.free
        except OSError:
            free = None
        return {'source': {'provider': settings['source']['provider'], 'owner': settings['source']['owner']},
                'destinations': destinations, 'schedule_hours': settings['schedule']['interval_hours'],
                'storage': {'path': str(settings['storage']['path']), 'free': free},
                'web': {'allow_actions': allow_actions},
                'runs': recent, 'latest': recent[0] if recent else None,
                'active': {'id': running} if running else None}

    @app.get('/api/overview')
    async def overview(request: _GitMirrorWebRequest):
        authenticated(request)
        return overview_data()

    @app.get('/api/repositories')
    async def repositories(request: _GitMirrorWebRequest):
        authenticated(request)
        def discover():
            with repo_cache_lock:
                if repo_cache['rows'] is not None and time.time() - repo_cache['at'] < 60:
                    return repo_cache['rows']
                provider = Provider.from_config(settings['source']).authenticate()
                records = provider.discover()
                result = [{'name': r['name'], 'visibility': r['visibility'],
                           'default_branch': r['default_branch']} for r in records]
                repo_cache.update({'rows': result, 'at': time.time()})
                return result
        try:
            records = await asyncio.to_thread(discover)
        except (SyncError, OSError) as exc:
            raise HTTPException(status_code=502, detail=f'Cannot read source repositories: {exc}') from exc
        return {'repositories': records}

    @app.get('/api/backups')
    async def backups(request: _GitMirrorWebRequest):
        authenticated(request)
        output = []
        def check():
            for identifier, spec in settings['destinations'].items():
                if spec['provider'] != 'local':
                    continue
                item = {'id': identifier, 'path': str(spec['path']),
                        'repositories': 0, 'generations': 0, 'free': None, 'error': None}
                try:
                    backup_volume(spec['path'])
                    for directory in backup_directories(spec['path'], scope_id(settings['source'])):
                        current = current_backup(directory)
                        if current:
                            item['repositories'] += 1
                            item['generations'] += len(backup_generations(directory))
                    item['free'] = shutil.disk_usage(spec['path']).free
                except (SyncError, OSError) as exc:
                    item['error'] = str(exc)
                output.append(item)
        await asyncio.to_thread(check)
        return {'destinations': output}

    @app.get('/api/runs/{run_id}/log')
    async def run_log(run_id: str, request: _GitMirrorWebRequest):
        authenticated(request)
        if not re.fullmatch(r'[0-9a-f]{20}', run_id) or not read_db('SELECT id FROM runs WHERE id=?', (run_id,)):
            raise HTTPException(status_code=404, detail='Unknown run')
        filename = logs_dir / (run_id + '.log')
        try:
            with open(filename, 'rb') as file:
                file.seek(0, os.SEEK_END)
                file.seek(max(0, file.tell() - 160000))
                content = file.read().decode('utf-8', 'replace')
        except FileNotFoundError:
            content = ''
        return {'log': content}

    @app.get('/api/events')
    async def events(request: _GitMirrorWebRequest):
        authenticated(request)
        async def stream():
            while not await request.is_disconnected():
                with active_lock:
                    run_id = active['id']
                yield 'data: ' + json.dumps({'active': run_id, 'at': time.time()}) + '\n\n'
                await asyncio.sleep(2.5)
        return StreamingResponse(stream(), media_type='text/event-stream',
                                 headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-store'})

    def execute(job_id, command):
        error = None
        code = 1
        try:
            with (logs_dir / (job_id + '.log')).open('w', encoding='utf-8') as log:
                if os.name != 'nt':
                    os.chmod(logs_dir / (job_id + '.log'), 0o600)
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding='utf-8', errors='replace', bufsize=1)
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                code = process.wait()
                error = None if code == 0 else f'GitMirror exited with code {code}'
        except (OSError, ValueError) as exc:
            error = f'Cannot start or monitor the CLI: {exc}'
        finally:
            try:
                write_db('UPDATE runs SET ended=?, state=?, exit_code=?, error=? WHERE id=?',
                         (time.time(), 'succeeded' if code == 0 and not error else 'failed', code, error, job_id))
            finally:
                with active_lock:
                    if active['id'] == job_id:
                        active['id'] = None

    @app.post('/api/sync')
    async def start_sync(request: _GitMirrorWebRequest):
        csrf_check(request)
        if not allow_actions:
            raise HTTPException(status_code=403, detail='Start the web service with --allow-actions')
        try:
            data = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail='Invalid JSON request')
        if not isinstance(data, dict) or type(data.get('apply')) is not bool:
            raise HTTPException(status_code=400, detail='An explicit sync mode is required')
        repository = data.get('repo') or ''
        if not isinstance(repository, str) or (repository and not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', repository)):
            raise HTTPException(status_code=400, detail='Invalid repository name')
        destinations = data.get('destinations', [])
        if (not isinstance(destinations, list) or any(not isinstance(d, str) or d not in settings['destinations']
                                                     for d in destinations) or len(destinations) > len(settings['destinations'])):
            raise HTTPException(status_code=400, detail='Select configured destinations only')
        destinations = list(dict.fromkeys(destinations))
        apply = data['apply']
        required = 'SYNC' if repository else 'SYNC ALL'
        if apply and data.get('confirmation') != required:
            raise HTTPException(status_code=400, detail=f'Type {required} to confirm this applied sync')
        command = [sys.executable, str(Path(__file__).resolve()), '--config', str(config_file),
                   'sync', '--apply' if apply else '--dry-run']
        if repository:
            command += ['--only', repository]
        for destination in destinations:
            command += ['--destination', destination]
        job_id = secrets.token_hex(10)
        with active_lock:
            if active['id'] is not None:
                raise HTTPException(status_code=409, detail='Another dashboard job is running')
            active['id'] = job_id
            try:
                write_db('INSERT INTO runs (id, started, mode, repository, destinations, state) '
                         'VALUES (?, ?, ?, ?, ?, ?)',
                         (job_id, time.time(), 'apply' if apply else 'dry-run', repository or None,
                          json.dumps(destinations), 'running'))
                threading.Thread(target=execute, args=(job_id, command), daemon=True,
                                 name='gitmirror-web-' + job_id).start()
            except Exception:
                active['id'] = None
                raise
        return {'id': job_id, 'state': 'running', 'mode': 'apply' if apply else 'dry-run'}

    return app


def serve_web(config_file, *, host='127.0.0.1', port=8787, password_file=None,
              allow_lan=False, allowed_hosts=None, allow_actions=False):
    """Web is optional. It never changes the existing CLI scheduler."""
    loopback = host in ('127.0.0.1', '::1', 'localhost')
    if not loopback and not allow_lan:
        raise SyncError('Non-loopback binding needs --allow-lan and an explicit password')
    if not 1 <= port <= 65535:
        raise SyncError('Invalid web port')
    if not password_file:
        raise SyncError('Set --password-file to a private file with a strong dashboard password')
    path = Path(password_file).expanduser()
    try:
        info = path.stat()
        if not path.is_file() or (os.name != 'nt' and info.st_mode & 0o077):
            raise SyncError('Web password file must be a regular file restricted to its owner (chmod 600)')
        password = path.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise SyncError(f'Cannot read dashboard password file: {exc}') from exc
    settings = load_config(config_file)
    app = create_web_app(settings, Path(config_file).expanduser().resolve(),
                         password=password, allow_actions=allow_actions, allowed_hosts=allowed_hosts)
    try:
        import uvicorn
    except ImportError as exc:
        raise SyncError('Install web dependencies: pip install "gitmirror-resilient[web]"') from exc
    if not loopback:
        print('WARNING: LAN access is plain HTTP. Use a trusted LAN, VPN or HTTPS reverse proxy.')
    print(f'GitMirror dashboard listening on http://{host}:{port} (actions={"enabled" if allow_actions else "disabled"})')
    uvicorn.run(app, host=host, port=port, log_level='warning', proxy_headers=False)
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
    web.add_argument('--allow-actions', action='store_true', help='Enable manual dry-run and applied syncs')

    initialization = sub.add_parser('init-backup', help='Initialize a configured local backup volume')
    add_destination_selector(initialization)
    verification = sub.add_parser('verify-backup', help='Verify local backups and archives offline')
    verification.add_argument('--only', help='Verify the backup for one repository name')
    add_destination_selector(verification)
    recovery = sub.add_parser('restore', help='List or restore a verified generation offline')
    recovery.add_argument('--list', action='store_true', help='List available generations')
    recovery.add_argument('--repo', help='Repository name or ID')
    recovery.add_argument('--generation', default='current', help='Generation from the listing, or current')
    recovery.add_argument('--include-archive', action='store_true', help='Restore archived refs and their LFS data')
    recovery.add_argument('--output', help='New output directory for the restored bare repository')
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
    add_destination_selector(health, providers=True)
    for name in ('sync', 'run'):
        command = sub.add_parser(name)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='Make changes (default: dry-run)')
        mode.add_argument('--dry-run', action='store_true', help='Plan changes without writing')
        command.add_argument('--only', help='Only one repository')
        add_destination_selector(command, providers=True)
        command.add_argument('--verify-lfs', action='store_true', help='Recheck all archived and mirrored LFS objects (requires --apply)')
    args = parser.parse_args(argv)
    if args.command in ('sync', 'run') and args.verify_lfs and not args.apply:
        parser.error('--verify-lfs requires --apply')
    if args.command == 'restore' and not args.list and (not args.repo or not args.output):
        parser.error('restore requires --repo and --output, or --list')
    if args.command == 'restore' and args.list and args.output:
        parser.error('--list cannot be combined with --output')
    try:
        if args.command == 'web':
            return serve_web(args.config, host=args.host, port=args.port,
                             password_file=args.password_file, allow_lan=args.allow_lan,
                             allowed_hosts=args.allowed_host, allow_actions=args.allow_actions)
        if args.command == 'doctor':
            return doctor(args.config, network=args.network, ssh=args.ssh, only=args.only, platform=args.platform,
                          identifiers=args.destination)
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
            if args.command == 'verify-backup':
                return verify_local_backups(cfg, args.only, args.destination)
            if args.command == 'cleanup':
                return cleanup_backups(cfg, args.destination, apply=args.apply)
            if args.list:
                return select_restore_backup(cfg, args.destination, args.repo, args.generation, listing=True)
            return restore_backup(cfg, args.destination, args.repo, args.generation, args.output, args.include_archive)
        def execute():
            if not args.apply:
                return run(cfg, only=args.only, platform=args.platform, identifiers=args.destination)
            data = cfg['storage']['path']
            data.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(data / '.gitmirror.lock'), timeout=0)
            try:
                with lock:
                    return run(cfg, apply=True, only=args.only, platform=args.platform, full_lfs=args.verify_lfs,
                               identifiers=args.destination)
            except Timeout:
                print('Another GitMirror process is running; no changes made.', file=sys.stderr)
                return 2
        if args.command == 'sync':
            return execute()
        while True:
            result = execute()
            print(f'Sync cycle finished with status {result}', flush=True)
            time.sleep(cfg['schedule']['interval_hours'] * 3600)
    except (ConfigError, SyncError, OSError, Timeout) as exc:
        print(f'GitMirror: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
