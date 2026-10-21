"""Regressions for the 2026-10-21 audit; no external mail is sent."""
import asyncio
from datetime import datetime,timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func,update
from app import local_group_diagnostics as groups,local_diagnostics as diagnostics,local_send
from app.local_models import DiagnosticPlan,DiagnosticRun,DiagnosticDayQuota,MailOperation,SenderPacing
from app.sender import SendResult,SendFailure
from tests.test_group_diagnostics import make_group,cycle


@pytest.fixture(autouse=True)
def local(monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    monkeypatch.setattr(groups,'CONCURRENCY',1)


@pytest.mark.asyncio
async def test_automatic_reply_waits_for_mailbox_spacing_without_using_quota(session,monkeypatch):
    monkeypatch.setattr(groups,'in_window',lambda *a:True)
    plan,boxes=await make_group(session,2,auto_reply=True)
    for b in boxes:b.wait_minutes_between=120
    await session.commit()
    calls=[]
    def send(**kw):calls.append(kw['from_email']);return SendResult('<audit-'+str(len(calls))+'@local.test>')
    async def received(*args,**kwargs):return {'found':True,'folder':'INBOX'}
    monkeypatch.setattr(diagnostics,'send_email',send);monkeypatch.setattr(diagnostics,'find_received',received)
    _,runs=await cycle(session,plan)
    for run in runs:await groups.dispatch_run(run.id)
    await groups.poll_run(runs[0].id)
    assert len(calls)==2
    assert await session.scalar(select(DiagnosticDayQuota.used))==2
    # Advance the persisted pacing timestamp and polling slot; a later tick replies once.
    await session.execute(update(SenderPacing).values(last_reserved_at=local_send.utcnow()-timedelta(minutes=121)))
    await session.execute(update(DiagnosticRun).values(last_checked_at=None));await session.commit()
    await groups.poll_run(runs[0].id)
    assert len(calls)==3
    assert await session.scalar(select(DiagnosticDayQuota.used))==3


@pytest.mark.asyncio
async def test_20_closed_windows_do_not_starve_open_plan(session,monkeypatch):
    _,boxes=await make_group(session,2,enabled=False)
    for n in range(21):
        session.add(DiagnosticPlan(name='closed' if n<20 else 'open',mode='group',inbox_ids=[b.id for b in boxes],
            sender_id=boxes[0].id,recipient_id=boxes[1].id,enabled=True,next_run_at=local_send.utcnow()-timedelta(days=1),
            sending_days=list(range(7)),hours_start='00:00',hours_end='23:59',max_messages_per_day=100))
    await session.commit()
    monkeypatch.setattr(groups,'in_window',lambda plan,*a:plan.name=='open')
    calls=[]
    original_enqueue=groups.enqueue_group_cycle
    async def enqueue(db,plan_id,**kwargs):
        calls.append(plan_id)
        return await original_enqueue(db,plan_id,**kwargs)
    async def no_dispatch(*args):pass
    monkeypatch.setattr(groups,'bounded',no_dispatch)
    monkeypatch.setattr(groups,'enqueue_group_cycle',enqueue)
    await groups.run_group_jobs();await groups.run_group_jobs()
    assert len(calls)==1
    assert (await session.get(DiagnosticPlan,calls[0])).name=='open'


@pytest.mark.asyncio
async def test_inbox_counts_include_diagnostic_sends_and_attempts(session,monkeypatch):
    from app.routers.inbox import list_inboxes
    from app.routers.local import overview
    monkeypatch.setattr(groups,'in_window',lambda *a:True)
    plan,_=await make_group(session,2)
    monkeypatch.setattr(diagnostics,'send_email',lambda **kw:SendResult('<audit-badge@local.test>'))
    _,runs=await cycle(session,plan);await groups.dispatch_run(runs[0].id)
    inboxes=await list_inboxes(db=session);normal=next(x for x in inboxes if x.id==runs[0].sender_id)
    dashboard=await overview(db=session);local=next(x for x in dashboard['accounts'] if x['id']==normal.id)
    assert normal.sent_today==local['sent_today']==1
    assert normal.attempts_today==local['attempts_today']==1


def test_utc_clock_and_moscow_window_do_not_depend_on_server_timezone(monkeypatch):
    import os,time
    from app import time as clock
    from app.jobs import _in_sending_window
    old=os.environ.get('TZ')
    try:
        monkeypatch.setenv('TZ','Europe/Moscow');time.tzset()
        assert abs((clock.now()-clock.utcnow()).total_seconds())<1
        campaign=SimpleNamespace(timezone='Europe/Moscow',sending_days=list(range(7)),sending_hours_start='09:00',sending_hours_end='17:00')
        assert not _in_sending_window(datetime(2026,10,21,5,30),campaign)  # 08:30 MSK
        assert _in_sending_window(datetime(2026,10,21,13,0),campaign)      # 16:00 MSK
    finally:
        if old is None:os.environ.pop('TZ',None)
        else:os.environ['TZ']=old
        time.tzset()


def test_restore_rejects_exit_one_and_requires_single_transaction(monkeypatch):
    import app.backup_pg as backup
    calls=[]
    def fail(args,**kwargs):
        calls.append(args);return SimpleNamespace(returncode=1,stderr=b'pg_restore: error: unexpected EOF')
    monkeypatch.setattr(backup.subprocess,'run',fail)
    with pytest.raises(backup.BackupToolError,match='unexpected EOF'):
        backup._restore_strict('postgresql://audit:audit@localhost/disposable','/tmp/synthetic.dump',clean=True)
    assert '--single-transaction' in calls[0] and '--exit-on-error' in calls[0]


@pytest.mark.asyncio
async def test_campaign_retries_known_auth_refusal_only_after_explicit_resume(session,monkeypatch):
    from app import jobs
    from app.local_models import SendControl
    from app.models import CampaignLead,Lead,QueueSlot,EmailLog
    from app.settings_manager import settings
    from app.routers.inbox import unpause_inbox
    from fastapi import BackgroundTasks
    from tests.test_send_auth_failure import _make_smtp_inbox,_make_due_slot,_SessionCtx
    box=await _make_smtp_inbox(session);box.tracking_domain='links.example.com';box.wait_minutes_between=0
    slot=await _make_due_slot(session,box)
    cl=await session.get(CampaignLead,slot.campaign_lead_id);lead=await session.get(Lead,cl.lead_id)
    lead.custom_data={'consent_basis':'synthetic audit','consent_date':'2026-10-20'}
    session.add(SendControl(id=1,campaigns_enabled=True));await session.commit()
    calls=[]
    def fail(**kw):calls.append('535');return SendFailure('auth_failed','535 rejected before DATA')
    def success(**kw):calls.append('accepted');return SendResult('<audit-resumed@local.test>')
    async def webhook(*a,**kw):return None
    monkeypatch.setattr(settings,'test_mode',False)
    monkeypatch.setattr(jobs,'_in_sending_window',lambda *a:True)
    monkeypatch.setattr(jobs,'fire_webhook_event',webhook)
    monkeypatch.setattr(jobs,'AsyncSessionLocal',lambda:_SessionCtx(session))
    monkeypatch.setattr(jobs,'send_email',fail)
    await jobs.send_slot_job(slot.id)
    assert box.paused and calls==['535']
    await unpause_inbox(box.id,BackgroundTasks(),db=session)
    monkeypatch.setattr(jobs,'send_email',success)
    await jobs.send_slot_job(slot.id)
    assert calls==['535','accepted']
    assert await session.scalar(select(func.count(QueueSlot.id)))==0
    assert await session.scalar(select(func.count(EmailLog.id)))==1
    assert await session.scalar(select(func.count(MailOperation.id)))==2
    await jobs.send_slot_job(slot.id)
    assert len(calls)==2


@pytest.mark.asyncio
async def test_resume_never_releases_accepted_or_uncertain_operations(session):
    from app.models import Inbox
    box=Inbox(email='audit@local.test');session.add(box);await session.flush()
    for state in ['accepted','uncertain','reserved']:
        session.add(MailOperation(operation_key=state,inbox_id=box.id,recipient='target@local.test',kind='campaign',status=state))
    await session.commit()
    assert await local_send.authorize_rejected_retries(session,box.id)==0
    assert set((await session.execute(select(MailOperation.operation_key))).scalars())=={'accepted','uncertain','reserved'}


@pytest.mark.asyncio
async def test_read_scoped_api_key_cannot_modify_send_controls(session):
    import httpx
    from app.auth import hash_api_key
    from app.models import User,APIKey
    from app.database import get_db
    from app.main import app
    user=User(username='audit-admin',email='audit@local.test',role='admin',is_active=True)
    session.add(user);await session.flush()
    session.add(APIKey(user_id=user.id,name='read-only audit key',key_hash=hash_api_key('qk_synthetic_audit_only'),prefix='qk_synthetic',scopes=['read']))
    await session.commit()
    async def db():yield session
    previous=dict(app.dependency_overrides);app.dependency_overrides[get_db]=db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            headers={'X-API-Key':'qk_synthetic_audit_only'}
            assert (await client.get('/api/local/control',headers=headers)).status_code==200
            assert (await client.put('/api/local/control',headers=headers,json={'emergency_stopped':True})).status_code==403
    finally:app.dependency_overrides.clear();app.dependency_overrides.update(previous)


@pytest.mark.asyncio
async def test_login_throttles_failed_attempts_even_with_spoofed_forwarded_headers(session):
    import httpx
    from app.database import get_db
    from app.main import app
    async def db():yield session
    previous=dict(app.dependency_overrides);app.dependency_overrides[get_db]=db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            statuses=[]
            for n in range(11):
                r=await client.post('/api/auth/login',json={'username':'audit-absent','password':'wrong'},headers={'X-Forwarded-For':f'198.51.100.{n}'})
                statuses.append(r.status_code)
        assert statuses[:10]==[401]*10 and statuses[10]==429
    finally:app.dependency_overrides.clear();app.dependency_overrides.update(previous)


@pytest.mark.asyncio
async def test_editing_group_pauses_it_and_preserves_queue_and_used_quota(session):
    from app.routers.local import edit_group_settings,GroupSettings
    plan,_=await make_group(session,2);_,runs=await cycle(session,plan)
    session.add(DiagnosticDayQuota(plan_id=plan.id,day=local_send.day_bounds()[0],used=2));await session.commit()
    settings=GroupSettings(name='Edited',interval_minutes=60,per_account_daily_limit=2,max_messages_per_day=1,
        auto_reply=True,sending_days=[0,1],hours_start='10:00',hours_end='12:00')
    result=await edit_group_settings(plan.id,settings,db=session,_admin=None)
    assert not result['enabled'] and not plan.enabled and plan.name=='Edited'
    assert plan.max_messages_per_day==1 and plan.inbox_ids==[runs[0].sender_id,runs[1].sender_id]
    assert await session.scalar(select(DiagnosticDayQuota.used))==2
    assert await session.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status=='queued'))==2


@pytest.mark.asyncio
async def test_restore_waits_for_workers_and_blocks_new_background_work():
    from app.maintenance import background_work,exclusive_restore
    started=asyncio.Event();release=asyncio.Event();inside=asyncio.Event();calls=[]
    @background_work
    async def work():
        calls.append(1);started.set();await release.wait()
    task=asyncio.create_task(work());await started.wait()
    async def restore():
        async with exclusive_restore():inside.set()
    restoring=asyncio.create_task(restore());await asyncio.sleep(0)
    await work();assert calls==[1] and not inside.is_set()
    release.set();await asyncio.gather(task,restoring)
    assert inside.is_set()
