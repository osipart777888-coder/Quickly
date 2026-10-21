"""Crash consistency of diagnostic queue creation; all mail stays local or mocked."""
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from app import local_group_diagnostics as groups
from app.local_models import DiagnosticPlan,DiagnosticRun
from app.time import utcnow
from tests.test_group_diagnostics import make_group

@pytest.mark.asyncio
async def test_crash_after_enqueue_flush_rolls_back_schedule_and_rotation(session,monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    plan,_=await make_group(session,2)
    old=utcnow()-timedelta(minutes=1);plan.next_run_at=old;await session.commit()
    pid=plan.id
    original=groups.enqueue_group_cycle
    async def fail(db,plan_id,**kwargs):
        await original(db,plan_id,**kwargs)
        raise ValueError('injected crash before commit')
    monkeypatch.setattr(groups,'enqueue_group_cycle',fail)
    monkeypatch.setattr(groups,'in_window',lambda *a:True)
    await groups.run_group_jobs()
    async with groups.AsyncSessionLocal() as check:
        saved=await check.get(DiagnosticPlan,pid)
        assert saved.next_run_at==old
        assert saved.rotation_offset==0 and saved.last_batch_id is None
        assert await check.scalar(select(func.count(DiagnosticRun.id)))==0

@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    monkeypatch.setattr(groups,'in_window',lambda *a:True)

@pytest.mark.asyncio
@pytest.mark.parametrize('workers',[2,5])
async def test_concurrent_queue_requests_commit_one_complete_500_member_cycle(session,workers):
    import asyncio
    if session.bind.dialect.name!='postgresql':pytest.skip('PostgreSQL row lock race')
    plan,_=await make_group(session,500)
    pid=plan.id;old=utcnow()-timedelta(minutes=1);plan.next_run_at=old;await session.commit()
    async def enqueue():
        async with groups.AsyncSessionLocal() as db:
            result=await groups.enqueue_group_cycle(db,pid,scheduled_at=utcnow());await db.commit();return result
    results=await asyncio.gather(*(enqueue() for _ in range(workers)))
    async with groups.AsyncSessionLocal() as db:
        saved=await db.get(DiagnosticPlan,pid)
        assert saved.next_run_at>old and saved.rotation_offset==1 and saved.last_batch_id
        assert await db.scalar(select(func.count(DiagnosticRun.id)))==500
        assert await db.scalar(select(func.count(func.distinct(DiagnosticRun.batch_id))))==1
        assert sum(r['queued'] for r in results)==500
        assert (await groups.enqueue_group_cycle(db,pid))['queued']==0

@pytest.mark.asyncio
async def test_two_schedulers_do_not_duplicate_and_committed_queue_survives_worker_failure(session,monkeypatch):
    import asyncio
    if session.bind.dialect.name!='postgresql':pytest.skip('PostgreSQL row lock race')
    plan,_=await make_group(session,25,next_run_at=utcnow()-timedelta(minutes=1))
    async def crash_after_commit(_):raise RuntimeError('worker process disappeared after queue commit')
    monkeypatch.setattr(groups,'dispatch_run',crash_after_commit)
    await asyncio.gather(groups.run_group_jobs(),groups.run_group_jobs())
    # A new session is the first operation of the restarted worker.
    async with groups.AsyncSessionLocal() as db:
        saved=await db.get(DiagnosticPlan,plan.id)
        assert saved.next_run_at>utcnow() and saved.rotation_offset==1
        assert await db.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.status=='queued'))==25
        assert (await groups.enqueue_group_cycle(db,plan.id))['pending']==25

@pytest.mark.asyncio
async def test_large_group_cannot_starve_small_group(session,monkeypatch):
    large,_=await make_group(session,25)
    small,_=await make_group(session,2)
    for plan in [large,small]:await groups.enqueue_group_cycle(session,plan.id)
    await session.commit();called=[]
    async def dispatch(rid):called.append(rid)
    monkeypatch.setattr(groups,'dispatch_run',dispatch)
    await groups.run_group_jobs()
    selected=(await session.execute(select(DiagnosticRun.plan_id).where(DiagnosticRun.id.in_(called)))).scalars().all()
    assert small.id in selected and large.id in selected and len(called)==groups.DISPATCH_PER_TICK
