"""GitMirror mirrors Git history across hosts and verifies local Git and LFS backups."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
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
    if not isinstance(value, str) or urlparse(value).scheme != 'https' or not urlparse(value).netloc:
        raise ConfigError(f'{name} must be an HTTPS URL')
    return value.rstrip('/')


def load_config(path):
    file = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(file.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f'Cannot load config {file}: {exc}') from exc
    raw = _mapping(raw, 'config')
    if raw.get('version') != 1:
        raise ConfigError('version must be 1')
    source = _mapping(raw.get('source'), 'source')
    if source.get('provider') != 'github':
        raise ConfigError('source.provider currently supports github only')
    if not isinstance(source.get('owner'), str) or not source['owner'].strip():
        raise ConfigError('source.owner is required')
    _token_source(source, 'source')
    source['api_url'] = _url(source.get('api_url', 'https://api.github.com'), 'source.api_url')
    dests = _mapping(raw.get('destinations'), 'destinations')
    for provider, default_url in (('gitlab', 'https://gitlab.com/api/v4'),
                                  ('codeberg', 'https://codeberg.org/api/v1')):
        cfg = _mapping(dests.get(provider), f'destinations.{provider}')
        _token_source(cfg, f'destinations.{provider}')
        cfg['api_url'] = _url(cfg.get('api_url', default_url), f'destinations.{provider}.api_url')
    if not isinstance(dests['codeberg'].get('owner'), str) or not dests['codeberg']['owner'].strip():
        raise ConfigError('destinations.codeberg.owner is required')
    storage = _mapping(raw.get('storage'), 'storage')
    pathstr = storage.get('path')
    if not isinstance(pathstr, str) or not pathstr.strip():
        raise ConfigError('storage.path must be a non-empty path')
    storage['path'] = Path(os.path.expandvars(pathstr)).expanduser().resolve()
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
    return {'version': 1, 'source': source, 'destinations': dests,
            'storage': storage, 'retry': retry, 'schedule': schedule}


# Runtime, credentials, and transports

HOME = Path.home()
ROOT = HOME / 'gitmirror-data'
GH_OWNER = ''
CB_OWNER = ''
GH_API = 'https://api.github.com'
GL_API = 'https://gitlab.com/api/v4'
CB_API = 'https://codeberg.org/api/v1'
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
    """Configure legacy engine from validated config without touching live data."""
    global HOME, ROOT, GH_OWNER, CB_OWNER, GH_API, GL_API, CB_API
    global JOURNAL_DIR, RETRY_DELAYS, RUN_ID, SSH_ENV
    HOME = Path.home()
    ROOT = settings['storage']['path']
    GH_OWNER = settings['source']['owner']
    CB_OWNER = settings['destinations']['codeberg']['owner']
    GH_API = settings['source']['api_url'].rstrip('/')
    GL_API = settings['destinations']['gitlab']['api_url'].rstrip('/')
    CB_API = settings['destinations']['codeberg']['api_url'].rstrip('/')
    JOURNAL_DIR = ROOT / 'gitlab-protection-journal'
    RETRY_DELAYS = tuple(settings['retry']['delays_seconds'])
    RUN_ID = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
    SSH_ENV = dict(os.environ)
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
            with urllib.request.urlopen(req, timeout=45) as res:
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


def cmd(args, *, check=True, cwd=None, timeout=420, input_text=None, env=None, retry=True):
    """Retry Git commands only when the failure looks like a transport issue."""
    # Git's stdin protocols require LF even on Windows.
    input_bytes = None if input_text is None else input_text.encode('utf-8', 'surrogateescape')
    attempts = len(RETRY_DELAYS) + 1 if retry else 1
    for attempt in range(attempts):
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


def git(*args, repo=None, check=True, timeout=420, input_text=None, env=None, retry=True):
    a = ['git']
    if repo is not None:
        a += ['-C', str(repo)]
    a += list(args)
    return cmd(a, check=check, timeout=timeout, input_text=input_text, env=env, retry=retry)


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
    return parse_refs(git('ls-remote', '--heads', '--tags', url, timeout=100).stdout)


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
def lfs_snapshot(repo, refs):
    """Share Git objects with an isolated repository containing only these refs."""
    try:
        with tempfile.TemporaryDirectory(prefix='.lfs-check-', dir=repo) as directory:
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


def lfs_objects(repo, refs):
    """List LFS objects in the full history of these refs, excluding other backups."""
    if not refs:
        return {}
    with lfs_snapshot(repo, refs) as (snapshot, env):
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


def archive_source_old_tips(repo, previous):
    """Keep each previously observed GitHub head/tag object reachable forever."""
    for sha in set(previous.values()):
        ref = f'refs/archive/github/{sha}'
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


def push_snapshot(repo, remote, source, previous, *, url=None):
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
    try:
        result = git('push', '--atomic', *arguments, repo=repo, env=env,
                     check=False, retry=False, timeout=1800)
        if result.returncode and 'the receiving end does not support --atomic push' in result.stderr:
            if len(changed) > 1:
                raise SyncError('Destination lacks atomic push support; multiple refs were left untouched')
            if remote_refs(url) != previous:
                raise SyncError('Destination changed before the single-ref push')
            result = git('push', '--no-atomic', *arguments, repo=repo, env=env,
                         check=False, retry=False, timeout=1800)
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

def discover_repos(gh_headers):
    repos, page = [], 1
    while True:
        url = f'{GH_API}/user/repos?affiliation=owner&visibility=all&per_page=100&page={page}'
        _, arr = api(url, gh_headers)
        if not isinstance(arr, list):
            raise SyncError('GitHub returned non-array repository list, aborting entire run')
        repos.extend(x for x in arr if x.get('owner', {}).get('login', '').lower() == GH_OWNER.lower())
        if len(arr) < 100:
            break
        page += 1
        if page > 100:
            raise SyncError('Unusual number of GitHub pages; aborting')
    if not repos:
        raise SyncError('Zero GitHub repositories detected: aborting; no destinations touched')
    names = [r['name'] for r in repos]
    if len(set(names)) != len(names):
        raise SyncError('Duplicate names found in GitHub list')
    return sorted(repos, key=lambda x: x['name'].lower())


def get_gitlab(name, desired_private, headers, user, apply):
    ident = urllib.parse.quote(f'{user}/{name}', safe='')
    status, obj = api(f'{GL_API}/projects/{ident}', headers)
    if status == 404:
        if not apply:
            return None, 'would-create'
        _, obj = api(f'{GL_API}/projects', headers, method='POST', payload={
            'name':name, 'path':name, 'visibility':'private' if desired_private else 'public',
            'builds_access_level':'disabled'
        })
        print('    Created GitLab project.')
    if not isinstance(obj, dict) or not obj.get('ssh_url_to_repo'):
        raise SyncError('Missing GitLab SSH URL')
    visibility = obj.get('visibility')
    if visibility not in ('private', 'public', 'internal'):
        raise SyncError(f'Unknown GitLab visibility for {name}: {visibility!r}; refusing changes')
    return obj['ssh_url_to_repo'], visibility


def codeberg_url(name):
    ident = f'{urllib.parse.quote(CB_OWNER, safe="")}/{urllib.parse.quote(name, safe="")}'
    return f'{CB_API}/repos/{ident}'


def get_codeberg(name, desired_private, headers, apply):
    status, obj = api(codeberg_url(name), headers)
    if status == 404:
        if not apply:
            return None, 'would-create'
        _, obj = api(f'{CB_API}/user/repos', headers, method='POST', payload={
            'name':name, 'private':desired_private, 'auto_init':False
        })
        print('    Created Codeberg repository.')
    if not isinstance(obj, dict) or not obj.get('ssh_url'):
        raise SyncError('Missing Codeberg SSH URL')
    if type(obj.get('private')) is not bool:
        raise SyncError(f'Unknown Codeberg visibility for {name}; refusing changes')
    return obj['ssh_url'], 'private' if obj['private'] else 'public'


def desired_visibility(desired_private):
    return 'private' if desired_private else 'public'


def verify_github_public_now(name, gh_headers):
    # Re-check GitHub directly before making a destination public.
    # It may have been made private or deleted since initial discovery.
    url = f'{GH_API}/repos/{urllib.parse.quote(GH_OWNER, safe="")}/{urllib.parse.quote(name, safe="")}'
    status, info = api(url, gh_headers)
    if (status != 200 or not isinstance(info, dict)
            or not isinstance(info.get('owner'), dict)
            or info['owner'].get('login', '').lower() != GH_OWNER.lower()
            or info.get('private') is not False):
        raise SyncError(f'GitHub {name} is no longer confirmed public; refusing publication')


def read_destination_visibility(platform, name, gl_headers, cb_headers, gl_user):
    if platform == 'gitlab':
        project = gl_project_info(name, gl_headers, gl_user)
        vis = project.get('visibility')
        if vis not in ('private', 'public', 'internal'):
            raise SyncError(f'Cannot verify GitLab visibility on {name}: {vis!r}')
        return vis
    status, obj = api(codeberg_url(name), cb_headers)
    if status != 200 or not isinstance(obj, dict) or type(obj.get('private')) is not bool:
        raise SyncError(f'Cannot verify Codeberg visibility on {name}')
    return 'private' if obj['private'] else 'public'


def set_destination_visibility(platform, name, desired_private, gl_headers, cb_headers, gl_user):
    """Change and re-read visibility. A timeout on a write may be ambiguous.

    Only a subsequent GET with the exact desired result counts as success.
    No automatic POST/PATCH/PUT retries are attempted.
    """
    target = desired_visibility(desired_private)
    if platform == 'gitlab':
        project = gl_project_info(name, gl_headers, gl_user)
        url = f'{GL_API}/projects/{project["id"]}'
        headers = gl_headers
        method = 'PUT'
        payload = {'visibility': target}
    else:
        url = codeberg_url(name)
        headers = cb_headers
        method = 'PATCH'
        payload = {'private': desired_private}

    error = None
    try:
        status, _ = api(url, headers, method=method, payload=payload)
        if status != 200:
            error = f'Unexpected HTTP {status} from {platform} visibility update'
    except SyncError as exc:
        error = str(exc)
    try:
        actual = read_destination_visibility(platform, name, gl_headers, cb_headers, gl_user)
    except SyncError as exc:
        raise SyncError(f'{platform}: visibility update not verified for {name}; '
                        f'write issue={error or "none"}; read issue={exc}') from exc
    if actual != target:
        raise SyncError(f'{platform}: visibility update failed for {name}; '
                        f'expected={target}, actual={actual}; write issue={error or "none"}')
    print(f'    {platform}: visibility verified as {target}')
    return target


# GitLab branch protection and recovery journal

# GitLab protected branch support. Only exact, project-owned rules are
# modified. Wildcard/group rules and protected tags are never bypassed.
# A write-ahead journal prevents a killed process from leaving permissions
# temporarily relaxed without detection/recovery on the next --apply run.


def gl_project_info(name, headers, owner):
    path = urllib.parse.quote(f'{owner}/{name}', safe='')
    status, project = api(f'{GL_API}/projects/{path}', headers)
    if status != 200 or not isinstance(project, dict) or not isinstance(project.get('id'), int):
        raise SyncError(f'Cannot resolve GitLab project {owner}/{name} for protection management')
    return project


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


def plan_gl_permissions(project_id, project, src, old, headers):
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
            if branch == project.get('default_branch'):
                raise SyncError(f'Refusing deletion of GitLab default branch {branch}; align defaults first')
            payload = rule_backup_payload(rule)
            operations.append(('unprotect-delete', branch, payload))
        elif not rule.get('allow_force_push'):
            operations.append(('force', branch, None))

    return operations


def prepare_gl_permissions(project_id, project, src, old, headers):
    """Apply prevalidated, journaled permission edits after backup succeeds."""
    operations = plan_gl_permissions(project_id, project, src, old, headers)
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


# Synchronization

def sync_repository(record, gh_headers, gl_headers, cb_headers, gl_user, apply, platforms, *, full_lfs=False):
    name = record['name']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise SyncError(f'Unsupported repository name: {name}')
    source_url = record.get('ssh_url')
    if not source_url:
        raise SyncError('Missing GitHub SSH URL')
    if type(record.get('private')) is not bool:
        raise SyncError('GitHub privacy field missing or invalid; refusing destination updates')
    priv = record['private']
    repo = ROOT / (name + '.git')
    gh = remote_refs(source_url)
    heads = {k:v for k,v in gh.items() if k.startswith('refs/heads/')}
    if not heads:
        raise SyncError('No GitHub branch found: keeping existing backups/destinations unchanged')
    print(f'  source: {len(heads)} branches, {len(gh)-len(heads)} tags; privacy={"private" if priv else "public"}')

    # Resolve API objects before changing any repository data.
    destinations = {}
    blocked = 0
    for platform in platforms:
        try:
            if platform == 'gitlab':
                url, vis = get_gitlab(name, priv, gl_headers, gl_user, apply)
            else:
                url, vis = get_codeberg(name, priv, cb_headers, apply)
            target = desired_visibility(priv)
            # Privacy reduction (public/internal -> private) MUST happen before
            # any push or LFS transfer. Never allow private GitHub data into
            # a publicly accessible destination.
            if apply and url is not None and priv and vis != 'private':
                print(f'  {platform}: securing visibility {vis} -> private before Git operations')
                vis = set_destination_visibility(platform, name, True,
                                                 gl_headers, cb_headers, gl_user)
            destinations[platform] = (url, vis)
        except SyncError as e:
            print(f'  {platform}: BLOCKED: {e}')
            blocked += 1

    planned = {}
    for platform, (url, vis) in destinations.items():
        if url is None:
            print(f'  {platform}: would create project, then copy {len(gh)} refs')
            continue
        try:
            old = remote_refs(url)
            changes = count_changes(gh, old)
            print(f'  {platform}: visibility={vis}, add={changes[0]}, rewrite/update={changes[1]}, delete={changes[2]} refs')
            target = desired_visibility(priv)
            if not apply and vis != target:
                when = 'before Git sync' if priv else 'after Git ref verification'
                print(f'    {platform}: WOULD change visibility {vis} -> {target} ({when})')
            if not apply and platform == 'gitlab' and changes != (0,0,0):
                project = gl_project_info(name, gl_headers, gl_user)
                ops = plan_gl_permissions(project['id'], project, gh, old, gl_headers)
                for action, branch, _ in ops:
                    print(f'    GitLab WOULD {"temporarily allow force push" if action == "force" else "unprotect for deletion"}: {branch}')
            planned[platform] = (url, old, changes)
        except SyncError as e:
            print(f'  {platform}: BLOCKED: cannot read Git refs: {e}')
            blocked += 1

    if not apply:
        return len(planned) + sum(u is None for u, _ in destinations.values()), blocked
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    archive_source_old_tips(repo, previous)
    git('fetch', '--atomic', '--prune', '--no-tags', '--refmap=', 'origin',
        '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*', repo=repo, timeout=1200)
    src = local_refs(repo)
    if not any(k.startswith('refs/heads/') for k in src):
        raise SyncError('Fetched GitHub state has no branches; refusing all pushes')

    source_lfs = previous_lfs if src == previous else lfs_objects(repo, src)
    if src != previous:
        fetch_backup_lfs(repo, 'origin', src, source_lfs)
    if full_lfs:
        verify_archived_lfs(repo)

    successes, failures = 0, blocked
    for platform, (url, vis) in destinations.items():
        try:
            if url is None:
                # Newly created destination: retrieve its SSH URL from API again.
                if platform == 'gitlab':
                    url, _ = get_gitlab(name, priv, gl_headers, gl_user, False)
                else:
                    url, _ = get_codeberg(name, priv, cb_headers, False)
                if not url:
                    raise SyncError('New project URL not available from API')
            old = remote_refs(url)
            refs_changed = src != old
            if refs_changed and old:
                backup_destination_refs(repo, platform, url, old)
            if refs_changed or source_lfs:
                ensure_remote(repo, platform, url)
            if source_lfs and refs_changed:
                git('-c', 'lfs.allowincompletepush=false', 'lfs', 'push', '--all',
                    platform, *src, repo=repo, timeout=1800)
            if refs_changed:
                gl_project = None
                gl_pid = None
                if platform == 'gitlab':
                    gl_project = gl_project_info(name, gl_headers, gl_user)
                    gl_pid = gl_project['id']
                try:
                    if gl_pid is not None:
                        prepare_gl_permissions(gl_pid, gl_project, src, old, gl_headers)
                    push_snapshot(repo, platform, src, old, url=url)
                finally:
                    # The recovery journal also handles SIGKILL or machine power loss.
                    if gl_pid is not None:
                        restore_gl_permissions(gl_pid, gl_headers)
            try:
                verify_destination_lfs(repo, platform, src, source_lfs, full=full_lfs)
            except SyncError:
                if full_lfs or refs_changed or not source_lfs:
                    raise
                # The destination may be missing LFS objects despite already having the refs.
                git('-c', 'lfs.allowincompletepush=false', 'lfs', 'push', '--all',
                    platform, *src, repo=repo, timeout=1800)
                verify_destination_lfs(repo, platform, src, source_lfs)
            if (refs_changed or source_lfs) and remote_refs(url) != src:
                raise SyncError('Destination refs do not match the GitHub snapshot')
            # Make a formerly private destination public ONLY after its Git
            # refs match the public GitHub snapshot. This avoids publishing
            # an old/unrelated destination state on a failed Git push.
            target = desired_visibility(priv)
            if vis != target:
                if priv:
                    raise SyncError('Private GitHub repository still has non-private destination')
                print(f'  {platform}: Git refs verified; updating visibility {vis} -> public')
                verify_github_public_now(name, gh_headers)
                set_destination_visibility(platform, name, False,
                                           gl_headers, cb_headers, gl_user)
            print(f'  {platform}: SYNCED (Git refs, Git LFS and visibility verified)')
            successes += 1
        except SyncError as e:
            print(f'  {platform}: FAILED without deleting project: {e}')
            failures += 1
    return successes, failures


def run(settings, *, apply=False, only=None, platform='both', full_lfs=False):
    """Run the existing v3 mirror engine under the new configuration layer."""
    configure_runtime(settings, apply=apply)
    for executable in ('git', 'ssh', 'git-lfs'):
        if shutil.which(executable) is None:
            raise SyncError(f'Missing executable: {executable}')
    platforms = selected_platforms(platform)
    gh_headers = {'Authorization': f'Bearer {token(settings["source"])}',
                  'X-GitHub-Api-Version': '2022-11-28'}
    gl_headers = {'PRIVATE-TOKEN': token(settings['destinations']['gitlab'])} if 'gitlab' in platforms else {}
    cb_headers = {'Authorization': f'token {token(settings["destinations"]["codeberg"])}'} if 'codeberg' in platforms else {}
    if apply and 'gitlab' in platforms:
        recover_all_gl_permissions(gl_headers)
    gl_user = 'not selected'
    if 'gitlab' in platforms:
        gl_status, user = api(f'{GL_API}/user', gl_headers)
        if gl_status != 200 or not isinstance(user, dict) or not user.get('username'):
            raise SyncError('Cannot validate GitLab account')
        gl_user = user['username']
    if 'codeberg' in platforms:
        cb_status, cb_user = api(f'{CB_API}/user', cb_headers)
        if (cb_status != 200 or not isinstance(cb_user, dict) or
                cb_user.get('login', '').lower() != CB_OWNER.lower()):
            raise SyncError('Cannot validate Codeberg account')
    repos = discover_repos(gh_headers)
    print(f'GitHub: {GH_OWNER} -> GitLab: {gl_user} / Codeberg: {CB_OWNER}')
    print(f'Mode: {"APPLY" if apply else "DRY RUN"} | Discovered: {len(repos)} | Run: {RUN_ID}')
    if only:
        repos = [r for r in repos if r['name'].lower() == only.lower()]
        if not repos:
            raise SyncError(f'{only} not found among accessible GitHub repositories; no changes made')
    successes = failures = 0
    for n, repo in enumerate(repos, 1):
        print(f'\n[{n}/{len(repos)}] {repo["name"]}')
        try:
            ok, bad = sync_repository(repo, gh_headers, gl_headers, cb_headers,
                                      gl_user, apply, platforms, full_lfs=full_lfs)
            successes += ok
            failures += bad
        except SyncError as exc:
            print(f'  FAILED safely: {exc}')
            failures += 1
    print(f'\nSUMMARY: repositories={len(repos)}, successful/checked destinations={successes}, failed={failures}, mode={"apply" if apply else "dry-run"}')
    print('Missing GitHub repositories are never deleted from destinations.')
    return 0 if failures == 0 else 1


# Read-only diagnostics


def doctor(config, *, network=False, ssh=False, only=None, platform='both'):
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

    if network or ssh:
        configure_runtime(settings, apply=False)
        doctor_network(settings, selected_platforms(platform), ssh, only, report)
    else:
        report('WARNING', 'provider and SSH checks', 'skipped; pass --network or --ssh to opt in')
    return 2 if 'ERROR' in results else 0


def selected_platforms(platform):
    return ['gitlab', 'codeberg'] if platform == 'both' else [platform]


def doctor_network(settings, platforms, ssh, only, report):
    providers = {
        'github': (settings['source'], GH_API, 'login', GH_OWNER, 'Authorization', 'Bearer '),
        'gitlab': (settings['destinations']['gitlab'], GL_API, 'username', None, 'PRIVATE-TOKEN', ''),
        'codeberg': (settings['destinations']['codeberg'], CB_API, 'login', CB_OWNER, 'Authorization', 'token '),
    }
    authenticated = {}
    for name in ('github', *platforms):
        spec, base, field, expected, header, prefix = providers[name]
        try:
            headers = {header: prefix + token(spec)}
        except (SyncError, ValueError):
            report('ERROR', f'{name} credentials', 'configured token is missing or unreadable')
            continue
        if name == 'github':
            headers['X-GitHub-Api-Version'] = '2022-11-28'
        try:
            status, account = api(f'{base}/user', headers)
            login = account.get(field) if isinstance(account, dict) else None
            if status != 200 or not isinstance(login, str) or not login:
                raise SyncError('Authentication failed')
            if expected and login.casefold() != expected.casefold():
                report('ERROR', f'{name} API', 'authenticated account differs from the configured owner')
                continue
        except (SyncError, ValueError):
            report('ERROR', f'{name} API', 'identity request failed; check credentials and network access')
            continue
        authenticated[name] = (headers, login)
        report('OK', f'{name} API', 'authenticated identity request succeeded')

    if not ssh:
        return
    try:
        if 'github' not in authenticated:
            raise SyncError('Source authentication failed')
        repos = discover_repos(authenticated['github'][0])
        record = next(repo for repo in repos if only is None or repo['name'].casefold() == only.casefold())
    except (SyncError, StopIteration, ValueError):
        report('ERROR', 'SSH', 'cannot select an accessible source repository')
        return

    for name, (headers, login) in authenticated.items():
        try:
            if name == 'github':
                url = record.get('ssh_url')
            elif name == 'gitlab':
                url, _ = get_gitlab(record['name'], record['private'], headers, login, False)
            else:
                url, _ = get_codeberg(record['name'], record['private'], headers, False)
            if not url and name != 'github':
                report('WARNING', f'{name} SSH', 'matching destination is absent; read access was not checked')
                continue
            if not isinstance(url, str) or not re.match(r'^(ssh://|[^/@:\s]+@[^/:\s]+:)', url):
                raise SyncError('SSH URL unavailable')
            git('ls-remote', '--heads', '--tags', '--', url, timeout=30, retry=False,
                env={**SSH_ENV, 'GIT_ALLOW_PROTOCOL': 'ssh'})
            report('OK', f'{name} SSH', 'authenticated Git read succeeded')
        except (SyncError, ValueError):
            report('ERROR', f'{name} SSH', 'Git read failed; check trusted host keys and SSH key access')


# Command-line interface

def main(argv=None):
    parser = argparse.ArgumentParser(prog='gitmirror', description='Resilient Git mirroring')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', default='config.yml', help='Path to YAML config')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate', help='Validate YAML without connecting to providers')
    health = sub.add_parser('doctor', help='Check dependencies, configuration, and backup storage')
    health.add_argument('--network', action='store_true', help='Check provider credentials and API access')
    health.add_argument('--ssh', action='store_true', help='Check provider credentials and SSH Git read access')
    health.add_argument('--only', help='Repository to sample for the SSH check')
    health.add_argument('--platform', choices=['both', 'gitlab', 'codeberg'], default='both')
    for name in ('sync', 'run'):
        command = sub.add_parser(name)
        mode = command.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='Make changes (default: dry-run)')
        mode.add_argument('--dry-run', action='store_true', help='Plan changes without writing')
        command.add_argument('--only', help='Only one repository')
        command.add_argument('--platform', choices=['both', 'gitlab', 'codeberg'], default='both')
        command.add_argument('--verify-lfs', action='store_true', help='Recheck all archived and mirrored LFS objects (requires --apply)')
    args = parser.parse_args(argv)
    if args.command in ('sync', 'run') and args.verify_lfs and not args.apply:
        parser.error('--verify-lfs requires --apply')
    try:
        if args.command == 'doctor':
            return doctor(args.config, network=args.network, ssh=args.ssh, only=args.only, platform=args.platform)
        cfg = load_config(args.config)
        if args.command == 'validate':
            print(f'Config valid: {Path(args.config).expanduser()}')
            return 0
        def execute():
            if not args.apply:
                return run(cfg, only=args.only, platform=args.platform)
            data = cfg['storage']['path']
            data.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(data / '.gitmirror.lock'), timeout=0)
            try:
                with lock:
                    return run(cfg, apply=True, only=args.only, platform=args.platform, full_lfs=args.verify_lfs)
            except Timeout:
                print('Another GitMirror process is running; no changes made.', file=sys.stderr)
                return 2
        if args.command == 'sync':
            return execute()
        while True:
            result = execute()
            print(f'Sync cycle finished with status {result}', flush=True)
            time.sleep(cfg['schedule']['interval_hours'] * 3600)
    except (ConfigError, SyncError, OSError) as exc:
        print(f'GitMirror: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
