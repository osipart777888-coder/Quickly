"""Destructive restore fault injection ONLY in explicitly named synthetic databases.

Run in a disposable Linux app container (pg_dump/pg_restore installed), after
seeding quickly_r2_source (1000/10000) and quickly_r2_target. DATABASE_URL must
name quickly_r2_target; network host must be quickly-revision2-pg.
All former databases and dumps are retained. No mail transports run here.
"""
import asyncio,hashlib,json,os,subprocess,sys,threading,time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

url=make_url(os.environ['DATABASE_URL'])
assert url.database=='quickly_r2_target' and url.host=='quickly-revision2-pg','Synthetic stand only'
from app import security  # bind the running process to its original key before fault injection
from app import restore_safety as safety
from app.backup_pg import pg_dump_custom,pg_restore_replace,sync_connection_uri,BackupToolError

source=url.set(database='quickly_r2_source').render_as_string(hide_password=False)
target=url.render_as_string(hide_password=False)
dump=Path('/tmp/revision2-source.dump');dump.write_bytes(pg_dump_custom(source))

def query(statement,params=(),database=target):
    with psycopg2.connect(sync_connection_uri(database)) as c:
        with c.cursor() as cur:
            cur.execute(statement,params)
            return cur.fetchall() if cur.description else []

def fingerprint():
    tables=query("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    digest=hashlib.sha256()
    for (name,) in tables:
        rows=query(sql.SQL('SELECT to_jsonb(t)::text FROM {} t ORDER BY to_jsonb(t)::text').format(sql.Identifier(name)))
        digest.update(json.dumps([name,rows],sort_keys=True).encode())
    return digest.hexdigest()

results=[]
def unchanged_failure(name,action):
    before=fingerprint()
    try:action()
    except Exception:pass
    else:raise AssertionError(name+' unexpectedly succeeded')
    assert fingerprint()==before,name+' changed the original DB'
    results.append({'scenario':name,'result':'passed','old_state_unchanged':True})
    print(name,'passed',flush=True)

bad=Path('/tmp/revision2-corrupt.dump');bad.write_bytes(b'PGDMP corrupt synthetic archive')
unchanged_failure('corrupt_dump',lambda:pg_restore_replace(target,bad))
with patch.dict(os.environ,QUICKLY_ENCRYPTION_KEY='wrong-synthetic-key'):
    # The fresh candidate subprocess uses this key; no cached Fernet can mask the failure.
    unchanged_failure('wrong_encryption_key',lambda:pg_restore_replace(target,dump))

# A real SQL type incompatibility. init_db cannot repair this legacy column;
# the candidate schema verifier must reject it before activation.
query('ALTER TABLE local_diagnostic_run ALTER COLUMN check_attempts TYPE varchar USING check_attempts::varchar',database=source)
broken=Path('/tmp/revision2-incompatible.dump');broken.write_bytes(pg_dump_custom(source))
query('ALTER TABLE local_diagnostic_run ALTER COLUMN check_attempts TYPE integer USING check_attempts::integer',database=source)
unchanged_failure('incompatible_migration',lambda:pg_restore_replace(target,broken))

# Missing replay uniqueness is incompatible even if every column/PK exists.
index=query("SELECT indexrelid::regclass::text FROM pg_index WHERE indrelid='local_mail_operation'::regclass AND indisunique AND NOT indisprimary",database=source)[0][0]
query(sql.SQL('DROP INDEX {}').format(sql.Identifier(index)),database=source)
missing=Path('/tmp/revision2-missing-unique.dump');missing.write_bytes(pg_dump_custom(source))
query(sql.SQL('CREATE UNIQUE INDEX {} ON local_mail_operation(operation_key)').format(sql.Identifier(index)),database=source)
unchanged_failure('missing_replay_uniqueness',lambda:pg_restore_replace(target,missing))

original_swap=safety._swap
reached_swap=[]
def sql_error(c,live,replacement,retained):
    # First rename is real; the second SQL statement fails inside the same transaction.
    reached_swap.append(True)
    c.autocommit=False
    try:
        with c.cursor() as cur:
            cur.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=ANY(%s) AND pid<>pg_backend_pid()',([live,replacement],))
            cur.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(live),sql.Identifier(retained)))
            cur.execute('SELECT missing_revision2_function()')
        c.commit()
    except BaseException:c.rollback();raise
    finally:c.autocommit=True
with patch.object(safety,'_swap',sql_error):unchanged_failure('sql_failure_during_atomic_swap',lambda:pg_restore_replace(target,dump))
assert reached_swap

original_prepare=safety._prepare
reached_post=[]
def post_failure(uri):
    if make_url(uri).database==url.database:
        reached_post.append(True);raise RuntimeError('injected post-switch initialization failure')
    return original_prepare(uri)
with patch.object(safety,'_prepare',post_failure):unchanged_failure('post_switch_initialization_failure',lambda:pg_restore_replace(target,dump))
assert reached_post

before=fingerprint();old_oid=query('SELECT oid FROM pg_database WHERE datname=current_database()')[0][0]
with patch.object(safety,'_prepare',post_failure),patch.object(safety.Activation,'rollback',side_effect=OSError('injected database unavailable during rollback')):
    try:pg_restore_replace(target,dump)
    except BackupToolError as error:assert 'не подтверждено' in str(error)
    else:raise AssertionError('failed rollback reported success')
assert query('SELECT emergency_stopped,campaigns_enabled FROM local_send_control')==[(True,False)]
assert safety.recover_interrupted_restore(target)==[]
assert fingerprint()==before
assert query('SELECT oid FROM pg_database WHERE datname=current_database()')[0][0]==old_oid
results.append({'scenario':'rollback_unavailable_then_startup_recovery','result':'passed','old_state_unchanged':True})
print('rollback_unavailable_then_startup_recovery passed',flush=True)

before=fingerprint()
code="""
import os
from sqlalchemy.engine import make_url
from app import restore_safety as s
from app.backup_pg import pg_restore_replace
original=s._prepare
def prepare(uri):
 if make_url(uri).database=='quickly_r2_target':os._exit(73)
 return original(uri)
s._prepare=prepare
pg_restore_replace(os.environ['DATABASE_URL'],'/tmp/revision2-source.dump')
"""
crash=subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=180)
assert crash.returncode==73
assert safety.recover_interrupted_restore(target)==[]
assert fingerprint()==before
results.append({'scenario':'process_death_after_swap','result':'passed','old_state_unchanged':True})
print('process_death_after_swap passed',flush=True)

before=fingerprint()
activation=pg_restore_replace(target,dump,defer_confirmation=True)
activation.stage('awaiting_startup');activation.connection.close();activation.connection=None
code="""
import asyncio
from contextlib import asynccontextmanager
from app import main
@asynccontextmanager
async def broken(app):
 raise RuntimeError('injected application startup failure')
 yield
main.lifespan=broken
async def run():
 async with main.recoverable_lifespan(main.app):pass
asyncio.run(run())
"""
startup=subprocess.run([sys.executable,'-c',code],capture_output=True,timeout=180)
assert startup.returncode!=0
assert fingerprint()==before
results.append({'scenario':'application_startup_failure','result':'passed','old_state_unchanged':True})
print('application_startup_failure passed',flush=True)

async def drain_and_concurrency():
    import app.local_models
    from app.database import AsyncSessionLocal,engine
    from app.backup_restore_ops import restore_database_from_path
    from app.maintenance import create_background_task,is_restoring
    from app import local_diagnostics as diagnostics
    from app.local_group_diagnostics import run_group_jobs
    from app.local_models import DiagnosticRun,SendControl,DiagnosticPlan
    from app.sender import SendResult
    from app.time import utcnow
    from sqlalchemy import select,update
    from fastapi import BackgroundTasks
    # The actual diagnostic worker reserves quota/journal, then a controlled
    # local adapter waits. No SMTP server outside this container is contacted.
    query('UPDATE local_send_control SET emergency_stopped=false,campaigns_enabled=true')
    query('UPDATE local_diagnostic_plan SET enabled=true')
    entered=threading.Event();release=threading.Event();calls=[]
    def send(**kwargs):
        entered.set();release.wait(timeout=30);calls.append(1)
        return SendResult('<revision2-drain@local.test>')
    with patch.object(diagnostics,'send_email',send),patch('app.local_group_diagnostics.in_window',lambda *a:True):
        worker=create_background_task(diagnostics.run_diagnostic_job())
        deadline=time.monotonic()+15
        while not entered.is_set():
            assert time.monotonic()<deadline,'worker failed to reach transport'
            await asyncio.sleep(.02)
        restoring=asyncio.create_task(restore_database_from_path(dump,BackgroundTasks()))
        while not is_restoring():await asyncio.sleep(.01)
        assert not restoring.done() and not worker.done()
        rejected=create_background_task(asyncio.sleep(.01));assert rejected is None
        try:await restore_database_from_path(dump,BackgroundTasks())
        except BackupToolError:pass
        else:raise AssertionError('concurrent web restore was accepted')
        # Cross-process PostgreSQL lock is independent of the in-memory guard.
        lock=safety._lock(target)
        try:
            second=subprocess.run([sys.executable,'-c',"from app.restore_safety import _lock;import os;_lock(os.environ['DATABASE_URL'])"],capture_output=True,timeout=20)
            assert second.returncode!=0
        finally:lock.close()
        release.set();await worker;await restoring
        assert 1<=len(calls)<=4,'new queued sends started during maintenance'
    await engine.dispose()
    return len(calls)

calls=asyncio.run(drain_and_concurrency())
results.extend([{'scenario':'concurrent_restore','result':'passed'}, {'scenario':'restore_with_active_queue','result':'passed','inflight_completed':calls}])
async def web_restore():
    import httpx
    from app.main import app
    from app.auth import create_access_token,hash_api_key
    from app.backup_restore_staging import stage_decrypted_dump
    from app.routers.backup import STAGING_KIND_ADMIN
    from app import backup_restore_ops
    from app.database import engine
    before=fingerprint()
    token,_=stage_decrypted_dump(dump.read_bytes(),kind=STAGING_KIND_ADMIN)
    async def broken(_):raise RuntimeError('injected web post-switch failure')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app,raise_app_exceptions=False),base_url='http://test') as client:
        with patch.object(backup_restore_ops,'reinitialize_after_restore',broken):
            response=await client.post('/api/settings/backup/restore/execute',json={'restore_token':token},headers={'Authorization':'Bearer '+create_access_token(1,'admin')})
        assert response.status_code==500
        assert fingerprint()==before
        results.append({'scenario':'web_post_initialization_rollback','result':'passed','old_state_unchanged':True})
        query("INSERT INTO api_key(user_id,name,key_hash,prefix,scopes,revoked,created_at) VALUES(1,'Synthetic restore key',%s,'qk_revision2','[\"*\"]'::json,false,now())",(hash_api_key('qk_revision2_restore_synthetic'),))
        token,_=stage_decrypted_dump(dump.read_bytes(),kind=STAGING_KIND_ADMIN)
        response=await asyncio.wait_for(client.post('/api/settings/backup/restore/execute',json={'restore_token':token},headers={'X-API-Key':'qk_revision2_restore_synthetic'}),180)
        assert response.status_code==200,response.status_code
        results.append({'scenario':'web_restore_api_key_short_auth_session','result':'passed'})
    await engine.dispose()
asyncio.run(web_restore())
assert query('SELECT count(*) FROM inbox')[0][0]==1000
assert query('SELECT count(*) FROM lead')[0][0]==10000
assert query('SELECT emergency_stopped,campaigns_enabled FROM local_send_control')==[(True,False)]
assert query('SELECT count(*) FROM local_diagnostic_plan WHERE enabled')[0][0]==0
assert query("SELECT count(*) FROM local_diagnostic_run WHERE status='queued'")[0][0]==1000
results.append({'scenario':'success_1000_accounts_10000_contacts','result':'passed','queued_preserved':1000,'controls_stopped':True})
Path('/tmp/revision2-restore-results.json').write_text(json.dumps({'passed':len(results),'failed':0,'skipped':0,'scenarios':results},indent=2))
print(json.dumps({'passed':len(results),'failed':0,'skipped':0}),flush=True)
