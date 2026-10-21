"""Prepared database swaps with a durable recovery journal and retained old DB."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url


def _urls(database_url):
    from app.backup_pg import sync_connection_uri
    url=make_url(sync_connection_uri(database_url))
    return url,url.set(database='postgres').render_as_string(hide_password=False)

def _directory():
    from app.backup_pg import local_disk_backups_enabled
    path=Path(os.getenv('QUICKLY_RESTORE_JOURNAL_DIR') or (str(Path.cwd()/'backups'/'restore-journal') if local_disk_backups_enabled() else '/tmp/quickly-recovery/restore-journal'))
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(path,0o700)
    return path

def _server_tag(url):
    return hashlib.sha256(url.render_as_string(hide_password=True).encode()).hexdigest()

def _write(path,data):
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(data,f);f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)
    directory=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)

def _lock(database_url):
    from app.backup_pg import BackupToolError
    url,admin_uri=_urls(database_url)
    connection=psycopg2.connect(admin_uri)
    connection.autocommit=True
    with connection.cursor() as c:
        c.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))",('quickly.restore:'+url.database,))
        if not c.fetchone()[0]:
            connection.close()
            raise BackupToolError('Другое восстановление уже выполняется')
    return connection

def _create(connection,name):
    with connection.cursor() as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))

def _swap(connection,live,replacement,retained):
    # RENAME is transactional on PostgreSQL 15. Both names change or neither does.
    connection.autocommit=False
    try:
        with connection.cursor() as c:
            c.execute("SET LOCAL lock_timeout='15s'")
            c.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=ANY(%s) AND pid<>pg_backend_pid()',([live,replacement],))
            c.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(live),sql.Identifier(retained)))
            c.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(replacement),sql.Identifier(live)))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.autocommit=True

def _prepare(uri):
    from app.backup_pg import BackupToolError
    env=dict(os.environ,DATABASE_URL=uri)
    env.pop('TEST_DATABASE_URL',None)
    result=subprocess.run([sys.executable,'-m','app.restore_candidate'],env=env,capture_output=True,timeout=600)
    if result.returncode:
        # Provider/configuration exceptions may contain secrets; keep the response categorical.
        raise BackupToolError('База не прошла миграции или проверку совместимости; восстановление не подтверждено')

@dataclass
class Activation:
    database_url:str
    path:Path
    data:dict
    connection:object=None

    def stage(self,name):
        self.data['stage']=name;_write(self.path,self.data)

    def confirm(self):
        self.stage('confirmed')
        if self.connection:self.connection.close();self.connection=None

    def rollback(self):
        connection=self.connection or _lock(self.database_url)
        try:
            with connection.cursor() as c:
                c.execute('SELECT datname FROM pg_database WHERE datname=ANY(%s)',([self.data['live'],self.data['previous'],self.data['candidate']],))
                names={row[0] for row in c}
            if self.data['previous'] in names:
                if self.data['live'] in names:
                    _swap(connection,self.data['live'],self.data['previous'],self.data['candidate'])
                else:
                    with connection.cursor() as c:c.execute(sql.SQL('ALTER DATABASE {} RENAME TO {}').format(sql.Identifier(self.data['previous']),sql.Identifier(self.data['live'])))
            self.stage('rolled_back')
        finally:
            connection.close();self.connection=None


def activate_dump(database_url,dump_path,*,defer_confirmation=False):
    from app.backup_pg import _restore_strict,_verify_restored_database,pg_dump_custom,BackupToolError
    url,_=_urls(database_url)
    connection=_lock(database_url)
    for old in _directory().glob('restore-*.json'):
        saved=json.loads(old.read_text())
        if saved.get('server')==_server_tag(url) and saved.get('stage') not in {'confirmed','rolled_back'}:
            connection.close()
            raise BackupToolError('Есть незавершённое восстановление; сначала запустите Quickly для безопасного восстановления состояния')
    token=uuid.uuid4().hex[:20]
    data={'server':_server_tag(url),'live':url.database,'candidate':'quickly_candidate_'+token,
          'previous':'quickly_before_restore_'+token,'stage':'preparing'}
    activation=Activation(database_url,_directory()/('restore-'+token+'.json'),data,connection)
    activation.stage('preparing')
    try:
        _create(connection,data['candidate'])
        candidate_uri=url.set(database=data['candidate']).render_as_string(hide_password=False)
        _restore_strict(candidate_uri,dump_path)
        _verify_restored_database(candidate_uri)
        _prepare(candidate_uri)
        # Verify a fresh snapshot of the old state in a separate DB before activation.
        snapshot=activation.path.parent.parent/('quickly-before-restore-'+token+'.dump')
        fd=os.open(snapshot,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'wb') as f:f.write(pg_dump_custom(database_url));f.flush();os.fsync(f.fileno())
        check_name='quickly_snapshot_check_'+token
        _create(connection,check_name)
        _restore_strict(url.set(database=check_name).render_as_string(hide_password=False),snapshot)
        _verify_restored_database(url.set(database=check_name).render_as_string(hide_password=False),require_admin=False)
        data['snapshot']=snapshot.name
        activation.stage('switching')
        _swap(connection,url.database,data['candidate'],data['previous'])
        activation.stage('activated')
        # Exercise migrations/ORM on the active name as a separate fresh process too.
        _prepare(url.render_as_string(hide_password=False))
        if not defer_confirmation:activation.confirm()
        return activation
    except BaseException as original:
        try:activation.rollback()
        except BaseException:
            # Never claim rollback succeeded if PostgreSQL is unavailable; startup uses this journal.
            if activation.connection:activation.connection.close();activation.connection=None
            raise BackupToolError('Не удалось завершить откат. Прежняя база и копии сохранены. После восстановления доступности PostgreSQL перезапустите Quickly; восстановление не подтверждено.') from original
        raise


def recover_interrupted_restore(database_url):
    from app.backup_pg import is_postgresql_url
    if not is_postgresql_url(database_url):return []
    url,_=_urls(database_url)
    awaiting=[]
    for path in sorted(_directory().glob('restore-*.json')):
        data=json.loads(path.read_text())
        if data.get('server')!=_server_tag(url) or data.get('stage') in {'confirmed','rolled_back'}:continue
        activation=Activation(database_url,path,data)
        if data['stage']=='awaiting_startup':awaiting.append(activation)
        else:activation.rollback()
    return awaiting
