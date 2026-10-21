"""A local-install connection budget shared by sends and mailbox reads."""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import weakref
import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from app.local_models import SendControl

def _inbox_id(value):
    if isinstance(value,int):return value
    return getattr(value,'inbox_id',None) or getattr(value,'id',None)

async def _read_allowed(inbox_id):
    if not inbox_id:return
    from app.database import AsyncSessionLocal
    from app.local_models import MailboxReadState
    from app.time import utcnow
    async with AsyncSessionLocal() as db:
        state=await db.get(MailboxReadState,inbox_id)
        if state and state.retry_after and state.retry_after>utcnow():
            from app.unibox import GmailAPIError
            error=GmailAPIError(429,'Чтение временно ограничено провайдером',retry_after=str((state.retry_after-utcnow()).total_seconds()))
            error._quickly_backoff_recorded=True
            raise error

async def _record_read_limit(inbox_id,error):
    if not inbox_id or getattr(error,'_quickly_backoff_recorded',False):return
    from app.diagnostic_observation import retry_delay
    code=getattr(error,'status_code',0)
    quota=code==403 and any(word in str(getattr(error,'body','')).lower() for word in ['ratelimitexceeded','quotaexceeded','dailylimitexceeded'])
    delay=retry_delay(getattr(error,'retry_after',None))
    if code not in {429,503} and not quota and not delay:return
    from datetime import timedelta
    from app.database import AsyncSessionLocal
    from app.local_models import MailboxReadState
    from app.time import utcnow
    from sqlalchemy import case
    async with AsyncSessionLocal() as db:
        if db.bind.dialect.name=='postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        until=utcnow()+(delay or timedelta(minutes=5))
        stmt=insert(MailboxReadState).values(inbox_id=inbox_id,retry_after=until)
        await db.execute(stmt.on_conflict_do_update(index_elements=['inbox_id'],set_={'retry_after':case((MailboxReadState.retry_after>until,MailboxReadState.retry_after),else_=until)}))
        await db.commit()
    error._quickly_backoff_recorded=True

def read_limit_error(error):
    code=getattr(error,'status_code',0)
    return code in {429,503} or bool(getattr(error,'retry_after',None)) or code==403 and any(word in str(getattr(error,'body','')).lower() for word in ['ratelimitexceeded','quotaexceeded','dailylimitexceeded'])

_budgets=weakref.WeakKeyDictionary()
_mail_threads=ThreadPoolExecutor(max_workers=16,thread_name_prefix='quickly-mail')

class Budget:
    def __init__(self):
        self.condition=asyncio.Condition()
        self.active=0
        self.reading=0
        self.peak=0
        self.holders=set()

@asynccontextmanager
async def mail_slot(db, *, reading=False):
    from app.local_send import local_install
    if not local_install():
        yield
        return
    budget=_budgets.setdefault(asyncio.get_running_loop(),Budget())
    task=asyncio.current_task()
    if task in budget.holders:
        yield
        return
    # These operations already commit their durable intent/metadata before IO.
    # Release the caller's connection before waiting for admission; otherwise
    # waiting requests can occupy the entire SQL pool and starve send completion.
    if db is not None:await db.commit()
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as settings_db:
        control=await settings_db.get(SendControl,1)
    limit=max(1,min(16,getattr(control,'mail_concurrency',4) or 4))
    async with budget.condition:
        await budget.condition.wait_for(lambda:budget.active<limit and (not reading or budget.reading<max(1,limit//2)))
        budget.active+=1;budget.reading+=int(reading)
        budget.holders.add(task)
        budget.peak=max(budget.peak,budget.active)
    try:
        yield
    finally:
        async with budget.condition:
            budget.active-=1;budget.reading-=int(reading)
            budget.holders.discard(task)
            budget.condition.notify_all()

def bounded_io(*,reading=False):
    def decorate(function):
        @wraps(function)
        async def run(db,*args,**kwargs):
            async with mail_slot(db,reading=reading):
                from app.maintenance import is_restoring
                if reading and is_restoring():
                    raise RuntimeError('Выполняется восстановление базы; чтение отложено')
                from app.local_send import local_install
                iid=_inbox_id(args[0]) if args and reading and local_install() else None
                if iid:await _read_allowed(iid)
                try:return await function(db,*args,**kwargs)
                except Exception as error:
                    if iid:await _record_read_limit(iid,error)
                    raise
        return run
    return decorate

async def await_thread(function,*args,**kwargs):
    # Cancellation must not release a connection slot while its socket thread lives.
    context=contextvars.copy_context()
    task=asyncio.get_running_loop().run_in_executor(_mail_threads,partial(context.run,function,*args,**kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise

async def mail_thread(db,function,*args,**kwargs):
    """Bound administrative connection probes as well as message reads."""
    iid=kwargs.pop('mail_inbox_id',None) or (_inbox_id(args[0]) if args else None)
    from app.local_send import local_install
    if not local_install():iid=None
    async with mail_slot(db,reading=True):
        from app.maintenance import is_restoring
        if is_restoring():raise RuntimeError('Выполняется восстановление; проверка отложена')
        if iid:await _read_allowed(iid)
        try:return await await_thread(function,*args,**kwargs)
        except Exception as error:
            if iid:await _record_read_limit(iid,error)
            raise
