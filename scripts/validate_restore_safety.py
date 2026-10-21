"""Restore validation exclusively in disposable databases; no SMTP/API sends."""
import os,asyncio
from pathlib import Path
import psycopg2
from app.backup_pg import pg_restore_replace,BackupToolError,sync_connection_uri
from app.security import init_encryption
url=os.environ['DATABASE_URL'];assert url.endswith('/quickly_audit_restore_dst')
uri=sync_connection_uri(url)
def query(sql):
    with psycopg2.connect(uri) as c:
        with c.cursor() as r:
            r.execute(sql)
            return r.fetchall() if r.description else []
query("DROP TABLE IF EXISTS audit_payload; CREATE TABLE audit_payload(id integer PRIMARY KEY,payload text); INSERT INTO audit_payload VALUES(99999,'existing data')")
try:pg_restore_replace(url,'/tmp/bad.dump')
except BackupToolError:pass
else:raise AssertionError('Damaged dump accepted')
assert query('SELECT count(*) FROM audit_payload WHERE id=99999')[0][0]==1
print('PASS damaged dump: error raised, existing target row preserved',flush=True)
init_encryption('audit-wrong-key-only')
try:pg_restore_replace(url,'/tmp/valid.dump')
except BackupToolError:pass
else:raise AssertionError('Wrong encryption key accepted')
assert query('SELECT count(*) FROM audit_payload WHERE id=99999')[0][0]==1
print('PASS wrong key: rejected before live replacement',flush=True)
init_encryption(os.environ['QUICKLY_ENCRYPTION_KEY'])
pg_restore_replace(url,'/tmp/valid.dump')
assert query('SELECT count(*) FROM inbox')[0][0]==1000
assert query('SELECT count(*) FROM lead')[0][0]==10000
assert list(Path('/tmp/quickly-recovery').glob('quickly-before-restore-*.dump'))
print('PASS valid restore: 1000 mailboxes,10000 contacts,previous snapshot retained',flush=True)
query("CREATE TABLE IF NOT EXISTS keep_external(user_id integer REFERENCES app_user(id)); UPDATE app_user SET username='must-remain' WHERE id=1")
try:pg_restore_replace(url,'/tmp/valid.dump')
except BackupToolError:pass
else:raise AssertionError('Target DDL failure ignored')
assert query('SELECT username FROM app_user WHERE id=1')[0][0]=='must-remain'
query('DROP TABLE keep_external')
print('PASS target SQL failure: transaction rolled back, previous user preserved',flush=True)
async def post_restore():
    from fastapi import BackgroundTasks
    from app.backup_restore_ops import reinitialize_after_restore
    from app.database import engine
    await reinitialize_after_restore(BackgroundTasks())
    await engine.dispose()
asyncio.run(post_restore())
assert query('SELECT emergency_stopped,campaigns_enabled FROM local_send_control')[0]==(True,False)
assert query('SELECT count(*) FROM local_diagnostic_plan WHERE enabled')[0][0]==0
print('PASS post-restore: campaigns stopped and diagnostic schedules paused',flush=True)
