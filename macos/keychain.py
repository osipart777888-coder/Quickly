#!/usr/bin/env python3
"""Optional extra copy of local keys in the macOS login Keychain.

The secret travels over stdin, never in command arguments or terminal output.
"""
import base64
import getpass
import os
from pathlib import Path
import subprocess
import sys

path = Path.home() / 'Library/Application Support/Quickly/secrets.env'
account = getpass.getuser()
service = 'local.quickly.secrets'
if sys.argv[1:] == ['save']:
    value = base64.b64encode(path.read_bytes()).decode('ascii')
    command = f'add-generic-password -U -a "{account}" -s "{service}" -w "{value}"\n'
    result = subprocess.run(['/usr/bin/security', '-i'], input=command, text=True, capture_output=True)
    check = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', account, '-s', service, '-w'], capture_output=True)
    if check.returncode or check.stdout.strip().decode() != value:
        raise SystemExit('Не удалось сохранить ключи в Связке ключей. Защищённый локальный файл сохранён.')
    print('Ключи дополнительно сохранены в Связке ключей macOS.')
elif sys.argv[1:] == ['restore']:
    if path.exists():
        raise SystemExit('Файл уже существует; восстановление не перезаписывает ключи.')
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', account, '-s', service, '-w'], capture_output=True)
    if result.returncode:
        raise SystemExit('Не удалось прочитать ключи из Связки ключей.')
    value = base64.b64decode(result.stdout.strip(), validate=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(value)
    print('Файл секретов восстановлен из Связки ключей.')
else:
    raise SystemExit('Использование: python3 macos/keychain.py save|restore')
