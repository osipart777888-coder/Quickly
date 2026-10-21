"""Observation terminates, respects provider blocks and never resends a message."""
from datetime import timedelta
import asyncio,imaplib,smtplib,threading
from types import SimpleNamespace
import pytest
from sqlalchemy import select,func
from app import local_diagnostics as diagnostics,local_group_diagnostics as groups
from app.local_models import DiagnosticRun,SendControl,MailboxReadState,MailOperation
from app.models import Inbox
from app.time import utcnow
from tests.test_group_diagnostics import make_group

@pytest.fixture(autouse=True)
def local(monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    monkeypatch.setattr(groups,'in_window',lambda *a:True)

async def accepted(db,*,old=False,**settings):
    plan,boxes=await make_group(db,2,**settings)
    run=DiagnosticRun(sender_id=boxes[0].id,recipient_id=boxes[1].id,plan_id=plan.id,status='accepted',message_id='<sent@local.test>',created_at=utcnow()-timedelta(days=4),accepted_at=utcnow()-timedelta(hours=73 if old else 1))
    db.add(run);await db.commit();return run,plan,boxes

@pytest.mark.asyncio
async def test_expiry_persists_under_emergency_stop_and_manual_check_only_reads(session,monkeypatch):
    run,plan,_=await accepted(session,old=True,enabled=False,observation_hours=72)
    session.add(SendControl(id=1,emergency_stopped=True));await session.commit()
    async def forbidden(*a,**kw):raise AssertionError('expired automated check must not open network')
    monkeypatch.setattr(diagnostics,'find_received',forbidden)
    await groups.run_group_jobs();await session.refresh(run)
    assert run.status=='expired' and run.observation_status=='expired' and run.next_check_at is None
    assert run.check_attempts==0
    async def found(*a,**kw):return {'found':True,'folder':'Spam'}
    monkeypatch.setattr(diagnostics,'find_received',found)
    await diagnostics.check_receipt(session,run)
    assert run.status=='received' and run.folder_kind=='spam' and run.check_attempts==1
    assert await session.scalar(select(func.count(MailOperation.id)))==0
    assert run.accepted_at<utcnow()-timedelta(hours=72)

@pytest.mark.asyncio
async def test_acceptance_time_and_configured_interval_replace_old_24h_cutoff(session,monkeypatch):
    run,_,_=await accepted(session,observation_hours=48,poll_interval_seconds=600)
    calls=[]
    async def missing(*a,**kw):calls.append(1);return {'found':False}
    monkeypatch.setattr(diagnostics,'find_received',missing)
    await groups.run_group_jobs();await session.refresh(run)
    assert calls==[1] and run.check_attempts==1 and run.observation_status=='not_found'
    assert 590<(run.next_check_at-utcnow()).total_seconds()<=600
    assert run.observation_deadline==run.accepted_at+timedelta(hours=48)
    await groups.run_group_jobs();assert calls==[1]

@pytest.mark.asyncio
@pytest.mark.parametrize('code,body',[(429,''),(503,''),(403,'userRateLimitExceeded')])
async def test_retry_after_persists_for_all_runs_of_same_receiving_account(session,monkeypatch,code,body):
    from app.unibox import GmailAPIError
    first,plan,boxes=await accepted(session)
    second=DiagnosticRun(plan_id=plan.id,sender_id=boxes[0].id,recipient_id=boxes[1].id,status='accepted',message_id='<second@local.test>',accepted_at=utcnow())
    session.add(second);await session.commit();calls=[]
    async def throttled(*a,**kw):calls.append(1);raise GmailAPIError(code,body,retry_after='1200')
    monkeypatch.setattr(diagnostics,'find_received',throttled)
    await diagnostics.check_receipt(session,first)
    # Fresh session simulates process restart; Retry-After is not in memory only.
    async with groups.AsyncSessionLocal() as fresh:
        saved=await fresh.get(DiagnosticRun,second.id)
        await diagnostics.check_receipt(fresh,saved)
        state=await fresh.get(MailboxReadState,boxes[1].id)
        assert state.retry_after>utcnow()+timedelta(seconds=1190)
        assert saved.check_attempts==0 and saved.observation_status=='temporarily_unavailable'
    assert calls==[1] and not boxes[1].paused

@pytest.mark.asyncio
async def test_imap_auth_error_pauses_account_and_autopoll_does_not_bypass_it(session,monkeypatch):
    run,_,boxes=await accepted(session);calls=[]
    async def denied(*a,**kw):calls.append(1);raise imaplib.IMAP4.error('AUTHENTICATIONFAILED synthetic')
    monkeypatch.setattr(diagnostics,'find_received',denied)
    await diagnostics.check_receipt(session,run,manual=False)
    assert boxes[1].paused
    await diagnostics.check_receipt(session,run,manual=False)
    assert calls==[1] and run.check_attempts==1

@pytest.mark.asyncio
async def test_shared_connection_budget_and_cancel_waits_for_actual_socket_thread(session):
    from app.mail_io import mail_slot,await_thread,_budgets
    session.add(SendControl(id=1,mail_concurrency=2));await session.commit()
    started=threading.Event();release=threading.Event();admitted=asyncio.Event()
    def blocked_socket():started.set();release.wait(timeout=3)
    async def first():
        async with groups.AsyncSessionLocal() as db:
            async with mail_slot(db,reading=True):await await_thread(blocked_socket)
    async def second():
        async with groups.AsyncSessionLocal() as db:
            async with mail_slot(db,reading=True):admitted.set()
    task=asyncio.create_task(first())
    while not started.is_set():await asyncio.sleep(.01)
    task.cancel();other=asyncio.create_task(second())
    # A second read waits, while a send still has the reserved capacity.
    async with groups.AsyncSessionLocal() as db:
        async with mail_slot(db):pass
    await asyncio.sleep(.1)
    assert not admitted.is_set()  # canceled socket still occupies its read slot
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    await other
    budget=_budgets[asyncio.get_running_loop()]
    assert budget.active==budget.reading==0 and budget.peak<=2

@pytest.mark.parametrize('kind',['smtp','imap'])
def test_authentication_failure_closes_socket(monkeypatch,kind):
    from app import smtp_utils
    closed=[]
    imap_error=imaplib.IMAP4.error
    class Client:
        def __init__(self,*a,**kw):pass
        def login(self,*a):
            if kind=='smtp':raise smtplib.SMTPAuthenticationError(535,b'bad synthetic password')
            raise imap_error('AUTHENTICATIONFAILED')
        def close(self):closed.append('close')
        def shutdown(self):closed.append('shutdown')
    account=SimpleNamespace(smtp_host='127.0.0.1',smtp_port=25,smtp_use_ssl=False,smtp_use_tls=False,smtp_username='synthetic',smtp_password='bad',imap_host='127.0.0.1',imap_port=143,imap_use_ssl=False,imap_username='synthetic',imap_password='bad')
    monkeypatch.setattr(smtplib,'SMTP',Client);monkeypatch.setattr(imaplib,'IMAP4',Client)
    with pytest.raises(Exception):getattr(smtp_utils,'_'+kind+'_connect')(account)
    assert closed==['close' if kind=='smtp' else 'shutdown']

@pytest.mark.asyncio
@pytest.mark.parametrize('provider',['gmail','office365'])
async def test_slow_oauth_refresh_does_not_block_event_loop(session,monkeypatch,provider):
    import time
    from app import unibox
    entered=threading.Event();heartbeat=asyncio.Event()
    def slow(*a):entered.set();time.sleep(.08);return True
    async def credentials(*a):return ('synthetic-client','synthetic-secret','synthetic-tenant') if provider=='office365' else ('synthetic-client','synthetic-secret')
    monkeypatch.setattr(unibox,'get_office365_oauth_credentials' if provider=='office365' else 'get_google_oauth_credentials',credentials)
    monkeypatch.setattr(unibox,'refresh_office365_token' if provider=='office365' else 'refresh_access_token',slow)
    async def live_ui():
        while not entered.is_set():await asyncio.sleep(0)
        heartbeat.set()
    ui=asyncio.create_task(live_ui())
    await (unibox._refresh_o365_token if provider=='office365' else unibox._refresh_gmail_token)(session,SimpleNamespace())
    assert heartbeat.is_set()
    await ui

@pytest.mark.asyncio
@pytest.mark.parametrize('code,body',[(429,''),(403,'userRateLimitExceeded')])
async def test_unibox_and_diagnostics_share_provider_read_block(session,monkeypatch,code,body):
    from app import unibox
    run,_,boxes=await accepted(session);calls=[]
    account=SimpleNamespace(inbox_id=boxes[1].id,access_token='synthetic-token')
    def limited(*a):calls.append(1);raise unibox.GmailAPIError(code,body,retry_after='1200')
    with pytest.raises(unibox.GmailAPIError):
        await unibox._gmail_call_with_refresh(session,account,limited)
    state=await session.get(MailboxReadState,boxes[1].id)
    assert state.retry_after>utcnow()+timedelta(seconds=1190)
    async def forbidden(*a,**kw):raise AssertionError('diagnostics bypassed a Unibox provider block')
    monkeypatch.setattr(diagnostics,'find_received',forbidden)
    await diagnostics.check_receipt(session,run)
    async with groups.AsyncSessionLocal() as fresh:
        with pytest.raises(unibox.GmailAPIError):await unibox._o365_call_with_refresh(fresh,account,limited)
    assert calls==[1] and run.check_attempts==0 and run.observation_status=='temporarily_unavailable'
