"""Command-line interface. --apply is always opt-in."""
import argparse
import shutil
import sys
import time
from pathlib import Path

from filelock import FileLock, Timeout

from . import __version__
from .config import ConfigError, load_config
from .engine import SyncError, run


def main(argv=None):
    parser = argparse.ArgumentParser(prog='gitmirror', description='Resilient Git mirroring')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', default='config.yml', help='Path to YAML config')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('validate', help='Validate YAML without connecting to providers')
    sub.add_parser('doctor', help='Check local tools without connecting to providers')
    for name in ('sync', 'run'):
        command = sub.add_parser(name)
        command.add_argument('--apply', action='store_true', help='Make changes (default: dry-run)')
        command.add_argument('--dry-run', action='store_true', help='Plan changes without writing')
        command.add_argument('--only', help='Only one repository')
        command.add_argument('--platform', choices=['both', 'gitlab', 'codeberg'], default='both')
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == 'validate':
            print(f'Config valid: {Path(args.config).expanduser()}')
            return 0
        if args.command == 'doctor':
            missing = [x for x in ('git', 'git-lfs', 'ssh') if shutil.which(x) is None]
            if missing:
                print('Missing tools: ' + ', '.join(missing), file=sys.stderr)
                return 2
            print('Local tools available: git, git-lfs, ssh')
            print('Provider access is not checked by doctor.')
            return 0
        data = cfg['storage']['path']
        data.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(data / '.gitmirror.lock'), timeout=0)
        def execute():
            try:
                with lock:
                    return run(cfg, apply=args.apply, only=args.only, platform=args.platform)
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
