from pathlib import Path
import os
import pytest
import yaml
from gitmirror import load_config, ConfigError

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


@pytest.mark.parametrize('failure', ['no-targets', 'self', 'relative', 'nested', 'archive-disabled', 'duplicate'])
def test_v2_rejects_unsafe_or_ambiguous_destinations(tmp_path, failure):
    spec = {'version': 2,
            'source': {'provider': 'gitlab', 'owner': 'source', 'token_env': 'TEST_SOURCE'},
            'storage': {'path': str(tmp_path / 'work')}}
    local = {'provider': 'local', 'path': str(tmp_path / 'backup')}
    remote = {'provider': 'gitlab', 'owner': 'SOURCE', 'token_env': 'TEST_TARGET'}
    variants = {'no-targets': [], 'self': [remote], 'relative': [{**local, 'path': 'backup'}],
                'nested': [{**local, 'path': str(tmp_path / 'work' / 'backup')}],
                'archive-disabled': [{**local, 'archive': False}], 'duplicate': [local, local]}
    spec['destinations'] = variants[failure]
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump(spec), encoding='utf-8')
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize('policy', [{'keep': 0}, {'days': -1}, {'automatic': 'true'}, {'keep': True}])
def test_retention_requires_explicit_safe_values(tmp_path, policy):
    path = tmp_path / 'config.yml'
    path.write_text(yaml.safe_dump({
        'version': 2, 'source': {'provider': 'github', 'owner': 'source', 'token_env': 'SOURCE'},
        'destinations': [{'id': 'backup', 'provider': 'local', 'path': str(tmp_path / 'backups'), 'retention': policy}],
    }), encoding='utf-8')
    with pytest.raises(ConfigError, match='keep and days'):
        load_config(path)
