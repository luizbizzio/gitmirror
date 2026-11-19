"""Installer release and lifecycle checks with temporary paths and no system changes."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest


pytestmark = pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux installer')
ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_FILES = [
    'INSTALL.md', 'LICENSE', 'NOTICE.txt', 'README.md', 'gitmirror.py', 'install.sh',
    'packaging/legacy.sh', 'packaging/lifecycle.sh', 'pyproject.toml', 'web/app.js',
    'web/index.html', 'web/style.css',
]


def shell(script, directory):
    return subprocess.run(['bash', '-c',
                           'GITMIRROR_INSTALL_LIBRARY=1; source "$PROJECT/install.sh"; ' + script],
                          cwd=directory, env={**os.environ, 'PROJECT': str(ROOT)}, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=15)


def bundle(tmp_path, *, extra=None, link=False):
    archive = tmp_path / 'bundle.tar.gz'
    with tarfile.open(archive, 'w:gz') as output:
        for name in ARCHIVE_FILES:
            entry = tarfile.TarInfo(name)
            entry.size = 4
            if link and name == 'gitmirror.py':
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/etc/passwd'
                entry.size = 0
            output.addfile(entry, io.BytesIO(b'test'))
        if extra:
            output.addfile(tarfile.TarInfo(extra), io.BytesIO())
    return hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.mark.parametrize('fault', [None, 'checksum', 'symlink', 'traversal'])
def test_release_integrity_and_archive_shape(tmp_path, fault):
    digest = bundle(tmp_path, extra='../outside' if fault == 'traversal' else None, link=fault == 'symlink')
    if fault == 'checksum':
        digest = '0' * 64
    result = shell(f'verify_archive bundle.tar.gz {digest} extracted', tmp_path)
    assert (result.returncode == 0) is (fault is None), result.stderr
    if fault is None:
        assert (tmp_path / 'extracted/gitmirror.py').read_text() == 'test'
    else:
        assert not (tmp_path / 'extracted').exists()


@pytest.mark.skipif(not shutil.which('jq'), reason='release metadata parser requires jq')
@pytest.mark.parametrize('fault', [None, 'mutable', 'prerelease', 'wrong-url', 'no-digest'])
def test_metadata_pins_immutable_stable_asset(tmp_path, fault):
    digest = bundle(tmp_path)
    url = 'https://github.com/luizbizzio/gitmirror/releases/download/v1.2.3/gitmirror-linux-source.tar.gz'
    metadata = {'tag_name': 'v1.2.3', 'immutable': fault != 'mutable', 'draft': False,
                'prerelease': fault == 'prerelease',
                'assets': [{'state': 'uploaded', 'name': 'gitmirror-linux-source.tar.gz',
                            'digest': None if fault == 'no-digest' else 'sha256:' + digest,
                            'browser_download_url': 'https://other.invalid/a' if fault == 'wrong-url' else url}]}
    (tmp_path / 'metadata.json').write_text(json.dumps(metadata))
    result = shell('''
        tag= checksum= artifact=
        mkdir output
        download_https() {
            if [[ "$1" == "$release_api/latest" ]]; then cp metadata.json "$2";
            else cp bundle.tar.gz "$2"; fi
        }
        release_source output
        [[ "$tag" == v1.2.3 ]]
    ''', tmp_path)
    assert (result.returncode == 0) is (fault is None), result.stderr
    assert (tmp_path / 'output/source').exists() is (fault is None)


def test_dependencies_require_approval_and_install_only_missing(tmp_path):
    script = '''
        dpkg-query() { [[ "$*" != *" git-lfs" ]] && printf installed; }
        apt-get() { printf '%s\\n' "$*" >> apt.calls; }
        python3() { :; }
        git() { :; }
        dependencies
    '''
    denied = shell('approve=0; ' + script, tmp_path)
    assert denied.returncode and not (tmp_path / 'apt.calls').exists()
    approved = shell('approve=1; ' + script, tmp_path)
    assert approved.returncode == 0, approved.stderr
    assert (tmp_path / 'apt.calls').read_text().splitlines() == ['update', 'install -y -- git-lfs']


def test_cron_match_reports_entries_and_not_environment_or_comments(tmp_path):
    cron = ('# gitmirror comment\nTOKEN=gitmirror-secret\n'
            '0 * * * * TOKEN=hidden flock -n /run/gm.lock python3 /srv/gitmirror.py\n'
            '0 2 * * * unrelated-job\n')
    (tmp_path / 'cron').write_text(cron)
    result = shell('cron_matches < cron | cut -d: -f1', tmp_path)
    assert result.stdout.strip() == '3'


def test_migration_refuses_cron_with_an_unrelated_second_command(tmp_path):
    result = shell('''
        source "$PROJECT/packaging/legacy.sh"
        line="@hourly /usr/bin/flock -n /run/gm.lock /usr/bin/python3 -u /srv/gitmirror.py"
        selected_legacy_command "$line >> /var/log/gitmirror.log 2>&1" /run/gm.lock
        ! selected_legacy_command "$line && /srv/another-job" /run/gm.lock
        ! selected_legacy_command "0 * * * * /srv/another-job; ${line#@hourly }" /run/gm.lock
        ! selected_legacy_command "$line" /run/different.lock
    ''', tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('startup', ['success', 'failure', 'previous-link-failure'])
def test_upgrade_switch_and_failed_health_rollback(tmp_path, startup):
    result = shell(f'''
        source "$PROJECT/packaging/lifecycle.sh"
        app="$PWD/app" etc="$PWD/etc"; mkdir -p "$app/old" "$app/new" "$etc"
        echo v1.0.0 >"$app/old/version"; echo v1.1.0 >"$app/new/version"
        point "$app/old" current
        check_release() {{ :; }}
        systemctl() {{ :; }}
        drain() {{ :; }}
        undrain() {{ :; }}
        health() {{ [[ "{startup}" != failure || "$(readlink -e "$app/current")" == "$app/old" ]]; }}
        mv() {{
            if [[ "{startup}" == previous-link-failure && "$*" == *"$app/previous" ]]; then return 1; fi
            command mv "$@"
        }}
        switch_release "$app/new"
    ''', tmp_path)
    assert (result.returncode == 0) is (startup == 'success'), result.stderr
    assert (tmp_path / 'app/current').resolve().name == ('new' if startup == 'success' else 'old')
    assert not (tmp_path / 'etc/maintenance-pending').exists()


def test_uninstall_does_not_touch_configuration_or_archives(tmp_path):
    setup = '''
        source "$PROJECT/packaging/lifecycle.sh"
        app="$PWD/app" etc="$PWD/etc" state="$PWD/state" web=web.service scheduler=scheduler.service unit_dir="$PWD/units"
        mkdir -p "$app" "$etc" "$state" "$unit_dir"
        touch "$unit_dir/$web" "$unit_dir/$scheduler" "$state/archive" "$etc/secret"
        systemctl() { :; }
        drain() { :; }
        uninstall
    '''
    denied = shell('confirmation=wrong; ' + setup, tmp_path)
    assert denied.returncode and (tmp_path / 'app').is_dir()
    removed = shell('confirmation=UNINSTALL; ' + setup, tmp_path)
    assert removed.returncode == 0, removed.stderr
    assert not (tmp_path / 'app').exists() and not list((tmp_path / 'units').iterdir())
    assert (tmp_path / 'state/archive').exists() and (tmp_path / 'etc/secret').exists()
