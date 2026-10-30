"""Install the private Mac bridge and its launch agent. Credentials stay local."""
import argparse
import json
import os
import plistlib
import secrets
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parser-root', type=Path, default=ROOT / 'integrations/company_parser')
    parser.add_argument('--no-launch', action='store_true')
    args = parser.parse_args()
    directory = Path(os.environ.get('QUICKLY_DATA_DIR', Path.home() / 'Library/Application Support/Quickly'))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'parser-bridge.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    source = args.parser_root.resolve()
    if not (source / 'company_emails/worker.py').is_file(): raise SystemExit('Не найдены исходники парсера')
    # A LaunchAgent cannot reliably access Documents (macOS privacy prompts).
    # Install code in Application Support; never move the user's original tree.
    runtime = directory / 'parser-runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    for name in ['company_emails', 'vendor', 'data']:
        shutil.copytree(source / name, runtime / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ['requirements.txt', 'LICENSE', 'THIRD_PARTY.md']:
        shutil.copy2(source / name, runtime / name)
    for name in ['local_parser_bridge.py', 'local_parser_worker.py', 'local_parser_access.py']:
        shutil.copy2(ROOT / 'scripts' / name, runtime / name)
    # Database backups include WAL and do not modify the old parser databases.
    for old in (source / 'runs').glob('*/contacts.sqlite'):
        destination = runtime / 'runs' / old.parent.name
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(old.resolve().as_uri() + '?mode=ro', uri=True) as origin:
            with sqlite3.connect(destination / 'contacts.sqlite') as target: origin.backup(target)
        (destination / 'contacts.sqlite').chmod(0o600)
        if (old.parent / 'plan.json').exists(): shutil.copy2(old.parent / 'plan.json', destination / 'plan.json')
    # Saved selection URLs are portable. Browser login sessions stay untouched
    # in the original project; Quickly owns separate Chrome profiles.
    if (source / '.local/sources').is_dir():
        shutil.copytree(source / '.local/sources', runtime / '.local/sources', dirs_exist_ok=True)
    python = runtime / '.venv/bin/python'
    if not python.is_file():
        subprocess.run([str(Path(sys.executable).resolve()), '-m', 'venv', str(runtime / '.venv')], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(runtime / 'requirements.txt')], check=True)
    subprocess.run([str(python), '-c', 'import pychrome, psutil, requests, xlsxwriter'], check=True)
    subprocess.run([str(python), '-c', 'from pathlib import Path; from company_emails.world import install; install(Path.cwd())'], cwd=runtime, check=True)
    config.update(parser_root=str(runtime), original_root=str(source), port=config.get('port', 8877), api_key=config.get('api_key') or secrets.token_urlsafe(32))
    config['base_url'] = 'http://host.docker.internal:' + str(config['port'])
    # Private file is mounted read-only into Quickly. No tokens in arguments/logs.
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, ensure_ascii=False) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)
    logs = directory / 'logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    agent = Path.home() / 'Library/LaunchAgents/com.quickly.parser-bridge.plist'
    agent.parent.mkdir(parents=True, exist_ok=True)
    payload = {'Label': 'com.quickly.parser-bridge', 'ProgramArguments': [str(python), str(runtime / 'local_parser_bridge.py'), '--config', str(path)],
        'WorkingDirectory': str(runtime), 'RunAtLoad': True, 'KeepAlive': True,
        'StandardOutPath': str(logs / 'parser-bridge.log'), 'StandardErrorPath': str(logs / 'parser-bridge-error.log')}
    with agent.open('wb') as output: plistlib.dump(payload, output)
    agent.chmod(0o600)
    if not args.no_launch:
        domain = 'gui/' + str(os.getuid())
        subprocess.run(['launchctl', 'bootout', domain + '/com.quickly.parser-bridge'], capture_output=True)
        subprocess.run(['launchctl', 'bootstrap', domain, str(agent)], check=True)
    print('Локальный парсер подключён. Откройте Quickly → Сбор компаний → Локальный парсер.')


if __name__ == '__main__': main()
