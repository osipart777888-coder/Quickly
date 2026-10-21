"""Offline restore helper for the Mac launcher; shares the web restore protocol."""
import argparse
import json
import re
from app.database import db_url
from app.backup_pg import pg_restore_replace
from app.restore_safety import Activation,_directory,recover_interrupted_restore

parser=argparse.ArgumentParser()
parser.add_argument('operation',choices=['restore','status','rollback','recover'])
parser.add_argument('value',nargs='?')
args=parser.parse_args()
if args.operation=='restore':
    activation=pg_restore_replace(db_url,args.value,defer_confirmation=True)
    activation.stage('awaiting_startup')
    activation.connection.close();activation.connection=None
    print('JOURNAL='+activation.path.name)
elif args.operation=='recover':
    for activation in recover_interrupted_restore(db_url):activation.rollback()
else:
    if not re.fullmatch(r'restore-[a-f0-9]{20}\.json',args.value or ''):raise SystemExit('Invalid recovery journal')
    path=_directory()/args.value
    activation=Activation(db_url,path,json.loads(path.read_text()))
    if args.operation=='rollback':activation.rollback()
    else:
        print(activation.data['stage'])
        if activation.data['stage']!='confirmed':raise SystemExit(1)
