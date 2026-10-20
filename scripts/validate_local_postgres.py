"""Run only in a separate disposable Quickly validation database, never the live DB.

Create quickly_validation with createdb first. Run via compose run --no-deps app
python scripts/validate_local_postgres.py. The script cannot choose another DB.
"""
import asyncio
import os
from sqlalchemy.engine import make_url
url = make_url(os.environ['DATABASE_URL'])
assert url.database == 'quickly', 'Expected isolated Quickly container configuration'
os.environ['DATABASE_URL'] = url.set(database='quickly_validation').render_as_string(hide_password=False)
os.environ['QUICKLY_LOCAL_INSTALL'] = 'true'
from sqlalchemy import func, select, text
from app.database import Base, engine, AsyncSessionLocal
from app.models import Inbox, Lead, Campaign, AppSetting, SmtpAccount
from app.local_models import MailOperation, SendControl, SenderDayQuota
from app.local_send import guarded_send
from app.sender import SendResult


async def main():
    assert engine.url.database == 'quickly_validation'
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        inbox = Inbox(email='validation@local.test', provider='smtp', max_emails_per_day=5)
        db.add(inbox)
        await db.commit()
        inbox_id = inbox.id
    sends = []
    def transport(**kwargs):
        sends.append(kwargs['to_email'])
        return SendResult(f'<validation-{len(sends)}@local.test>')
    async def attempt(index, key=None):
        async with AsyncSessionLocal() as db:
            inbox = await db.get(Inbox, inbox_id)
            result = await guarded_send(db, inbox, operation_key=key or f'pg-race:{index}', kind='test',
                                        send=transport, to_email='receiver@local.test')
            return bool(result)
    results = await asyncio.gather(*(attempt(n) for n in range(18)))
    assert sum(results) == len(sends) == 5, (results, len(sends))
    # A new session simulates worker restart; committed operations cannot replay.
    assert not await attempt(0)
    async with AsyncSessionLocal() as db:
        inbox = await db.get(Inbox, inbox_id)
        inbox.max_emails_per_day = 10
        await db.commit()
    before = len(sends)
    results = await asyncio.gather(*(attempt(n, 'pg-unique-step') for n in range(12)))
    assert sum(results) == 1 and len(sends) == before + 1
    async with AsyncSessionLocal() as db:
        db.add(SendControl(id=1, emergency_stopped=True))
        await db.commit()
    assert not await attempt(99)
    async with AsyncSessionLocal() as db:
        quota = await db.scalar(select(SenderDayQuota.used).where(SenderDayQuota.inbox_id == inbox_id))
        assert quota == 6
        db.add(AppSetting(key='local_validation_secret', value='validation-secret-only'))
        await db.commit()
        stored = await db.scalar(text("SELECT value FROM app_setting WHERE key = 'local_validation_secret'"))
        assert stored != 'validation-secret-only' and 'validation-secret-only' not in stored
        assert (await db.get(AppSetting, 'local_validation_secret')).value == 'validation-secret-only'
    print('PASS PostgreSQL: 18 parallel attempts => 5 sends; 12 duplicate workers => 1 send; restart replay blocked; emergency stop; encrypted setting roundtrip.')
    await engine.dispose()

asyncio.run(main())
