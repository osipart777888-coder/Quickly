"""Bounded, persistent diagnostic queues for groups of owned mailboxes."""
from __future__ import annotations
import asyncio
from datetime import timedelta, timezone
import json
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy import and_, func, or_, select, update
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
        .where(Inbox.id.in_(ids)).order_by(Inbox.id))).all()
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
        result.append({'id':inbox.id,'email':inbox.email,'provider':inbox.provider,'eligible':not reason,'reason':reason})
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


async def enqueue_group_cycle(db, plan_id):
    plan = await db.scalar(select(DiagnosticPlan).where(DiagnosticPlan.id == plan_id).with_for_update().execution_options(populate_existing=True))
    if not plan or plan.mode != 'group':
        raise ValueError('Групповое расписание не найдено')
    control = await db.get(SendControl,1,populate_existing=True)
    if control and control.emergency_stopped:
        raise ValueError('Аварийная остановка включена')
    pending = await db.scalar(select(func.count(DiagnosticRun.id)).where(DiagnosticRun.plan_id == plan.id,
        DiagnosticRun.status.in_(['queued','dispatching']))) or 0
    if pending:
        # Repeated clicks and competing schedulers do not create another batch.
        return {'queued':0,'pending':pending,'batch_id':plan.last_batch_id,'excluded':[]}
    members = await group_members(db,plan.inbox_ids)
    pairs = ring_pairs(members,plan.rotation_offset)
    if not pairs:
        raise ValueError('Нужны минимум два доступных подключённых ящика в группе')
    batch_id = uuid.uuid4().hex
    for pair in pairs:
        db.add(DiagnosticRun(id=uuid.uuid4().hex,plan_id=plan.id,batch_id=batch_id,
            sender_id=pair['sender_id'],recipient_id=pair['recipient_id'],status='queued',
            report={'note':'Диагностическая проверка в очереди. Письмо ещё не отправлялось.'}))
    plan.rotation_offset += 1
    plan.last_batch_id = batch_id
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
        claimed = await db.scalar(update(DiagnosticRun).where(DiagnosticRun.id == run_id,DiagnosticRun.status == 'queued')
            .values(status='dispatching',started_at=utcnow()).returning(DiagnosticRun.id))
        await db.commit()
        if not claimed:
            return
        members = await group_members(db,[run.sender_id,run.recipient_id])
        if any(not item['eligible'] for item in members) or len(members) != 2:
            run.status = 'skipped'
            run.error = '; '.join(r['reason'] for r in members if not r['eligible'])
            await db.commit()
            return
        await execute_diagnostic(db,run,plan)


async def poll_run(run_id):
    from app.local_diagnostics import check_receipt, send_diagnostic_reply
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
        # Claim a polling slot so repeated failures do not starve later mailboxes.
        due = await db.scalar(update(DiagnosticRun).where(DiagnosticRun.id == run.id,
            or_(DiagnosticRun.last_checked_at.is_(None),DiagnosticRun.last_checked_at < utcnow()-timedelta(seconds=45)))
            .values(last_checked_at=utcnow()).returning(DiagnosticRun.id))
        await db.commit()
        if not due:
            return
        if run.status == 'accepted':
            run = await check_receipt(db,run)
        if run.reply_message_id:
            await check_receipt(db,run,reply=True)
        elif plan and plan.mode == 'group' and plan.auto_reply and run.status == 'received' and in_window(plan):
            # Persist the reply claim first; failures, quota blocks and crashes are visible,
            # never silently retried or moved to a different sender.
            claim = await db.scalar(update(DiagnosticRun).where(DiagnosticRun.id == run.id,
                DiagnosticRun.status == 'received').values(status='reply_pending',started_at=utcnow()).returning(DiagnosticRun.id))
            await db.commit()
            if claim:
                try:
                    await send_diagnostic_reply(db,run,allow_claimed=True)
                    run.status = 'reply_sent'
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
    from app.local_diagnostics import start_diagnostic
    async with AsyncSessionLocal() as db:
        control = await db.get(SendControl,1)
        if control and control.emergency_stopped:
            return
        now = utcnow()
        # Explicit stale states are retained as uncertain; no job reclaims them.
        await db.execute(update(DiagnosticRun).where(DiagnosticRun.status.in_(['dispatching','reply_pending']),
            DiagnosticRun.started_at < now-timedelta(minutes=10)).values(status='uncertain',
                error='Обработка прервалась. Проверьте исходящие; автоматического повтора не будет.'))
        await db.commit()
        plan_ids = (await db.execute(select(DiagnosticPlan.id).where(DiagnosticPlan.enabled == True,
            DiagnosticPlan.next_run_at <= now).order_by(DiagnosticPlan.next_run_at,DiagnosticPlan.id).limit(20))).scalars().all()
        for plan_id in plan_ids:
            # A rejected plan rolls back its transaction. Reload the next plan
            # explicitly rather than touching objects expired by that rollback.
            plan = await db.get(DiagnosticPlan,plan_id,populate_existing=True)
            if not plan or not plan.enabled:
                continue
            if not in_window(plan):
                continue
            claimed = await db.scalar(update(DiagnosticPlan).where(DiagnosticPlan.id == plan.id,
                DiagnosticPlan.enabled == True,DiagnosticPlan.next_run_at <= now)
                .values(next_run_at=now+timedelta(minutes=plan.interval_minutes)).returning(DiagnosticPlan.id))
            await db.commit()
            if claimed:
                try:
                    if plan.mode == 'group':
                        await enqueue_group_cycle(db,plan.id)
                        await db.commit()
                    else:
                        await start_diagnostic(db,plan.sender_id,plan.recipient_id,plan)
                except ValueError:
                    await db.rollback()
        queued = (await db.execute(select(DiagnosticRun.id,DiagnosticPlan).join(DiagnosticPlan)
            .where(DiagnosticRun.status == 'queued',DiagnosticPlan.enabled == True)
            .order_by(DiagnosticRun.created_at,DiagnosticRun.id).limit(2000))).all()
        ids = [rid for rid,plan in queued if in_window(plan)][:DISPATCH_PER_TICK]
    await bounded(ids,dispatch_run)
    async with AsyncSessionLocal() as db:
        pending = (await db.execute(select(DiagnosticRun.id).outerjoin(DiagnosticPlan).where(
            or_(DiagnosticRun.plan_id.is_(None),DiagnosticPlan.enabled == True),
            or_(DiagnosticRun.status.in_(['accepted','reply_sent']),and_(DiagnosticRun.status == 'received',DiagnosticPlan.mode == 'group',DiagnosticPlan.auto_reply == True,DiagnosticPlan.enabled == True)),
            DiagnosticRun.created_at >= utcnow()-timedelta(hours=24),
            or_(DiagnosticRun.last_checked_at.is_(None),DiagnosticRun.last_checked_at < utcnow()-timedelta(minutes=1)))
            .order_by(DiagnosticRun.last_checked_at.asc().nullsfirst(),DiagnosticRun.created_at).limit(20))).scalars().all()
    await bounded(pending,poll_run)
