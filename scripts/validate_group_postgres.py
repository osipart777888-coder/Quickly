"""Run in the Quickly container, exclusively in disposable quickly_group_validation."""
import os
from sqlalchemy.engine import make_url
url=make_url(os.environ['DATABASE_URL'])
assert url.database=='quickly'
os.environ['DATABASE_URL']=url.set(database='quickly_group_validation').render_as_string(hide_password=False)
os.environ['QUICKLY_LOCAL_INSTALL']='true'
import asyncio
import uuid
from sqlalchemy import select,func
from app.database import Base,engine,AsyncSessionLocal
from app.models import Inbox,SmtpAccount
from app.local_models import DiagnosticPlan,DiagnosticRun,DiagnosticDayQuota,DiagnosticSenderDayQuota,MailOperation
from app import local_group_diagnostics as groups
from app import local_diagnostics
from app.sender import SendResult
from app.time import utcnow
from datetime import timedelta

async def main():
    assert engine.url.database=='quickly_group_validation'
    async with engine.begin() as conn:await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        boxes=[Inbox(email=f'validation-{i}@local.test',provider='smtp',max_emails_per_day=5) for i in range(500)]
        db.add_all(boxes);await db.flush()
        db.add_all([SmtpAccount(inbox_id=b.id,smtp_host='smtp.example.com',smtp_username=b.email,smtp_password='test-only',
            imap_host='imap.example.com',imap_username=b.email,imap_password='test-only') for b in boxes])
        plan=DiagnosticPlan(name='Disposable concurrency test',mode='group',inbox_ids=[b.id for b in boxes],sender_id=boxes[0].id,
            recipient_id=boxes[1].id,enabled=True,sending_days=list(range(7)),hours_start='00:00',hours_end='23:59',
            max_messages_per_day=7,per_account_daily_limit=1,next_run_at=utcnow()+timedelta(days=1))
        db.add(plan);await db.commit();pid=plan.id
    async def enqueue():
        async with AsyncSessionLocal() as db:
            result=await groups.enqueue_group_cycle(db,pid);await db.commit();return result
    results=await asyncio.gather(*(enqueue() for _ in range(5)))
    assert sum(r['queued'] for r in results)==500
    calls=[]
    def transport(**kwargs):calls.append(kwargs['from_email']);return SendResult('<'+uuid.uuid4().hex+'@local.test>')
    local_diagnostics.send_email=transport
    groups.in_window=lambda *args:True
    async with AsyncSessionLocal() as db:
        ids=(await db.execute(select(DiagnosticRun.id).order_by(DiagnosticRun.id).limit(40))).scalars().all()
    await asyncio.gather(*(groups.dispatch_run(rid) for rid in ids),*(groups.dispatch_run(ids[0]) for _ in range(12)))
    assert len(calls)==7 and len(set(calls))==7
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(DiagnosticRun.id)))==500
        assert await db.scalar(select(DiagnosticDayQuota.used))==7
        assert await db.scalar(select(func.sum(DiagnosticSenderDayQuota.used)))==7
        assert await db.scalar(select(func.count(MailOperation.id)))==7
    print('PASS PostgreSQL: 500 queued once across 5 concurrent planners; 52 concurrent dispatch calls => 7 unique simulated sends; atomic group and per-account budgets agree. No external messages sent.')
    await engine.dispose()
asyncio.run(main())
