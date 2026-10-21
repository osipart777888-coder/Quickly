"""Compare existing values before/after init_db on an isolated legacy copy.

Only counts/hashes leave this process. Credentials stay in environment/memory.
"""
import asyncio,hashlib,json,os
import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url
url=make_url(os.environ['DATABASE_URL'])
assert url.database.startswith('quickly_r2_legacy') and url.host=='quickly-revision2-pg'
from app import local_models
from app.backup_pg import sync_connection_uri
from app.database import init_db,engine
from app.security import decrypt

def normalized(value):
    if isinstance(value,str) and value.startswith('gAAAAA'):return decrypt(value)
    if isinstance(value,dict):return {k:normalized(v) for k,v in value.items()}
    if isinstance(value,list):return [normalized(v) for v in value]
    return value

def inspect(columns=None):
    with psycopg2.connect(sync_connection_uri(os.environ['DATABASE_URL'])) as connection:
        with connection.cursor() as cur:
            if columns is None:
                cur.execute("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name,ordinal_position")
                columns={}
                for table,column in cur.fetchall():columns.setdefault(table,[]).append(column)
            rows={}
            for table,names in columns.items():
                statement=sql.SQL('SELECT to_jsonb(t) FROM (SELECT {} FROM {}) t').format(sql.SQL(',').join(map(sql.Identifier,names)),sql.Identifier(table))
                cur.execute(statement)
                rows[table]=sorted(json.dumps(normalized(row[0]),sort_keys=True) for row in cur.fetchall())
    return columns,rows

columns,before=inspect()
async def migrate():await init_db();await engine.dispose()
asyncio.run(migrate())
_,after=inspect(columns)
differences={name:{'before_rows':len(values),'after_rows':len(after[name]),'changed_columns':sorted({key for row in values if row not in after[name] for key in json.loads(row)})} for name,values in before.items() if values!=after[name]}
if differences:print(json.dumps({'differences':differences}),flush=True)
assert before==after,'Existing values changed during additive migration'
digest=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest()
print(json.dumps({'existing_values_preserved':True,'before_sha256':digest,'after_sha256':digest,'tables_checked':len(columns),'administrators':len(before['app_user']),'accounts':len(before['inbox']),'contacts':len(before['lead'])}),flush=True)
