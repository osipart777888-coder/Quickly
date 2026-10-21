#!/usr/bin/env python3
"""Offline Mac restore with shared preparation, journal and startup rollback."""
import argparse,os,subprocess,uuid,shutil
from pathlib import Path
parser=argparse.ArgumentParser(description='Восстановить Quickly с проверкой и откатом')
parser.add_argument('backup',type=Path)
parser.add_argument('--replace-local-database',action='store_true',required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parent.parent
data=Path(os.environ.setdefault('QUICKLY_DATA_DIR',str(Path.home()/'Library/Application Support/Quickly')))
os.environ['PATH']='/Applications/Docker.app/Contents/Resources/bin:'+os.environ['PATH']
compose=['docker','compose','-p','quickly-mac','-f',str(root/'compose.macos.yml')]
def run(command,**kwargs):return subprocess.run(command,check=True,**kwargs)
backup=args.backup.expanduser().resolve(strict=True)
run([str(root/'macos/quickly'),'backup'])
# Keep the selected archive in the protected mounted backup directory.
copy=data/'backups'/('restore-input-'+uuid.uuid4().hex+'.dump')
shutil.copyfile(backup,copy);os.chmod(copy,0o600)
run(compose+['stop','app'])
helper=compose+['run','--rm','--no-deps','app','python','-m','app.restore_cli']
journal=None
try:
    result=run(helper+['restore','/app/backups/'+copy.name],capture_output=True,text=True)
    journal=next(x.split('=',1)[1] for x in result.stdout.splitlines() if x.startswith('JOURNAL='))
    run(compose+['up','-d','--wait','--wait-timeout','180','app'])
    run(helper+['status',journal])
except BaseException:
    run(compose+['stop','app'])
    if journal:run(helper+['rollback',journal])
    else:run(helper+['recover'])
    run(compose+['up','-d','--wait','--wait-timeout','180','app'])
    raise RuntimeError('Восстановление не активировано. Прежняя база возвращена; копии сохранены.') from None
print('Восстановление проверено. Прежняя база сохранена. Отправки остановлены; включайте их вручную после проверки.')
