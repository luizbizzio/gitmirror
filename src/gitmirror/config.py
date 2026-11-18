"""Secure, validated YAML configuration for GitMirror."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import yaml


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
