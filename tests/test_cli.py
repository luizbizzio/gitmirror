import pytest

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
