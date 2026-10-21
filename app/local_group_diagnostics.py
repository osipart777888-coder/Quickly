"""Bounded, persistent diagnostic queues for groups of owned mailboxes."""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import json
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import load_only
from app.database import AsyncSessionLocal
from app.local_models import DiagnosticPlan, DiagnosticRun, MailOperation, SendControl
from app.models import AppSetting, GmailAccount, Inbox, Office365Account, SmtpAccount
from app.time import utcnow

DISPATCH_PER_TICK = 20
CONCURRENCY = 4


def in_window(plan, now=None):
    local = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
    return local.weekday() in plan.sending_days and plan.hours_start <= local.strftime('%H:%M') < plan.hours_end


async def group_members(db, inbox_ids):
    """Batch metadata inspection only: preview never logs in or sends mail."""
    ids = sorted(set(inbox_ids))
    rows = (await db.execute(select(Inbox, SmtpAccount, GmailAccount.id, Office365Account.id)
        .outerjoin(SmtpAccount, SmtpAccount.inbox_id == Inbox.id)
        .outerjoin(GmailAccount, GmailAccount.inbox_id == Inbox.id)
        .outerjoin(Office365Account, Office365Account.inbox_id == Inbox.id)
        .where(Inbox.id.in_(ids)).options(load_only(Inbox.id,Inbox.email,Inbox.provider,Inbox.paused),
            load_only(SmtpAccount.inbox_id,SmtpAccount.smtp_host,SmtpAccount.imap_host,SmtpAccount.last_tested_at,SmtpAccount.last_test_ok,SmtpAccount.last_send_error,SmtpAccount.last_send_at)).order_by(Inbox.id))).all()
    health_rows = (await db.execute(select(AppSetting).where(AppSetting.key.in_(
        [f'local_connection_health_{iid}' for iid in ids])))).scalars().all()
    health = {}
    for setting in health_rows:
        try:
            health[int(setting.key.rsplit('_',1)[1])] = json.loads(setting.value)
        except (ValueError,TypeError):
            pass
    result = []
    for inbox, smtp, gmail_id, microsoft_id in rows:
        reason = ''
        if inbox.paused:
            reason = 'Ящик приостановлен'
        elif inbox.provider == 'smtp':
            if not smtp or not smtp.smtp_host or not smtp.imap_host:
                reason = 'Для групповой проверки нужны SMTP и IMAP'
            elif smtp.last_tested_at and not smtp.last_test_ok:
                reason = 'Последняя проверка подключения завершилась ошибкой'
            elif smtp.last_send_error and not (smtp.last_test_ok and smtp.last_tested_at and smtp.last_send_at and smtp.last_tested_at > smtp.last_send_at):
                reason = 'Не устранена ошибка отправки'
        elif inbox.provider == 'gmail' and not gmail_id:
            reason = 'Google OAuth не подключён'
        elif inbox.provider == 'office365' and not microsoft_id:
            reason = 'Microsoft OAuth не подключён'
        elif inbox.provider not in {'smtp','gmail','office365'}:
            reason = 'Неизвестный способ подключения'
        if not reason and any(health.get(inbox.id, {}).get(protocol) == 'error' for protocol in ('smtp','imap')):
            reason = 'Проверка подключения сообщает об ошибке'
        read_reason=''
        if inbox.paused:read_reason='Ящик приостановлен'
        elif inbox.provider=='smtp' and (not smtp or not smtp.imap_host):read_reason='IMAP не подключён'
        elif inbox.provider=='gmail' and not gmail_id:read_reason='Google OAuth не подключён'
        elif inbox.provider=='office365' and not microsoft_id:read_reason='Microsoft OAuth не подключён'
        elif inbox.provider not in {'smtp','gmail','office365'}:read_reason='Неизвестный способ подключения'
        if health.get(inbox.id,{}).get('imap')=='error':read_reason='Проверка IMAP сообщает об ошибке'
        result.append({'id':inbox.id,'email':inbox.email,'provider':inbox.provider,'eligible':not reason,'reason':reason,
                       'read_eligible':not read_reason,'read_reason':read_reason})
    present = {r['id'] for r in result}
    result.extend({'id':iid,'email':'','provider':'','eligible':False,'reason':'Ящик удалён'} for iid in ids if iid not in present)
    return result


def ring_pairs(members, rotation=0):
    available = sorted((r for r in members if r['eligible']),key=lambda r:r['id'])
    if len(available) < 2:
        return []
    offset = 1 + rotation % (len(available)-1)
    return [{'sender_id':sender['id'],'sender':sender['email'],
             'recipient_id':available[(index+offset)%len(available)]['id'],
             'recipient':available[(index+offset)%len(available)]['email']}
            for index,sender in enumerate(available)]


async def enqueue_group_cycle(db, plan_id, *, scheduled_at=None):
    # The row lock covers eligibility, queue, rotation AND the next due date.
    # Only the caller commits; no network operation is performed here.
    plan = await db.scalar(select(DiagnosticPlan).where(DiagnosticPlan.id == plan_id)
        .with_for_update(skip_locked=scheduled_at is not None).execution_options(populate_existing=True))
    if scheduled_at is not None and (not plan or not plan.enabled or not plan.next_run_at
            or plan.next_run_at > scheduled_at or not in_window(plan,scheduled_at)):
        return {'queued':0,'pending':0,'batch_id':None,'excluded':[]}
    if not plan or (plan.mode != 'group' and scheduled_at is None):
        raise ValueError('Групповое расписание не найдено')
    control = await db.get(SendControl,1,populate_existing=True)
    if control and control.emergency_stopped:
        raise ValueError('Аварийная остановка включена')
    pending = await db.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.plan_id == plan.id,
        DiagnosticRun.status.in_(['queued','dispatching']))) or 0
    if pending:
        # Repeated clicks and competing schedulers do not create another batch.
        return {'queued':0,'pending':pending,'batch_id':plan.last_batch_id,'excluded':[]}
    members = await group_members(db,plan.inbox_ids if plan.mode=='group' else [plan.sender_id,plan.recipient_id])
    pairs = ring_pairs(members,plan.rotation_offset) if plan.mode == 'group' else [
        {'sender_id':plan.sender_id,'recipient_id':plan.recipient_id}]
    if plan.mode != 'group' and (len(members)!=2 or any(m['reason'] in {'Ящик приостановлен','Ящик удалён'} for m in members)):
        pairs = []
    if not pairs:
        raise ValueError('Нужны минимум два доступных подключённых ящика в группе')
    batch_id = uuid.uuid4().hex
    addresses={m['id']:m['email'] for m in members}
    for pair in pairs:
        db.add(DiagnosticRun(id=uuid.uuid4().hex,plan_id=plan.id,batch_id=batch_id,
            sender_id=pair['sender_id'],recipient_id=pair['recipient_id'],sender_email=addresses[pair['sender_id']],recipient_email=addresses[pair['recipient_id']],status='queued',
            report={'note':'Диагностическая проверка в очереди. Письмо ещё не отправлялось.'}))
    plan.rotation_offset += 1
    plan.last_batch_id = batch_id
    if scheduled_at is not None:
        plan.next_run_at = scheduled_at+timedelta(minutes=plan.interval_minutes)
    await db.flush()
    return {'queued':len(pairs),'pending':len(pairs),'batch_id':batch_id,
            'excluded':[r for r in members if not r['eligible']]}


async def dispatch_run(run_id):
    """Claim before network work; a crash never blindly replays a claim."""
    from app.local_diagnostics import execute_diagnostic
    async with AsyncSessionLocal() as db:
        run = await db.get(DiagnosticRun,run_id)
        if not run or run.status != 'queued':
            return
        plan = await db.get(DiagnosticPlan,run.plan_id,populate_existing=True)
        control = await db.get(SendControl,1,populate_existing=True)
        if not plan or not plan.enabled or not in_window(plan) or (control and control.emergency_stopped):
            return
        from app.local_send import sender_ready
        sender = await db.scalar(select(Inbox).where(Inbox.id==run.sender_id).options(load_only(Inbox.id,Inbox.wait_minutes_between)).execution_options(populate_existing=True))
        if sender and not await sender_ready(db,sender):
            return
        claimed = await db.scalar(update(DiagnosticRun).where(DiagnosticRun.id == run_id,DiagnosticRun.status == 'queued')
            .values(status='dispatching',started_at=utcnow()).returning(DiagnosticRun.id))
        if claimed:
            plan.last_dispatched_at=utcnow()
        await db.commit()
        if not claimed:
            return
        members = await group_members(db,[run.sender_id,run.recipient_id])
        sender_member=next((m for m in members if m['id']==run.sender_id),{})
        recipient_member=next((m for m in members if m['id']==run.recipient_id),{})
        # An outgoing SMTP failure does not prove this mailbox cannot receive.
        ineligible=(not sender_member.get('eligible') or not recipient_member.get('read_eligible')) if plan.mode=='group' else any(item['reason'] in {'Ящик приостановлен','Ящик удалён'} for item in members)
        if ineligible or len(members) != 2:
            run.status = 'skipped'
            run.error = '; '.join(filter(None,[sender_member.get('reason'),recipient_member.get('read_reason') or recipient_member.get('reason')]))
            await db.commit()
            return
        await execute_diagnostic(db,run,plan)


async def poll_run(run_id):
    from app.local_diagnostics import check_receipt, send_diagnostic_reply, DiagnosticDeferred
    async with AsyncSessionLocal() as db:
        run = await db.get(DiagnosticRun,run_id)
        if not run:
            return
        plan = await db.get(DiagnosticPlan,run.plan_id,populate_existing=True) if run.plan_id else None
        if run.plan_id and (not plan or not plan.enabled):
            return
        control = await db.get(SendControl,1,populate_existing=True)
        if control and control.emergency_stopped:
            return
        if run.status in {'expired','reply_expired'}:
            return
        # Claim a polling slot so repeated failures do not starve later mailboxes.
        due = await db.scalar(update(DiagnosticRun).where(DiagnosticRun.id == run.id,
            or_(DiagnosticRun.next_check_at.is_(None),DiagnosticRun.next_check_at <= utcnow()))
            .values(next_check_at=utcnow()+timedelta(seconds=(plan.poll_interval_seconds if plan else 300))).returning(DiagnosticRun.id))
        await db.commit()
        if not due:
            return
        if run.status == 'accepted':
            run = await check_receipt(db,run,manual=False)
        if run.reply_message_id:
            await check_receipt(db,run,reply=True,manual=False)
        elif plan and plan.mode == 'group' and plan.auto_reply and run.status == 'received' and in_window(plan) and (not run.observation_deadline or run.observation_deadline>utcnow()):
            from app.local_send import sender_ready
            sender = await db.get(Inbox,run.recipient_id,populate_existing=True)
            if sender and not await sender_ready(db,sender):
                return
            # Persist the reply claim first; failures, quota blocks and crashes are visible,
            # never silently retried or moved to a different sender.
            claim = await db.scalar(update(DiagnosticRun).where(DiagnosticRun.id == run.id,
                DiagnosticRun.status == 'received').values(status='reply_pending',reply_started_at=utcnow()).returning(DiagnosticRun.id))
            await db.commit()
            if claim:
                try:
                    await send_diagnostic_reply(db,run,allow_claimed=True)
                    run.status = 'reply_sent'
                except DiagnosticDeferred:
                    run.status = 'received'
                    run.started_at = None
                except ValueError as exc:
                    run.status = 'reply_stopped'
                    run.error = str(exc)[:2000]
                await db.commit()


async def bounded(items, worker):
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async def one(item):
        async with semaphore:
            try:
                await worker(item)
            except Exception:
                import logging
                logging.getLogger('quickly.diagnostics').exception('Diagnostic worker failed; committed claim will not be replayed')
    await asyncio.gather(*(one(item) for item in items))


async def run_group_jobs():
    async with AsyncSessionLocal() as db:
        control = await db.get(SendControl,1)
        now = utcnow()
        # Legacy runs get a persisted deadline; expiration also works while schedules are paused.
        from app.diagnostic_observation import deadline_for
        observing=(await db.execute(select(DiagnosticRun).where(or_(
            and_(DiagnosticRun.status=='accepted',or_(DiagnosticRun.observation_deadline.is_(None),DiagnosticRun.observation_deadline<=now)),
            and_(DiagnosticRun.status=='reply_sent',or_(DiagnosticRun.reply_observation_deadline.is_(None),DiagnosticRun.reply_observation_deadline<=now)))))).scalars().all()
        observation_plans={p.id:p for p in (await db.execute(select(DiagnosticPlan).where(DiagnosticPlan.id.in_({r.plan_id for r in observing if r.plan_id})))).scalars().all()}
        for run in observing:
            plan=observation_plans.get(run.plan_id)
            if deadline_for(run,plan,reply=run.status=='reply_sent')<=now:
                run.observation_status='expired'
                run.status='reply_expired' if run.status=='reply_sent' else 'expired'
                run.next_check_at=None
        await db.commit()
        if control and control.emergency_stopped:
            return
        # Explicit stale states are retained as uncertain; no job reclaims them.
        await db.execute(update(DiagnosticRun).where(or_(and_(DiagnosticRun.status=='dispatching',DiagnosticRun.started_at<now-timedelta(minutes=10)),
                and_(DiagnosticRun.status=='reply_pending',func.coalesce(DiagnosticRun.reply_started_at,DiagnosticRun.started_at)<now-timedelta(minutes=10)))).values(status='uncertain',
                error='Обработка прервалась. Проверьте исходящие; автоматического повтора не будет.'))
        await db.commit()
        enabled_plans = (await db.execute(select(DiagnosticPlan).where(DiagnosticPlan.enabled == True)
            .order_by(DiagnosticPlan.next_run_at,DiagnosticPlan.id))).scalars().all()
        open_plans = [p for p in enabled_plans if in_window(p,now)]
        open_ids = [p.id for p in open_plans]
        ordered_plan_ids=[p.id for p in sorted(open_plans,key=lambda p:(p.last_dispatched_at or datetime.min,p.id))]
        busy_ids=set((await db.execute(select(DiagnosticRun.plan_id).where(DiagnosticRun.status.in_(['queued','dispatching'])).distinct())).scalars().all())
        plan_ids = [p.id for p in open_plans if p.id not in busy_ids and p.next_run_at and p.next_run_at <= now][:20]
        for plan_id in plan_ids:
            # A rejected plan rolls back its transaction. Reload the next plan
            # explicitly rather than touching objects expired by that rollback.
            plan = await db.get(DiagnosticPlan,plan_id,populate_existing=True)
            if not plan or not plan.enabled:
                continue
            if not in_window(plan):
                continue
            try:
                await enqueue_group_cycle(db,plan.id,scheduled_at=now)
                await db.commit()
            except ValueError:
                await db.rollback()
        queued = (await db.execute(select(DiagnosticRun.id,DiagnosticRun.sender_id,DiagnosticRun.plan_id)
            .where(DiagnosticRun.status == 'queued',DiagnosticRun.plan_id.in_(open_ids))
            .order_by(DiagnosticRun.created_at,DiagnosticRun.id))).all()
        # Waiting senders do not occupy all 20 dispatch positions.
        senders = {i.id:i for i in (await db.execute(select(Inbox).where(Inbox.id.in_({iid for _,iid,_ in queued})).options(load_only(Inbox.id,Inbox.wait_minutes_between)))).scalars().all()}
        from app.local_send import ready_sender_ids
        ready = await ready_sender_ids(db,list(senders.values()),now=now)
        from collections import defaultdict,deque
        by_plan=defaultdict(deque)
        for rid,iid,pid in queued:
            if iid in ready:by_plan[pid].append(rid)
        ids=[]
        while len(ids)<DISPATCH_PER_TICK and any(by_plan.values()):
            for pid in ordered_plan_ids:
                if by_plan[pid] and len(ids)<DISPATCH_PER_TICK:ids.append(by_plan[pid].popleft())
    await bounded(ids,dispatch_run)
    async with AsyncSessionLocal() as db:
        pending = (await db.execute(select(DiagnosticRun.id).outerjoin(DiagnosticPlan).where(
            or_(DiagnosticRun.plan_id.is_(None),DiagnosticPlan.enabled == True),
            or_(DiagnosticRun.status.in_(['accepted','reply_sent']),and_(DiagnosticRun.status == 'received',DiagnosticPlan.mode == 'group',DiagnosticPlan.auto_reply == True,DiagnosticPlan.enabled == True)),
            or_(DiagnosticRun.status != 'received',DiagnosticRun.observation_deadline.is_(None),DiagnosticRun.observation_deadline>utcnow()),
            or_(DiagnosticRun.next_check_at.is_(None),DiagnosticRun.next_check_at <= utcnow()))
            .order_by(DiagnosticRun.last_checked_at.asc().nullsfirst(),DiagnosticRun.created_at).limit(20))).scalars().all()
    await bounded(pending,poll_run)
