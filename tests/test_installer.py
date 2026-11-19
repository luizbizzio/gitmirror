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
    'packaging/lifecycle.sh', 'pyproject.toml', 'web/app.js',
    'web/index.html', 'web/style.css', 'web/fonts/LICENSE.md',
    'web/fonts/source-sans-pro-regular.woff2', 'web/fonts/source-sans-pro-semibold.woff2', 'web/fonts/source-sans-pro-bold.woff2', 'web/fonts/source-sans-pro-italic.woff2',
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
        fetch_release_metadata() {
            cp metadata.json "$2"
            printf '200'
        }
        download_https() { cp bundle.tar.gz "$2"; }
        release_source output
        [[ "$tag" == v1.2.3 ]]
    ''', tmp_path)
    assert (result.returncode == 0) is (fault is None), result.stderr
    assert (tmp_path / 'output/source').exists() is (fault is None)


def test_automatic_install_upgrade_selection_does_not_overwrite_partial_state(tmp_path):
    result = shell("""
        app="$PWD/app" state="$PWD/state" etc="$PWD/etc" unit_dir="$PWD/units"
        web=web.service scheduler=scheduler.service
        mkdir -p "$unit_dir"
        action=auto
        select_action
        [[ "$action" == install ]]
        mkdir -p "$app/current"
        printf 'v0.0.3\\n' > "$app/current/version"
        action=auto
        select_action
        [[ "$action" == upgrade ]]
    """, tmp_path)
    assert result.returncode == 0, result.stderr
    partial = tmp_path / 'partial'
    partial.mkdir()
    failed = shell("""
        app="$PWD/app" state="$PWD/state" etc="$PWD/etc" unit_dir="$PWD/units"
        web=web.service scheduler=scheduler.service
        mkdir -p "$app"
        action=auto
        select_action
    """, partial)
    assert failed.returncode != 0
    assert 'refusing to overwrite' in failed.stderr


def test_automatic_fallback_uses_main_without_dev_confirmation(tmp_path):
    result = shell("""
        tag= checksum= artifact= development=0 confirmation=
        mkdir output
        fetch_release_metadata() { printf '404'; }
        confirm() { printf unexpected-confirmation; return 1; }
        development_source() {
            [[ "$1" == output ]] || return 1
            printf 'selected-main' > selected
        }
        release_source output
        [[ "$(cat selected)" == selected-main ]]
    """, tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'No published stable release exists' in result.stdout
    assert 'unexpected-confirmation' not in result.stdout


def test_missing_explicit_release_tag_never_falls_back_to_main(tmp_path):
    result = shell("""
        tag=v9.9.9 checksum= artifact= development=0 confirmation=
        mkdir output
        fetch_release_metadata() { printf '404'; }
        development_source() { printf unsafe-fallback > selected; }
        release_source output
    """, tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / 'selected').exists()


def test_dev_source_packages_allowlisted_files_and_identifies_commit(tmp_path):
    if not shutil.which('git'):
        pytest.skip('git required')
    project = tmp_path / 'fixture-repo'
    project.mkdir()
    for name in ARCHIVE_FILES:
        dest = project / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, dest)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(project)], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(project), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    actual = subprocess.check_output(['git', '-C', str(project), 'rev-parse', 'HEAD'], text=True).strip()
    script = """
        tag= checksum= artifact=
        mkdir output
        git() {
            if [[ "$1" == -c && "$3" == clone ]]; then
                command git clone --quiet "$FIXTURE" "${@: -1}"
            else
                command git "$@"
            fi
        }
        development_source output
        [[ "$tag" == "dev-${EXPECTED:0:12}" ]]
        [[ "$checksum" =~ ^[a-f0-9]{64}$ ]]
        [[ -f output/source/gitmirror.py ]]
        [[ ! -e output/source/.git ]]
        [[ "$(tar -tzf output/release.tar.gz | wc -l)" == "${#files[@]}" ]]
        previous="$checksum"
        mkdir again
        tag= checksum= artifact=
        development_source again
        [[ "$checksum" == "$previous" ]]
        cmp -s output/release.tar.gz again/release.tar.gz
    """
    env = {**os.environ, 'PROJECT': str(ROOT), 'FIXTURE': str(project), 'EXPECTED': actual}
    result = subprocess.run(['bash', '-c',
                             'GITMIRROR_INSTALL_LIBRARY=1; source "$PROJECT/install.sh"; ' + script],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


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


def test_installer_exposes_clean_lifecycle_only(tmp_path):
    result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--help'],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'install' in result.stdout and 'upgrade' in result.stdout
    assert 'migrate' not in result.stdout and 'cutover' not in result.stdout
    assert not (ROOT / 'packaging/legacy.sh').exists()
    assert 'packaging/legacy.sh' not in (ROOT / 'install.sh').read_text()


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


def test_managed_cli_install_update_and_refuse_conflicting_commands(tmp_path):
    setup = """
        source "$PROJECT/packaging/lifecycle.sh"
        cli="$PWD/gitmirror"
        install_cli
        [[ -x "$cli" ]]
        bash -n "$cli"
        grep -Fq -- '-up|update|upgrade)' "$cli"
        grep -Fq -- 'args=(--confirm UPGRADE)' "$cli"
        grep -Fq -- 'exec sudo -- "$installer"' "$cli"
        check_cli
        remove_cli
        [[ ! -e "$cli" ]]
    """
    completed = shell(setup, tmp_path)
    assert completed.returncode == 0, completed.stderr

    conflicting = shell("""
        source "$PROJECT/packaging/lifecycle.sh"
        cli="$PWD/unrelated"
        printf 'not GitMirror\\n' >"$cli"
        check_cli
    """, tmp_path)
    assert conflicting.returncode != 0
    assert (tmp_path / 'unrelated').read_text() == 'not GitMirror\n'

    (tmp_path / 'target').write_text('not managed')
    link = tmp_path / 'symlink'
    link.symlink_to(tmp_path / 'target')
    symlink = shell("""
        source "$PROJECT/packaging/lifecycle.sh"
        cli="$PWD/symlink"
        check_cli
    """, tmp_path)
    assert symlink.returncode != 0


def test_managed_update_command_runs_installer_with_no_second_confirmation(tmp_path):
    generated = shell("""
        source "$PROJECT/packaging/lifecycle.sh"
        managed_cli_content >launcher
    """, tmp_path)
    assert generated.returncode == 0, generated.stderr
    installer = tmp_path / 'fake-installer'
    installer.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    installer.chmod(0o755)
    script = tmp_path / 'launcher'
    text = script.read_text()
    text = text.replace('installer=/opt/gitmirror-managed/current/install.sh',
                        f'installer={installer}')
    script.write_text(text)
    script.chmod(0o755)
    sudo = tmp_path / 'sudo'
    sudo.write_text('#!/usr/bin/env bash\n[[ "$1" == -- ]] && shift\nexec "$@"\n')
    sudo.chmod(0o755)
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}'}
    for invocation in ('-up', 'update'):
        response = subprocess.run([str(script), invocation], cwd=tmp_path, env=env,
                                  capture_output=True, text=True, timeout=8)
        assert response.returncode == 0, response.stderr
        assert response.stdout.splitlines() == ['upgrade', '--confirm', 'UPGRADE']


def test_upgrade_skips_same_version_only_when_digest_matches(tmp_path):
    source = """
        source "$PROJECT/packaging/lifecycle.sh"
        app="$PWD/app" cli="$PWD/gitmirror"
        mkdir -p "$app/current"
        printf 'dev-123456789abc\\n' >"$app/current/version"
        printf 'correct-digest\\n' >"$app/current/digest"
        tag=dev-123456789abc
        checksum=correct-digest
        confirm() { echo unexpected-confirmation; exit 1; }
        build_release() { echo unexpected-build; exit 1; }
        switch_release() { echo unexpected-switch; exit 1; }
        upgrade
    """
    matched = shell(source, tmp_path)
    assert matched.returncode == 0, matched.stderr
    assert 'already up to date' in matched.stdout
    assert (tmp_path / 'gitmirror').is_file()
    mismatched = shell(source.replace('checksum=correct-digest', 'checksum=other-digest'), tmp_path)
    assert mismatched.returncode != 0
    assert 'archive digest differs' in mismatched.stderr


def test_uninstall_does_not_touch_configuration_or_archives(tmp_path):
    setup = '''
        source "$PROJECT/packaging/lifecycle.sh"
        app="$PWD/app" etc="$PWD/etc" state="$PWD/state" web=web.service scheduler=scheduler.service unit_dir="$PWD/units"
        cli="$PWD/gitmirror"
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
    assert not (tmp_path / 'gitmirror').exists()
