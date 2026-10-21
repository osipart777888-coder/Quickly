"""Real loopback failures and SQL-pool pressure; all addresses are synthetic."""
import asyncio,io,json,socketserver,threading,time
from datetime import timedelta
from types import SimpleNamespace
from urllib.error import HTTPError
import httpx,pytest
from sqlalchemy import select,func
from app import local_diagnostics as diagnostics,local_group_diagnostics as groups
from app.local_models import DiagnosticRun,SendControl,MailOperation
from app.models import Inbox,SmtpAccount
from app.settings_manager import settings
from tests.support.local_mail import local_mail_servers,SMTPHandler
from tests.test_group_diagnostics import make_group

@pytest.fixture(autouse=True)
def local(monkeypatch):
    for name in ['QUICKLY_LOCAL_INSTALL','SMTP_ALLOW_PRIVATE_HOSTS','SMTP_ALLOW_PLAIN_SMTP']:monkeypatch.setenv(name,'true')
    monkeypatch.setattr(settings,'test_mode',False)
    monkeypatch.setattr(groups,'in_window',lambda *a:True)

async def configure(db,boxes,smtp_port,imap_port):
    for box in boxes:
        account=await db.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id==box.id))
        account.smtp_host=account.imap_host='127.0.0.1';account.smtp_port=smtp_port;account.imap_port=imap_port
        account.smtp_use_ssl=account.smtp_use_tls=account.imap_use_ssl=False
    await db.commit()

@pytest.mark.asyncio
async def test_disconnect_after_server_acceptance_is_not_replayed(session):
    class Disconnect(SMTPHandler):
        async def handle_DATA(self,server,session,envelope):
            await super().handle_DATA(server,session,envelope)
            server.transport.close()  # delivered, but no 250 acknowledgement
            return '250 never reaches the client'
    with local_mail_servers(handler_factory=Disconnect) as (store,smtp_port,imap_port):
        plan,boxes=await make_group(session,2)
        await configure(session,boxes,smtp_port,imap_port)
        queued=await groups.enqueue_group_cycle(session,plan.id);await session.commit()
        ids=(await session.execute(select(DiagnosticRun.id))).scalars().all()
        await groups.dispatch_run(ids[0]);await groups.dispatch_run(ids[1]);await groups.dispatch_run(ids[0]);await groups.run_group_jobs()
        assert sum(len(v) for v in store.messages.values())==2  # one per distinct sender, no replay
        assert await session.scalar(select(func.count(MailOperation.id)))==2
        assert set((await session.execute(select(MailOperation.status))).scalars())=={'uncertain'}

@pytest.mark.asyncio
async def test_timed_out_smtp_peer_does_not_block_healthy_account(session,monkeypatch):
    if session.bind.dialect.name=='sqlite':monkeypatch.setattr(groups,'CONCURRENCY',1)
    class Silent(socketserver.BaseRequestHandler):
        def handle(self):time.sleep(.6)
    silent=socketserver.ThreadingTCPServer(('127.0.0.1',0),Silent);silent.daemon_threads=True
    thread=threading.Thread(target=silent.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('SMTP_TIMEOUT_SECONDS','.2')
    try:
        with local_mail_servers() as (store,smtp_port,imap_port):
            plan,boxes=await make_group(session,3)
            await configure(session,boxes,smtp_port,imap_port)
            bad=await session.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id==boxes[0].id));bad.smtp_port=silent.server_address[1]
            session.add(SendControl(id=1,mail_concurrency=2));await session.commit()
            await groups.enqueue_group_cycle(session,plan.id);await session.commit()
            ids=(await session.execute(select(DiagnosticRun.id))).scalars().all()
            await asyncio.wait_for(groups.bounded(ids,groups.dispatch_run),5)
            assert await session.scalar(select(func.count(MailOperation.id)).where(MailOperation.status=='accepted'))==2
            assert await session.scalar(select(func.count(MailOperation.id)).where(MailOperation.status=='uncertain'))==1
            assert sum(len(v) for v in store.messages.values())==2
    finally:silent.shutdown();silent.server_close();thread.join(timeout=2)

@pytest.mark.asyncio
async def test_smtp_auth_failure_pauses_only_failed_sender_and_retains_other_jobs(session,monkeypatch):
    if session.bind.dialect.name=='sqlite':monkeypatch.setattr(groups,'CONCURRENCY',1)
    from aiosmtpd.smtp import AuthResult
    def auth(server,sess,envelope,mechanism,data):return AuthResult(success=data.login!=b'owner-0@local.test',handled=False)
    with local_mail_servers(authenticator=auth) as (_,smtp_port,imap_port):
        plan,boxes=await make_group(session,5)
        await configure(session,boxes,smtp_port,imap_port)
        await groups.enqueue_group_cycle(session,plan.id);await session.commit()
        ids=(await session.execute(select(DiagnosticRun.id))).scalars().all()
        await groups.bounded(ids,groups.dispatch_run)
        await session.refresh(boxes[0]);assert boxes[0].paused
        for other in boxes[1:]:await session.refresh(other);assert not other.paused
        assert await session.scalar(select(func.count(DiagnosticRun.id)))==5
        assert await session.scalar(select(func.count(MailOperation.id)).where(MailOperation.status=='accepted'))>=3

@pytest.mark.parametrize('provider',['gmail','graph'])
def test_real_provider_client_preserves_retry_after_header(monkeypatch,provider):
    import urllib.request
    from app.unibox import _gmail_request_json,_graph_request_json,GmailAPIError
    def limited(*a,**kw):raise HTTPError('https://provider.invalid',429,'limited',{'Retry-After':'1200'},io.BytesIO(b'{"error":"synthetic-rate-limit"}'))
    monkeypatch.setattr(urllib.request,'urlopen',limited)
    with pytest.raises(GmailAPIError) as error:
        (_gmail_request_json if provider=='gmail' else _graph_request_json)('GET','https://provider.invalid','synthetic-access-token')
    assert error.value.retry_after=='1200'

@pytest.mark.asyncio
async def test_waiting_mail_requests_cannot_starve_single_connection_sql_pool(session,monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
    from app import database
    from app.local_send import guarded_send
    from app.sender import SendResult
    if session.bind.dialect.name!='postgresql':pytest.skip('PostgreSQL single-connection pool pressure')
    _,boxes=await make_group(session,5)
    session.add(SendControl(id=1,mail_concurrency=2));await session.commit()
    small=create_async_engine(session.bind.url,pool_size=1,max_overflow=0,pool_timeout=1)
    factory=async_sessionmaker(small,expire_on_commit=False);monkeypatch.setattr(database,'AsyncSessionLocal',factory)
    def transport(**kw):time.sleep(.03);return SendResult('<pool@local.test>')
    async def send(iid):
        async with factory() as db:
            box=await db.get(Inbox,iid)
            return await guarded_send(db,box,operation_key='pool-'+str(iid),send=transport,to_email='pool-target@local.test')
    try:
        results=await asyncio.wait_for(asyncio.gather(*(send(b.id) for b in boxes)),8)
        assert all(isinstance(r,SendResult) for r in results)
    finally:await small.dispose()

@pytest.mark.asyncio
async def test_event_stream_releases_auth_transaction_and_closes_for_restore(session,monkeypatch):
    from app.routers.unibox import unibox_events_sse
    from app.unibox import unibox_events
    from app import maintenance
    from starlette.requests import Request
    await session.execute(select(Inbox))
    assert session.in_transaction()
    request=Request({'type':'http','method':'GET','path':'/api/unibox/events','headers':[]},receive=lambda:None)
    async def connected():return False
    monkeypatch.setattr(request,'is_disconnected',connected)
    response=await unibox_events_sse(request,db=session)
    assert not session.in_transaction()
    await unibox_events.publish({'type':'unibox.update'})
    assert 'unibox.update' in await response.body_iterator.__anext__()
    monkeypatch.setattr(maintenance,'_restoring',True)
    with pytest.raises(StopAsyncIteration):await response.body_iterator.__anext__()

@pytest.mark.asyncio
async def test_explicit_smtp_451_rejection_is_not_reported_as_unknown_delivery(session):
    class Limited(SMTPHandler):
        async def handle_DATA(self,*a):return '451 4.7.0 Please slow down'
    with local_mail_servers(handler_factory=Limited) as (store,smtp_port,imap_port):
        plan,boxes=await make_group(session,2);await configure(session,boxes,smtp_port,imap_port)
        await groups.enqueue_group_cycle(session,plan.id);await session.commit()
        rid=await session.scalar(select(DiagnosticRun.id))
        await groups.dispatch_run(rid);await groups.dispatch_run(rid)
        operation=await session.scalar(select(MailOperation))
        assert operation.status=='rejected' and not operation.retry_safe
        assert sum(len(v) for v in store.messages.values())==0
        run=await session.get(DiagnosticRun,rid)
        from app.diagnostic_observation import display_status
        assert display_status(run)=='rejected'

@pytest.mark.asyncio
async def test_removing_unused_mailbox_cleans_provider_cooldown(session):
    from app.local_models import MailboxReadState
    from app.routers.inbox import delete_inbox
    from app.time import utcnow
    box=Inbox(email='unused-cooldown@local.test');session.add(box);await session.flush();iid=box.id
    session.add(MailboxReadState(inbox_id=iid,retry_after=utcnow()+timedelta(minutes=20)));await session.commit()
    assert (await delete_inbox(iid,db=session))['ok']
    await session.commit()
    assert await session.get(MailboxReadState,iid) is None
    assert await session.get(Inbox,iid) is None

@pytest.mark.asyncio
async def test_removing_mailbox_with_queued_diagnostics_is_blocked_without_losing_queue(session):
    from app.routers.inbox import delete_inbox
    from fastapi import HTTPException
    plan,boxes=await make_group(session,2)
    await groups.enqueue_group_cycle(session,plan.id);await session.commit()
    with pytest.raises(HTTPException) as error:await delete_inbox(boxes[0].id,db=session)
    assert error.value.status_code==400
    assert await session.scalar(select(func.count(DiagnosticRun.id)))==2
