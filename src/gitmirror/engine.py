#!/usr/bin/env python3
"""GitHub -> GitLab + Codeberg; resilient Git mirroring and visibility sync.

Default mode is audit/dry-run. Does not remove entire repositories, ever.
This mirrors Git refs (heads/tags) and repo visibility, not issues, releases, wiki, Actions.
"""
import fnmatch
import datetime as dt
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import shutil
from pathlib import Path

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
TRANSIENT_GIT_ERRORS = (
    'connection closed', 'connection timed out', 'connection reset',
    'connection refused', 'connection aborted', 'broken pipe',
    'could not resolve hostname', 'temporary failure in name resolution',
    'network is unreachable', 'no route to host', 'kex_exchange_identification',
    'remote end hung up unexpectedly', 'operation timed out',
    'client_loop: send disconnect',
)

def configure_runtime(settings):
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
    SSH_ENV.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=20')




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


def cmd(args, *, check=True, cwd=None, timeout=420):
    """Retry Git commands only when the failure looks like a transport issue."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            p = subprocess.run(args, text=True, capture_output=True, cwd=cwd,
                               env=SSH_ENV, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            if args[0] == 'git' and attempt < len(RETRY_DELAYS):
                retry_notice('Git', attempt + 1, RETRY_DELAYS[attempt], 'command timeout')
                continue
            raise SyncError(f'Command timed out: {args[0]} ({e})') from e
        except OSError as e:
            raise SyncError(f'Command failed to run: {args[0]} ({e})') from e
        if check and p.returncode:
            err = (p.stderr or p.stdout or '').strip()[-1400:]
            transient = args[0] == 'git' and any(x in err.lower() for x in TRANSIENT_GIT_ERRORS)
            if transient and attempt < len(RETRY_DELAYS):
                retry_notice('Git', attempt + 1, RETRY_DELAYS[attempt],
                             err.splitlines()[0][:120] if err else 'SSH network failure')
                continue
            raise SyncError(f'Command failed (exit={p.returncode}): {" ".join(args[:4])}\n{err}')
        return p


def git(*args, repo=None, check=True, timeout=420):
    a = ['git']
    if repo is not None:
        a += ['-C', str(repo)]
    a += list(args)
    return cmd(a, check=check, timeout=timeout)


def parse_refs(text):
    result = {}
    for line in text.splitlines():
        bits = line.split('\t', 1)
        if len(bits) != 2:
            continue
        oid, ref = bits
        if ref.endswith('^{}') or not re.fullmatch('[0-9a-f]{40,64}', oid):
            continue
        if ref.startswith('refs/heads/') or ref.startswith('refs/tags/'):
            result[ref] = oid
    return result


def remote_refs(url):
    return parse_refs(git('ls-remote', '--heads', '--tags', url, timeout=100).stdout)


def local_refs(repo):
    out = git('for-each-ref', '--format=%(objectname)\t%(refname)', 'refs/heads/', 'refs/tags/', repo=repo).stdout
    return parse_refs(out)


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


def archive_source_old_tips(repo):
    """Keep each previously observed GitHub head/tag object reachable forever."""
    previous = local_refs(repo)
    for sha in set(previous.values()):
        ref = f'refs/archive/github/{sha}'
        if git('show-ref', '--verify', '--quiet', ref, repo=repo, check=False).returncode:
            git('update-ref', ref, sha, repo=repo)


def backup_destination_refs(repo, platform, url, refs):
    """Fetch destination heads/tags into immutable, timestamped local refs before overwriting."""
    if not refs:
        return
    prefix = f'refs/archive/{RUN_ID}/{platform}'
    git('fetch', '--no-tags', '--no-prune', '--refmap=', url,
        f'+refs/heads/*:{prefix}/heads/*',
        f'+refs/tags/*:{prefix}/tags/*', repo=repo, timeout=1200)
    saved = git('for-each-ref', '--format=%(refname)', prefix, repo=repo).stdout.splitlines()
    if len(saved) < len(refs):
        raise SyncError(f'Destination backup incomplete ({platform}): saved {len(saved)} of {len(refs)} refs')
    print(f'    Backup preserved {len(saved)} destination refs under {prefix}')


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


def store_journal(project_id, entries):
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = journal_path(project_id)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with open(temp, 'w', encoding='utf-8') as file:
        json.dump(entries, file, indent=2, sort_keys=True)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, path)
    if os.name != 'nt':
        directory_fd = os.open(str(JOURNAL_DIR), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


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


def sync_repository(record, gh_headers, gl_headers, cb_headers, gl_user, apply, platforms):
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
    archive_source_old_tips(repo)
    git('fetch', '--atomic', '--prune', '--no-tags', '--refmap=', 'origin',
        '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*', repo=repo, timeout=1200)
    src = local_refs(repo)
    if not any(k.startswith('refs/heads/') for k in src):
        raise SyncError('Fetched GitHub state has no branches; refusing all pushes')

    # Keep LFS refs scoped to current GitHub heads/tags, not recovery refs.
    has_lfs = False
    if cmd(['git','lfs','version'], check=False).returncode == 0:
        has_lfs = bool(git('lfs', 'ls-files', '--all', repo=repo).stdout.strip())
    else:
        raise SyncError('git-lfs is missing. Install it before applying to avoid incomplete backups.')
    if has_lfs:
        git('lfs','fetch','--all','origin',*src.keys(),repo=repo,timeout=1800)

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
            changes = count_changes(src, old)
            if changes == (0,0,0):
                # The Git refs can already match even when a previously private
                # destination must now be made public. Do not skip that change.
                target = desired_visibility(priv)
                if vis != target:
                    if priv:
                        # Privacy reduction is performed in the resolution phase.
                        raise SyncError('Private GitHub repository still has non-private destination')
                    print(f'  {platform}: Git refs already synchronized; publishing destination')
                    verify_github_public_now(name, gh_headers)
                    set_destination_visibility(platform, name, False,
                                               gl_headers, cb_headers, gl_user)
                    print(f'  {platform}: VISIBILITY SYNCED ({target})')
                else:
                    print(f'  {platform}: already synchronized')
                successes += 1
                continue
            if old:
                backup_destination_refs(repo, platform, url, old)
            ensure_remote(repo, platform, url)
            if has_lfs:
                git('lfs','push','--all',platform,*src.keys(),repo=repo,timeout=1800)
            gl_project = None
            gl_pid = None
            if platform == 'gitlab':
                gl_project = gl_project_info(name, gl_headers, gl_user)
                gl_pid = gl_project['id']
            try:
                if gl_pid is not None:
                    prepare_gl_permissions(gl_pid, gl_project, src, old, gl_headers)
                # Scoped to heads/tags, so local refs/archive are never pushed.
                git('push','--force','--prune',platform,
                    '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*',
                    repo=repo,timeout=1800)
                actual = remote_refs(url)
                if actual != src:
                    raise SyncError('Post-push refs do not match the GitHub snapshot')
            finally:
                # Never leave force-push enabled after a normal or failed run.
                # Recovery journal also handles SIGKILL or machine power loss.
                if gl_pid is not None:
                    restore_gl_permissions(gl_pid, gl_headers)
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
            print(f'  {platform}: SYNCED (Git refs and visibility verified)')
            successes += 1
        except SyncError as e:
            print(f'  {platform}: FAILED without deleting project: {e}')
            failures += 1
    return successes, failures



def run(settings, *, apply=False, only=None, platform='both'):
    """Run the existing v3 mirror engine under the new configuration layer."""
    configure_runtime(settings)
    ROOT.mkdir(parents=True, exist_ok=True)
    for executable in ('git', 'ssh', 'git-lfs'):
        if shutil.which(executable) is None:
            raise SyncError(f'Missing executable: {executable}')
    platforms = ['gitlab', 'codeberg'] if platform == 'both' else [platform]
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
                                      gl_user, apply, platforms)
            successes += ok
            failures += bad
        except SyncError as exc:
            print(f'  FAILED safely: {exc}')
            failures += 1
    print(f'\nSUMMARY: repositories={len(repos)}, successful/checked destinations={successes}, failed={failures}, mode={"apply" if apply else "dry-run"}')
    print('Missing GitHub repositories are never deleted from destinations.')
    return 0 if failures == 0 else 1
