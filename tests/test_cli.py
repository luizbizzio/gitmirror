from pathlib import Path
from subprocess import CompletedProcess

import pytest
import yaml

import gitmirror
from gitmirror import main

def test_validate(tmp_path, capsys):
    config = tmp_path / 'c.yml'
    config.write_text('version: 1\nsource: {provider: github, owner: demo, token_env: GITHUB_TOKEN}\ndestinations:\n  gitlab: {token_env: GITLAB_TOKEN}\n  codeberg: {owner: demo, token_env: CODEBERG_TOKEN}\nstorage: {path: ~/gitmirror-test}\n')
    assert main(['--config', str(config), 'validate']) == 0
    assert 'Config valid' in capsys.readouterr().out


@pytest.mark.parametrize('command', ['sync', 'run'])
@pytest.mark.parametrize('flags', [('--apply', '--dry-run'), ('--dry-run', '--apply')])
def test_conflicting_modes_fail_before_loading_config(command, flags, monkeypatch, capsys):
    def unexpected_config(*args):
        pytest.fail('Conflicting flags must be rejected before loading config')

    monkeypatch.setattr('gitmirror.load_config', unexpected_config)
    with pytest.raises(SystemExit) as exc:
        main([command, *flags])
    assert exc.value.code == 2
    assert 'not allowed' in capsys.readouterr().err


@pytest.fixture
def doctor_config(tmp_path, monkeypatch):
    sample = Path(__file__).resolve().parents[1] / 'config.example.yml'
    settings = yaml.safe_load(sample.read_text(encoding='utf-8'))
    settings['storage']['path'] = str(tmp_path / 'pristine')
    settings['source']['owner'] = 'demo'
    config = tmp_path / 'doctor.yml'
    config.write_text(yaml.safe_dump(settings), encoding='utf-8')

    def successful_probe(args, **kwargs):
        return CompletedProcess(args, 0, 'version for test', '')

    monkeypatch.setattr(gitmirror, 'cmd', successful_probe)
    return config


def test_doctor_default_checks_tools_and_storage_offline(doctor_config, capsys, monkeypatch):
    def fail_api(*args, **kwargs):
        pytest.fail('Network probes require an explicit flag')

    monkeypatch.setattr(gitmirror, 'api', fail_api)
    assert main(['--config', str(doctor_config), 'doctor']) == 0
    output = capsys.readouterr().out
    assert all('OK ' + item in output for item in ('Python', 'Git', 'Git LFS', 'SSH', 'configuration', 'storage space'))
    assert 'WARNING storage access' in output
    assert not (doctor_config.parent / 'pristine').exists()


def test_doctor_invalid_config_and_missing_tool_are_both_reported(doctor_config, monkeypatch, capsys):
    def missing_tool(args, **kwargs):
        return CompletedProcess(args, 1 if args[0] == 'ssh' else 0, 'test version', '')

    monkeypatch.setattr(gitmirror, 'cmd', missing_tool)
    doctor_config.write_text('broken: [', encoding='utf-8')
    assert main(['--config', str(doctor_config), 'doctor']) == 2
    output = capsys.readouterr().out
    assert 'ERROR SSH' in output and 'ERROR configuration' in output


def test_doctor_opt_in_uses_only_api_get_and_ssh_read(doctor_config, capsys, monkeypatch):
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic-github-token')
    monkeypatch.setenv('GITLAB_TOKEN', 'synthetic-gitlab-token')
    monkeypatch.delenv('CODEBERG_TOKEN', raising=False)
    ssh_calls = []

    def mock_api(url, headers, method='GET', payload=None):
        assert method == 'GET'
        if url == 'https://api.github.com/user':
            return 200, {'login': 'demo'}
        if url == 'https://gitlab.com/api/v4/user':
            return 200, {'username': 'demo'}
        if 'user/repos?' in url:
            return 200, [{'name': 'demo', 'private': False, 'owner': {'login': 'demo'},
                          'ssh_url': 'git@github.example.invalid:demo/demo.git'}]
        if url.endswith('/projects/demo%2Fdemo'):
            return 200, {'ssh_url_to_repo': 'git@gitlab.example.invalid:demo/demo.git', 'visibility': 'public'}
        pytest.fail(f'Unexpected API request {url}')

    def mock_git(*args, **kwargs):
        assert args[0] == 'ls-remote'
        assert kwargs['env']['GIT_ALLOW_PROTOCOL'] == 'ssh'
        assert 'StrictHostKeyChecking=yes' in kwargs['env']['GIT_SSH_COMMAND']
        ssh_calls.append(args[-1])
        return CompletedProcess(args, 0, '', '')

    monkeypatch.setattr(gitmirror, 'api', mock_api)
    monkeypatch.setattr(gitmirror, 'git', mock_git)
    assert main(['--config', str(doctor_config), 'doctor', '--ssh', '--only', 'demo']) == 2
    assert len(ssh_calls) == 2
    output = capsys.readouterr().out
    assert 'ERROR codeberg credentials' in output
    assert 'OK github SSH' in output and 'OK gitlab SSH' in output
    assert 'synthetic-' not in output
    assert not (doctor_config.parent / 'pristine').exists()


def test_full_lfs_verification_requires_apply(capsys):
    with pytest.raises(SystemExit) as exc:
        main(['sync', '--verify-lfs', '--dry-run'])
    assert exc.value.code == 2
    assert '--verify-lfs requires --apply' in capsys.readouterr().err
