"""Seed an empty, explicitly named synthetic restore DB. Never sends mail."""
import os,sys,asyncio
from sqlalchemy.engine import make_url
url=make_url(os.environ['DATABASE_URL'])
assert url.host=='quickly-revision2-pg' and sys.argv[1].startswith('quickly_r2_')
os.environ['DATABASE_URL']=url.set(database=sys.argv[1]).render_as_string(hide_password=False)
from app import local_models
from app.database import init_db,AsyncSessionLocal,engine
from app.models import User,Inbox,SmtpAccount,Lead
from app.local_models import DiagnosticPlan,SendControl
from app.auth import hash_password
from app.local_group_diagnostics import enqueue_group_cycle
from app.time import utcnow
from sqlalchemy import insert,text
async def run():
 await init_db()
 count=int(sys.argv[2]);prefix=sys.argv[3]
 async with AsyncSessionLocal() as db:
  from sqlalchemy import select,func
  assert await db.scalar(select(func.count(User.id)))==0,'Never append into an existing database'
  db.add(User(username=prefix,email=prefix+'@local.test',password_hash=hash_password('synthetic-only-password'),role='admin',is_active=True))
  control=await db.get(SendControl,1)
  if control:control.emergency_stopped=False;control.campaigns_enabled=True
  else:db.add(SendControl(id=1,emergency_stopped=False,campaigns_enabled=True))
  await db.execute(insert(Inbox),[{'email':f'{prefix}-{n}@local.test','provider':'smtp','wait_minutes_between':1,'max_emails_per_day':5} for n in range(count)])
  from sqlalchemy import select
  boxes=(await db.execute(select(Inbox).order_by(Inbox.id))).scalars().all()
  await db.execute(insert(SmtpAccount),[{'inbox_id':b.id,'smtp_host':'127.0.0.1','smtp_port':9,'smtp_username':b.email,'smtp_password':'synthetic-only-password','smtp_use_tls':False,'imap_host':'127.0.0.1','imap_port':9,'imap_username':b.email,'imap_password':'synthetic-only-password','imap_use_ssl':False} for b in boxes])
  await db.execute(insert(Lead),[{'email':f'{prefix}-contact-{n}@local.test','name':f'Synthetic {n}'} for n in range(10000 if count==1000 else 10)])
  plan=DiagnosticPlan(name='Synthetic restore plan',mode='group',inbox_ids=[b.id for b in boxes],sender_id=boxes[0].id,recipient_id=boxes[1].id,enabled=True,max_messages_per_day=count,interval_minutes=1440,sending_days=list(range(7)),hours_start='00:00',hours_end='23:59',next_run_at=utcnow())
  db.add(plan);await db.flush()
  await enqueue_group_cycle(db,plan.id,scheduled_at=utcnow())
  await db.commit()
 await engine.dispose()
 print('seeded',sys.argv[1],count,flush=True)
asyncio.run(run())
