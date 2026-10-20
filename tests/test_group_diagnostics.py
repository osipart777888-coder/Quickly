"""Groups queue owned diagnostics at scale; all transports here are doubles or loopback."""
from datetime import timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from app import local_group_diagnostics as groups
from app import local_diagnostics as diagnostics
from app.local_models import DiagnosticDayQuota, DiagnosticPlan, DiagnosticRun, DiagnosticSenderDayQuota, MailOperation, SendControl, SenderDayQuota
from app.models import Inbox, SmtpAccount
from app.routers.local import PlanRequest, create_plan, cancel_group_queue, diagnostic_runs
from app.sender import SendResult
from app.time import utcnow


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    monkeypatch.setattr(groups,'in_window',lambda *args:True)


async def make_group(db,count=3,**settings):
    boxes=[]
    offset=await db.scalar(select(func.count(Inbox.id))) or 0
    for n in range(count):
        box=Inbox(email=f'owner-{offset+n}@local.test',provider='smtp',max_emails_per_day=5)
        db.add(box);await db.flush();boxes.append(box)
        db.add(SmtpAccount(inbox_id=box.id,smtp_host='smtp.example.com',smtp_username=box.email,smtp_password='local-test-only',
            imap_host='imap.example.com',imap_username=box.email,imap_password='local-test-only'))
    await db.commit()
    values=dict(name='Owned group',mode='group',inbox_ids=[b.id for b in boxes],sender_id=boxes[0].id,recipient_id=boxes[1].id,
        enabled=True,sending_days=list(range(7)),hours_start='00:00',hours_end='23:59',interval_minutes=1440,
        max_messages_per_day=1000,per_account_daily_limit=1,next_run_at=utcnow()+timedelta(days=1))
    values.update(settings)
    plan=DiagnosticPlan(**values);db.add(plan);await db.commit()
    return plan,boxes


def transport(monkeypatch):
    calls=[]
    def send(**kwargs):
        calls.append(kwargs)
        return SendResult(f'<group-{len(calls)}@local.test>')
    monkeypatch.setattr(diagnostics,'send_email',send)
    return calls


async def cycle(db,plan):
    result=await groups.enqueue_group_cycle(db,plan.id);await db.commit()
    rows=(await db.execute(select(DiagnosticRun).where(DiagnosticRun.batch_id==result['batch_id']).order_by(DiagnosticRun.sender_id))).scalars().all()
    return result,rows


def test_ring_for_500_accounts_rotates_without_self_or_all_to_all():
    members=[{'id':i,'email':f'{i}@local.test','eligible':True} for i in range(500)]
    first=groups.ring_pairs(members,0);second=groups.ring_pairs(members,1)
    assert len(first)==len(second)==500
    assert len({p['recipient_id'] for p in first})==500
    assert all(p['sender_id']!=p['recipient_id'] for p in first)
    assert all(a['recipient_id']!=b['recipient_id'] for a,b in zip(first,second))


@pytest.mark.asyncio
async def test_500_account_cycle_is_persistent_and_repeat_enqueue_does_not_duplicate(session):
    plan,_=await make_group(session,500,enabled=False)
    result,runs=await cycle(session,plan)
    assert result['queued']==500 and all(r.status=='queued' for r in runs)
    assert all(not r.message_id for r in runs)
    repeated=await groups.enqueue_group_cycle(session,plan.id)
    assert repeated['queued']==0 and repeated['pending']==500
    assert await session.scalar(select(func.count(DiagnosticRun.id)))==500


@pytest.mark.asyncio
async def test_preview_excludes_paused_unconfigured_and_failed_accounts(session):
    plan,boxes=await make_group(session,5)
    boxes[2].paused=True
    smtp=await session.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id==boxes[3].id));smtp.imap_host=''
    smtp=await session.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id==boxes[4].id));smtp.last_tested_at=utcnow();smtp.last_test_ok=False
    await session.commit()
    members=await groups.group_members(session,plan.inbox_ids)
    assert sum(x['eligible'] for x in members)==2
    pairs=groups.ring_pairs(members)
    assert {p['sender_id'] for p in pairs}=={boxes[0].id,boxes[1].id}


@pytest.mark.asyncio
async def test_group_api_saves_paused_and_checks_ownership(session):
    plan,boxes=await make_group(session)
    data=dict(name='Bulk from UI',mode='group',inbox_ids=[b.id for b in boxes],enabled=True,owned_accounts_confirmed=True)
    result=await create_plan(PlanRequest(**data),db=session,_admin=None)
    saved=await session.get(DiagnosticPlan,result['id'])
    assert saved.mode=='group' and not saved.enabled and saved.inbox_ids==plan.inbox_ids
    with pytest.raises(HTTPException):
        await create_plan(PlanRequest(**{**data,'owned_accounts_confirmed':False}),db=session,_admin=None)
    with pytest.raises(HTTPException):
        await create_plan(PlanRequest(**{**data,'inbox_ids':[boxes[0].id,99999]}),db=session,_admin=None)


@pytest.mark.asyncio
async def test_outgoing_daily_limit_and_rotation_survive_new_cycle(session,monkeypatch):
    plan,_=await make_group(session)
    calls=transport(monkeypatch)
    _,runs=await cycle(session,plan)
    first_pairs=[(r.sender_id,r.recipient_id) for r in runs]
    for run in runs:await groups.dispatch_run(run.id)
    assert len(calls)==3
    _,next_runs=await cycle(session,plan)
    assert all(a[1]!=b.recipient_id for a,b in zip(first_pairs,next_runs))
    for run in next_runs:await groups.dispatch_run(run.id)
    assert len(calls)==3
    assert await session.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status=='skipped'))==3


@pytest.mark.asyncio
async def test_shared_group_cap_is_not_exceeded_and_counter_rollback_is_atomic(session,monkeypatch):
    plan,_=await make_group(session,max_messages_per_day=2)
    calls=transport(monkeypatch);_,runs=await cycle(session,plan)
    for run in runs:await groups.dispatch_run(run.id)
    assert len(calls)==2
    assert await session.scalar(select(DiagnosticDayQuota.used))==2
    assert await session.scalar(select(func.sum(DiagnosticSenderDayQuota.used)))==2


@pytest.mark.asyncio
async def test_standard_mailbox_quota_still_caps_group_messages(session,monkeypatch):
    plan,boxes=await make_group(session,2,per_account_daily_limit=10)
    for b in boxes:b.max_emails_per_day=1
    await session.commit();calls=transport(monkeypatch)
    for _ in range(2):
        _,runs=await cycle(session,plan)
        for run in runs:await groups.dispatch_run(run.id)
    assert len(calls)==2
    assert await session.scalar(select(func.sum(SenderDayQuota.used)))==2


@pytest.mark.asyncio
async def test_pause_resume_and_emergency_leave_pending_checks_unsent(session,monkeypatch):
    plan,_=await make_group(session,enabled=False)
    calls=transport(monkeypatch);_,runs=await cycle(session,plan)
    await groups.dispatch_run(runs[0].id);assert not calls
    plan.enabled=True;session.add(SendControl(id=1,emergency_stopped=True));await session.commit()
    await groups.dispatch_run(runs[0].id);assert not calls
    control=await session.get(SendControl,1);control.emergency_stopped=False;await session.commit()
    await groups.dispatch_run(runs[0].id);await groups.dispatch_run(runs[0].id)
    assert len(calls)==1


@pytest.mark.asyncio
async def test_recipient_paused_after_allocation_is_skipped_without_reassignment(session,monkeypatch):
    plan,boxes=await make_group(session);_,runs=await cycle(session,plan);calls=transport(monkeypatch)
    recipient=await session.get(Inbox,runs[0].recipient_id);recipient.paused=True;await session.commit()
    await groups.dispatch_run(runs[0].id)
    assert not calls
    await session.refresh(runs[0]);assert runs[0].status=='skipped'
    assert runs[0].recipient_id==recipient.id


@pytest.mark.asyncio
async def test_plan_stop_after_send_reservation_prevents_network(session,monkeypatch):
    from app import local_send
    plan,_=await make_group(session);_,runs=await cycle(session,plan);calls=transport(monkeypatch)
    reserve=local_send.reserve_operation
    async def stop_after_reserve(db,*args,**kwargs):
        result=await reserve(db,*args,**kwargs)
        await db.execute(update(DiagnosticPlan).where(DiagnosticPlan.id==plan.id).values(enabled=False));await db.commit()
        return result
    monkeypatch.setattr(local_send,'reserve_operation',stop_after_reserve)
    await groups.dispatch_run(runs[0].id)
    assert not calls
    assert await session.scalar(select(MailOperation.status))=='cancelled'


@pytest.mark.asyncio
async def test_cancel_queue_keeps_history_and_does_not_send(session,monkeypatch):
    plan,_=await make_group(session);_,runs=await cycle(session,plan);calls=transport(monkeypatch)
    result=await cancel_group_queue(plan.id,db=session,_admin=None);await session.commit()
    assert result['cancelled']==3
    for r in runs:await groups.dispatch_run(r.id)
    assert not calls and await session.scalar(select(func.count(DiagnosticRun.id)))==3


@pytest.mark.asyncio
async def test_crashed_claim_is_never_replayed(session,monkeypatch):
    plan,_=await make_group(session);_,runs=await cycle(session,plan);calls=transport(monkeypatch)
    await session.execute(update(DiagnosticRun).values(status='dispatching',started_at=utcnow()-timedelta(minutes=11)));await session.commit()
    await groups.run_group_jobs()
    assert not calls
    assert await session.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status=='uncertain'))==3


@pytest.mark.asyncio
async def test_history_filters_include_sender_and_recipient(session):
    plan,boxes=await make_group(session);await cycle(session,plan)
    rows=await diagnostic_runs(plan_id=plan.id,inbox_id=boxes[0].id,db=session)
    assert len(rows)==2 and all(boxes[0].id in (r['sender_id'],r['recipient_id']) for r in rows)


@pytest.mark.asyncio
async def test_unavailable_group_does_not_block_later_due_group(session,monkeypatch):
    # Shared-memory SQLite serializes writes; real concurrency is checked in PostgreSQL.
    monkeypatch.setattr(groups,'CONCURRENCY',1)
    first,boxes=await make_group(session,next_run_at=utcnow()-timedelta(minutes=2))
    for box in boxes:box.paused=True
    second,_=await make_group(session,next_run_at=utcnow()-timedelta(minutes=1))
    calls=transport(monkeypatch)
    monkeypatch.setattr(diagnostics,'find_received',unreceived)
    await groups.run_group_jobs()
    assert len(calls)==3
    assert await session.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.plan_id==second.id))==3


async def unreceived(*args,**kwargs):
    return {'found':False}


@pytest.mark.asyncio
async def test_paused_groups_do_not_starve_receipt_checks_for_enabled_group(session,monkeypatch):
    monkeypatch.setattr(groups,'CONCURRENCY',1)
    paused,_=await make_group(session,21,enabled=False)
    _,paused_runs=await cycle(session,paused)
    active,_=await make_group(session,2)
    _,active_runs=await cycle(session,active)
    await session.execute(update(DiagnosticRun).values(status='accepted'));await session.commit()
    checked=[]
    async def find(db,inbox,run,**kwargs):
        checked.append(run.id);return {'found':False}
    monkeypatch.setattr(diagnostics,'find_received',find)
    await groups.run_group_jobs()
    assert set(checked)=={r.id for r in active_runs}


@pytest.mark.asyncio
@pytest.mark.parametrize('auto_reply',[False,True])
async def test_group_roundtrip_over_real_local_smtp_imap(session,monkeypatch,auto_reply):
    from tests.support.local_mail import local_mail_servers
    from app.settings_manager import settings
    monkeypatch.setenv('SMTP_ALLOW_PRIVATE_HOSTS','true');monkeypatch.setenv('SMTP_ALLOW_PLAIN_SMTP','true')
    monkeypatch.setattr(settings,'test_mode',False)
    with local_mail_servers() as (store,smtp_port,imap_port):
        plan,boxes=await make_group(session,2,auto_reply=auto_reply,max_messages_per_day=4)
        for account in (await session.execute(select(SmtpAccount))).scalars().all():
            account.smtp_host=account.imap_host='127.0.0.1';account.smtp_port=smtp_port;account.imap_port=imap_port
            account.smtp_use_tls=account.smtp_use_ssl=account.imap_use_ssl=False
        await session.commit();_,runs=await cycle(session,plan)
        for run in runs:await groups.dispatch_run(run.id)
        for run in runs:await groups.poll_run(run.id)
        assert sum(len(v) for v in store.messages.values())==(4 if auto_reply else 2)
        for run in runs:
            await session.refresh(run)
            assert run.status==('reply_sent' if auto_reply else 'received')
            if auto_reply:
                run.last_checked_at=None;await session.commit();await groups.poll_run(run.id)
                await session.refresh(run);assert run.status=='reply_received'
        operations=(await session.execute(select(MailOperation))).scalars().all()
        assert len(operations)==(4 if auto_reply else 2) and all(o.status=='accepted' for o in operations)
