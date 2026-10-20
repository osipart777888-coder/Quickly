#!/usr/bin/env python3
"""Restore into a new DB, retain the previous DB, roll back failed app startup."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description='Восстановление Quickly с сохранением предыдущей базы')
parser.add_argument('backup', type=Path)
parser.add_argument('--replace-local-database', action='store_true', required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
os.environ.setdefault('QUICKLY_DATA_DIR', str(Path.home()/'Library/Application Support/Quickly'))
os.environ['PATH'] = '/Applications/Docker.app/Contents/Resources/bin:' + os.environ['PATH']
compose = ['docker','compose','-p','quickly-mac','-f',str(root/'compose.macos.yml')]

def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)
def db(*command, **kwargs):
    return run(compose + ['exec','-T','db'] + list(command), **kwargs)
def sql(statement):
    db('psql','-U','quickly','-d','postgres','-v','ON_ERROR_STOP=1','-c',statement,stdout=subprocess.DEVNULL)

backup = args.backup.expanduser().resolve(strict=True)
run([str(root/'macos/quickly'),'backup'])
stamp = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + str(os.getpid())
restored, previous = 'quickly_restore_' + stamp, 'quickly_before_restore_' + stamp
db('createdb','-U','quickly',restored)
try:
    with backup.open('rb') as source:
        db('pg_restore','-U','quickly','-d',restored,'--no-owner','--no-privileges','--exit-on-error',stdin=source)
except BaseException:
    db('dropdb','-U','quickly',restored)
    raise
run(compose + ['stop','app'])
switched = False
renamed = False
try:
    sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='quickly' AND pid <> pg_backend_pid()")
    sql(f'ALTER DATABASE quickly RENAME TO {previous}')
    renamed = True
    sql(f'ALTER DATABASE {restored} RENAME TO quickly')
    switched = True
    run(compose + ['up','-d','--wait','--wait-timeout','180','app'])
except BaseException:
    run(compose + ['stop','app'])
    if switched:
        sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='quickly' AND pid <> pg_backend_pid()")
        sql(f'ALTER DATABASE quickly RENAME TO {restored}')
    if renamed:
        sql(f'ALTER DATABASE {previous} RENAME TO quickly')
    run(compose + ['up','-d','app'])
    raise RuntimeError('Восстановление не активировано. Предыдущая база возвращена. Проверьте совместимость копии и внешнего ключа.') from None
print('Восстановление завершено. Предыдущая база сохранена под именем ' + previous)
