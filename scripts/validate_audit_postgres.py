"""Synthetic audit workload. DATABASE_URL must select disposable quickly_audit_fixes."""
import os,asyncio,uuid,json,time
os.environ['QUICKLY_LOCAL_INSTALL']='true'
from sqlalchemy import select,func
from app.database import Base,engine,AsyncSessionLocal
from app.models import Inbox,SmtpAccount
from app.local_models import DiagnosticPlan,DiagnosticRun,MailOperation,DiagnosticDayQuota,DiagnosticSenderDayQuota,SenderDayQuota,SenderPacing
from app import local_diagnostics,local_group_diagnostics as groups
from app.local_send import guarded_send
from app.local_imports import import_contacts
from app.sender import SendResult
from app.time import utcnow
from datetime import timedelta

async def main():
    assert engine.url.database=='quickly_audit_fixes', 'Disposable validation DB required'
    async with engine.begin() as c:await c.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        boxes=[Inbox(email=f'audit-{n}@local.test',provider='smtp',max_emails_per_day=5) for n in range(1000)]
        db.add_all(boxes);await db.flush()
        db.add_all([SmtpAccount(inbox_id=b.id,smtp_host='smtp.example.com',smtp_username=b.email,smtp_password='synthetic-only',imap_host='imap.example.com',imap_username=b.email,imap_password='synthetic-only') for b in boxes])
        plan=DiagnosticPlan(name='1000 participants',mode='group',inbox_ids=[b.id for b in boxes],sender_id=boxes[0].id,recipient_id=boxes[1].id,enabled=True,max_messages_per_day=7,per_account_daily_limit=1,next_run_at=utcnow()+timedelta(days=1))
        db.add(plan);await db.commit();pid=plan.id
    async def enqueue():
        async with AsyncSessionLocal() as db:
            result=await groups.enqueue_group_cycle(db,pid);await db.commit();return result
    results=await asyncio.gather(*(enqueue() for _ in range(5)))
    assert sum(r['queued'] for r in results)==1000
    calls=[]
    def transport(**kw):calls.append(kw['from_email']);return SendResult('<'+uuid.uuid4().hex+'@local.test>')
    local_diagnostics.send_email=transport;groups.in_window=lambda *a:True
    async with AsyncSessionLocal() as db:
        ids=(await db.execute(select(DiagnosticRun.id).order_by(DiagnosticRun.id).limit(40))).scalars().all()
    await asyncio.gather(*(groups.dispatch_run(rid) for rid in ids),*(groups.dispatch_run(ids[0]) for _ in range(12)))
    assert len(calls)==len(set(calls))==7
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(DiagnosticDayQuota.used))==7
        assert await db.scalar(select(func.sum(DiagnosticSenderDayQuota.used)))==7
        assert await db.scalar(select(func.sum(SenderDayQuota.used)))==7
        assert await db.scalar(select(func.count(MailOperation.id)))==7
        box=Inbox(email='pacing@local.test',provider='smtp',max_emails_per_day=5,wait_minutes_between=60)
        db.add(box);await db.commit();iid=box.id
    paced=[]
    def paced_transport(**kw):paced.append(1);return SendResult('<pace@local.test>')
    async def attempt(n):
        async with AsyncSessionLocal() as db:
            box=await db.get(Inbox,iid)
            return await guarded_send(db,box,kind='diagnostic',operation_key=f'paced:{n}',send=paced_transport,to_email='receiver@local.test')
    results=await asyncio.gather(*(attempt(n) for n in range(30)))
    assert sum(bool(r) for r in results)==len(paced)==1
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(SenderDayQuota.used).where(SenderDayQuota.inbox_id==iid))==1
        assert await db.scalar(select(func.count(MailOperation.id)).where(MailOperation.inbox_id==iid))==1
        started=time.monotonic()
        contacts=await import_contacts(db,[{'email':f'contact-{n}@local.test','name':f'Contact {n}','consent_basis':'synthetic audit','consent_date':'2026-10-21'} for n in range(10000)])
        await db.commit()
        assert contacts['added']==10000 and contacts['invalid']==0
    print(json.dumps({'queued_once':1000,'parallel_dispatches':52,'group_sends':7,'all_group_counters':7,'parallel_same_sender':30,'paced_sends':1,'contacts_imported':10000,'import_seconds':round(time.monotonic()-started,3),'external_messages':0}))
    await engine.dispose()

asyncio.run(main())
