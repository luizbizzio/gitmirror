from pathlib import Path
import os
import pytest
from gitmirror.config import load_config, ConfigError

CONFIG = """version: 1
source:
  provider: github
  owner: demo
  token_env: GITHUB_TOKEN
destinations:
  gitlab:
    token_env: GITLAB_TOKEN
  codeberg:
    owner: demo
    token_env: CODEBERG_TOKEN
storage:
  path: ~/gitmirror-test
"""

def test_valid(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text(CONFIG)
    cfg = load_config(path)
    assert cfg['source']['owner'] == 'demo'
    assert cfg['retry']['delays_seconds'] == [3, 8, 20]
    assert cfg['schedule']['interval_hours'] == 12
    assert isinstance(cfg['storage']['path'], Path)

def test_reject_plaintext_token(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text(CONFIG.replace('token_env: GITHUB_TOKEN','token: top-secret'))
    with pytest.raises(ConfigError, match='plaintext token'):
        load_config(path)

def test_reject_unsafe_yaml(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text('!!python/object/apply:os.system ["echo bad"]')
    with pytest.raises(ConfigError):
        load_config(path)

def test_reject_bad_interval(tmp_path):
    path = tmp_path / 'config.yml'
    path.write_text(CONFIG + '\nschedule:\n  interval_hours: 0\n')
    with pytest.raises(ConfigError):
        load_config(path)
