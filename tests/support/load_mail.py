"""Full-queue loopback SMTP/IMAP benchmark. Never points at a production DB.

Each invocation uses a fresh process and an empty quickly_r2_load_* database.
Every account sends one real TCP SMTP message and has it found over TCP IMAP.
Workers are driven without the minute scheduler wait; daily budgets, pacing,
STOP, replay prevention, and the shared connection limit remain enabled.
"""
import argparse,asyncio,json,os,resource,socket,subprocess,sys,threading,time
from pathlib import Path
from unittest.mock import patch
from sqlalchemy.engine import make_url
parser=argparse.ArgumentParser();parser.add_argument('--accounts',type=int,choices=[100,250,500],required=True);parser.add_argument('--output',required=True);parser.add_argument('--restart-database',action='store_true');parser.add_argument('--restart-container',choices=['quickly-revision2-check'],required=True);args=parser.parse_args()
url=make_url(os.environ['DATABASE_URL'])
assert url.database.startswith('quickly_r2_load_') and url.host=='127.0.0.1' and url.port==55439,'Disposable test database only'
os.environ.update(QUICKLY_LOCAL_INSTALL='true',SMTP_ALLOW_PRIVATE_HOSTS='true',SMTP_ALLOW_PLAIN_SMTP='true',QUICKLY_REQUIRE_EXTERNAL_KEYS='true',QUICKLY_ENCRYPTION_KEY='revision2-test-encryption-only',QUICKLY_SECRET_KEY='revision2-test-signing-only-32-characters',QUICKLY_TELEMETRY='false')
from app import local_models
from app.database import engine,init_db,AsyncSessionLocal
from app.local_models import DiagnosticPlan,DiagnosticRun,SendControl,MailOperation,SenderDayQuota,SenderPacing
from app.models import Inbox,SmtpAccount
from app import local_group_diagnostics as groups
from app.local_send import guarded_send,day_bounds
from app.sender import SendResult,SendFailure
from app.settings_manager import settings
from app.time import utcnow
from sqlalchemy import select,func,event
from tests.support.local_mail import local_mail_servers,SMTPHandler

sql_count=0
@event.listens_for(engine.sync_engine,'after_cursor_execute')
def sql_statement(*a,**kw):
    global sql_count
    sql_count+=1

class Connections:
    def __init__(self):self.active=0;self.peak=0;self.opens=0;self.lock=threading.Lock();self.by_kind={'smtp':0,'imap':0};self.peak_kind={'smtp':0,'imap':0}
    def opened(self,kind):
        with self.lock:
            self.active+=1;self.opens+=1;self.peak=max(self.peak,self.active);self.by_kind[kind]+=1;self.peak_kind[kind]=max(self.peak_kind[kind],self.by_kind[kind])
    def closed(self,kind):
        with self.lock:self.active-=1;self.by_kind[kind]-=1

async def main():
    global sql_count
    await init_db();settings.test_mode=False
    async with AsyncSessionLocal() as db:
        assert await db.scalar(select(func.count(Inbox.id)))==0,'Never overwrite an earlier benchmark'
    original_data=SMTPHandler.handle_DATA;deliveries=[]
    async def delayed(self,server,session,envelope):
        await asyncio.sleep(.3 if envelope.mail_from=='load-0@local.test' else .01)
        result=await original_data(self,server,session,envelope)
        deliveries.append((envelope.mail_from,time.perf_counter()))
        return result
    with patch.object(SMTPHandler,'handle_DATA',delayed),local_mail_servers() as (store,smtp_port,imap_port):
        async with AsyncSessionLocal() as db:
            boxes=[]
            for i in range(args.accounts):
                box=Inbox(email=f'load-{i}@local.test',provider='smtp',max_emails_per_day=5,wait_minutes_between=1,max_jitter_seconds=0)
                db.add(box);await db.flush();boxes.append(box)
                db.add(SmtpAccount(inbox_id=box.id,smtp_host='127.0.0.1',smtp_port=smtp_port,smtp_username=box.email,smtp_password='synthetic-only',smtp_use_ssl=False,smtp_use_tls=False,imap_host='127.0.0.1',imap_port=imap_port,imap_username=box.email,imap_password='synthetic-only',imap_use_ssl=False))
            plan=DiagnosticPlan(name='Loopback benchmark',mode='group',sender_id=boxes[0].id,recipient_id=boxes[1].id,inbox_ids=[b.id for b in boxes],enabled=True,interval_minutes=1440,max_messages_per_day=args.accounts,per_account_daily_limit=1,auto_reply=False,sending_days=list(range(7)),hours_start='00:00',hours_end='23:59',observation_hours=48,poll_interval_seconds=300)
            db.add(plan);db.add(SendControl(id=1,mail_concurrency=4,emergency_stopped=False,campaigns_enabled=False));await db.commit()
            sql_count=0;start=time.perf_counter();queued=await groups.enqueue_group_cycle(db,plan.id);await db.commit();queue_seconds=time.perf_counter()-start;queue_queries=sql_count
            ids=(await db.execute(select(DiagnosticRun.id))).scalars().all()
            control=await db.get(SendControl,1);control.emergency_stopped=True;await db.commit()
        # Restart ONLY our disposable app container. The DB container and all
        # other projects stay running. STOP must prevent any transport on restart.
        started=time.perf_counter()
        docker='/Applications/Docker.app/Contents/Resources/bin/docker'
        await engine.dispose()
        if args.restart_database:
            mounts=json.loads(subprocess.check_output([docker,'inspect','quickly-revision2-pg','--format','{{json .Mounts}}']))
            assert any(m.get('Name')=='quickly_revision2_pgdata_20261021' for m in mounts),'Never kill a DB without our persistent test volume'
            subprocess.run([docker,'kill','--signal=KILL','quickly-revision2-pg'],check=True,capture_output=True,timeout=15)
            subprocess.run([docker,'start','quickly-revision2-pg'],check=True,capture_output=True,timeout=15)
            for _ in range(100):
                ready=subprocess.run([docker,'exec','quickly-revision2-pg','pg_isready','-U','audit'],capture_output=True)
                if ready.returncode==0:break
                time.sleep(.1)
            else:raise RuntimeError('Test PostgreSQL did not recover')
        subprocess.run([docker,'restart',args.restart_container],check=True,capture_output=True,timeout=60)
        remote=url.set(host='quickly-revision2-pg',port=5432).render_as_string(hide_password=False)
        code=f"""
import asyncio
from sqlalchemy import select,func
from app.database import AsyncSessionLocal
from app.local_models import DiagnosticRun,MailOperation
from app.local_group_diagnostics import run_group_jobs
async def check():
 await run_group_jobs()
 async with AsyncSessionLocal() as db:
  assert await db.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status=='queued'))=={args.accounts}
  assert await db.scalar(select(func.count(MailOperation.id)))==0
asyncio.run(check())
"""
        subprocess.run([docker,'exec','-e','DATABASE_URL='+remote,args.restart_container,'python','-c',code],check=True,capture_output=True,timeout=60)
        restart_seconds=time.perf_counter()-started
        async with AsyncSessionLocal() as db:
            control=await db.get(SendControl,1);control.emergency_stopped=False;await db.commit()
        counter=Connections();real_connect=socket.create_connection
        class MeteredSocket(socket.socket):
            def _real_close(self):
                kind=getattr(self,'_measured_kind',None)
                if kind:self._measured_kind=None;counter.closed(kind)
                super()._real_close()
        def measured(address,*a,**kw):
            connected=real_connect(address,*a,**kw)
            port=int(address[1]);kind='smtp' if port==smtp_port else 'imap' if port==imap_port else None
            if not kind:return connected
            timeout=connected.gettimeout()
            result=MeteredSocket(fileno=connected.detach());result.settimeout(timeout)
            result._measured_kind=kind;counter.opened(kind);return result
        sql_count=0;cpu_start=time.process_time();started=time.perf_counter()
        with patch('socket.create_connection',measured),patch.object(groups,'in_window',lambda *a:True):
            await groups.bounded(ids,groups.dispatch_run)
            sending_seconds=time.perf_counter()-started
            checked_at=time.perf_counter()
            await groups.bounded(ids,groups.poll_run)
            reading_seconds=time.perf_counter()-checked_at
        total_seconds=time.perf_counter()-started;cpu_seconds=time.process_time()-cpu_start;queries=sql_count
        async with AsyncSessionLocal() as db:
            received=await db.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status=='received'))
            accepted=await db.scalar(select(func.count(MailOperation.id)).where(MailOperation.status=='accepted'))
            quotas=(await db.execute(select(SenderDayQuota.used))).scalars().all()
            checks=(await db.execute(select(DiagnosticRun.check_attempts))).scalars().all()
            errors=await db.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status.in_(['failed','uncertain','skipped'])))
            assert accepted==received==args.accounts and errors==0
            assert len(quotas)==args.accounts and set(quotas)=={1} and set(checks)=={1}
            # Attempt a second automatic message within the configured minute.
            box=await db.get(Inbox,boxes[0].id)
            def forbidden(**kw):raise AssertionError('pacing/quota protection bypassed')
            paced=await guarded_send(db,box,operation_key='benchmark-pacing',kind='diagnostic',send=forbidden,to_email=boxes[1].email)
            assert isinstance(paced,SendFailure) and paced.error_type=='local_deferred'
            # Exhaust the remaining permitted quota synthetically in this stand;
            # only the guard is called. No extra message is delivered.
            quota=await db.get(SenderDayQuota,(box.id,day_bounds()[0]));quota.used=5
            pace=await db.get(SenderPacing,box.id);pace.last_reserved_at=utcnow().replace(year=2000);await db.commit()
            capped=await guarded_send(db,box,operation_key='benchmark-quota',kind='diagnostic',send=forbidden,to_email=boxes[1].email)
            assert isinstance(capped,SendFailure) and capped.error_type=='local_block'
        slow=next(t for sender,t in deliveries if sender=='load-0@local.test')
        assert any(t<slow for sender,t in deliveries if sender!='load-0@local.test')
        assert counter.active==0 and counter.peak<=4 and counter.peak_kind['imap']<=2
        rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/(1024*1024 if sys.platform=='darwin' else 1024)
        result={'accounts':args.accounts,'queued':queued['queued'],'smtp_accepted':accepted,'imap_received':received,'errors':errors,'queue_seconds':round(queue_seconds,3),'queue_sql_queries':queue_queries,'send_seconds':round(sending_seconds,3),'read_seconds':round(reading_seconds,3),'worker_seconds':round(total_seconds,3),'cpu_seconds':round(cpu_seconds,3),'cpu_percent_one_core':round(cpu_seconds/total_seconds*100,2),'peak_rss_mb':round(rss,2),'peak_mail_connections':counter.peak,'peak_smtp_connections':counter.peak_kind['smtp'],'peak_imap_connections':counter.peak_kind['imap'],'mail_connections_opened':counter.opens,'processing_sql_queries':queries,'container_restart_queue_check_seconds':round(restart_seconds,3),'imap_messages_per_second':round(received/reading_seconds,3),'quota_and_pacing_checked':True,'slow_peer_did_not_block_others':True,'scheduler_mode':'workers driven continuously; production timer dispatches at most 20/minute','production_timer_ticks':(args.accounts+19)//20,'production_timer_minutes_from_first_tick':(args.accounts+19)//20-1,'production_timer_first_tick_wait_up_to_seconds':60,'database_container_crash_restarted':args.restart_database,'postgres_storage':'named Docker volume; not tmpfs','connection_limit':4,'per_account_daily_limit':5,'wait_minutes_between':1,'group_initial_limit_per_account':1,'provider':'loopback SMTP+IMAP only; no external mail'}
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    await engine.dispose()

asyncio.run(main())
