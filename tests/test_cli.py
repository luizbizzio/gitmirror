from gitmirror.cli import main

def test_validate(tmp_path, capsys):
    config = tmp_path / 'c.yml'
    config.write_text('version: 1\nsource: {provider: github, owner: demo, token_env: GITHUB_TOKEN}\ndestinations:\n  gitlab: {token_env: GITLAB_TOKEN}\n  codeberg: {owner: demo, token_env: CODEBERG_TOKEN}\nstorage: {path: ~/gitmirror-test}\n')
    assert main(['--config', str(config), 'validate']) == 0
    assert 'Config valid' in capsys.readouterr().out
