"""Fail-closed send controls, atomic daily budgets and durable replay prevention.

SMTP/API transport cannot promise exactly-once delivery across a network crash.
A committed reservation with an unknown result is deliberately never retried.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import uuid
from app.mail_io import bounded_io, await_thread
from zoneinfo import ZoneInfo
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from app.local_models import MailOperation, SenderDayQuota, SendControl, Suppression, SenderPacing
from app.models import EmailLog, Lead, Campaign, AppSetting
from app.queue_logic import compute_effective_daily_limit
from app.sender import SendFailure, SendResult
from app.time import utcnow


log = logging.getLogger("quickly.local_send")


def local_install() -> bool:
    return os.getenv('QUICKLY_LOCAL_INSTALL', '').lower() == 'true'


def day_bounds():
    zone = ZoneInfo(os.getenv('QUICKLY_QUOTA_TIMEZONE', 'Europe/Moscow'))
    now = utcnow().replace(tzinfo=timezone.utc).astimezone(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.date().isoformat(), start.astimezone(timezone.utc).replace(tzinfo=None), (start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)


async def campaign_sending_allowed(db) -> bool:
    if not local_install():
        return True
    control = await db.get(SendControl, 1, populate_existing=True)
    return bool(control and control.campaigns_enabled and not control.emergency_stopped)


async def contact_send_error(db, email, lead=None):
    from app.local_imports import email_address
    try:
        email = email_address(email)
    except ValueError:
        return 'Некорректный email получателя'
    if await db.get(Suppression, email):
        return 'Адрес в чёрном списке'
    import hashlib
    if await db.get(Suppression, 'sha256:' + hashlib.sha256(email.encode()).hexdigest()):
        return 'Контакт удалён и запрещён для повторного использования'
    if lead is not None:
        from app.collector.consent import marketing_error
        error = await marketing_error(db, email)
        if error:
            return error
    contacts = (await db.execute(select(Lead).where(func.lower(Lead.email) == email))).scalars().all()
    if any(item.status in {'unsubscribed', 'invalid', 'bounced', 'blacklisted'} for item in contacts):
        return 'Получатель отписался, заблокирован или адрес недействителен'
    return ''


def public_links_error(inbox):
    from urllib.parse import urlparse
    import ipaddress
    from app.app_settings import get_inbox_tracking_base
    from app.settings_manager import settings
    url = urlparse(get_inbox_tracking_base(inbox, settings.base_url.rstrip('/')))
    host = url.hostname or ''
    if url.scheme != 'https' or not host or host == 'localhost' or host.endswith(('.localhost', '.local')):
        return 'Для кампании нужен публичный HTTPS-домен ссылок отписки в настройках ящика'
    try:
        if not ipaddress.ip_address(host).is_global:
            return 'Домен ссылок отписки должен быть доступен получателям из интернета'
    except ValueError:
        pass
    return ''


class DeferredReason(str):
    pass


async def sender_ready(db, inbox, *, now=None):
    """Read-only readiness; the definitive pacing claim is atomic in reserve_operation."""
    return inbox.id in await ready_sender_ids(db,[inbox],now=now)


async def ready_sender_ids(db, inboxes, *, now=None):
    now = now or utcnow()
    if not inboxes:
        return set()
    ids = [i.id for i in inboxes]
    last = dict((await db.execute(select(SenderPacing.inbox_id,SenderPacing.last_reserved_at)
        .where(SenderPacing.inbox_id.in_(ids)))).all())
    missing = set(ids)-last.keys()
    if missing:
        last.update(dict((await db.execute(select(EmailLog.inbox_id,func.max(EmailLog.sent_at))
            .where(EmailLog.inbox_id.in_(missing)).group_by(EmailLog.inbox_id))).all()))
    return {i.id for i in inboxes if not last.get(i.id) or last[i.id]+timedelta(minutes=max(0,i.wait_minutes_between or 0)) <= now}


async def authorize_rejected_retries(db, inbox_id):
    """Explicit resume authorizes only known pre-delivery authentication refusals."""
    rows = (await db.execute(select(MailOperation).where(MailOperation.inbox_id == inbox_id,
        MailOperation.kind == 'campaign',MailOperation.status == 'rejected',MailOperation.retry_safe == True)
        .with_for_update())).scalars().all()
    for row in rows:
        # Keep the refused attempt and its quota charge; free the business key
        # for one new atomic claim. Accepted/uncertain operations are never changed.
        row.operation_key = row.operation_key[:210] + ':retry-' + row.id
        row.retry_safe = False
        row.error += ' · Повтор разрешён владельцем при возобновлении ящика'
    await db.flush()
    return len(rows)


async def reserve_operation(db, inbox, *, operation_key, recipient, kind, lead=None, campaign_id=None,
                            diagnostic_plan_id=None, diagnostic_initial=False):
    """Commit mailbox/group budgets, pacing and the unique intent together before network."""
    control = await db.get(SendControl, 1, populate_existing=True)
    if control and control.emergency_stopped:
        return None, 'Аварийная остановка включена'
    if kind == 'campaign' and not (control and control.campaigns_enabled):
        return None, 'Реальная отправка кампаний не разрешена пользователем'
    if kind == 'campaign':
        campaign = await db.get(Campaign, campaign_id, populate_existing=True)
        if not campaign or campaign.paused or await db.get(AppSetting, f'local_cancelled_campaign_{campaign_id}'):
            return None, 'Кампания приостановлена или отменена'
        error = public_links_error(inbox)
        if error:
            return None, error
    await db.refresh(inbox, attribute_names=['paused', 'max_emails_per_day', 'wait_minutes_between'])
    if inbox.paused:
        return None, 'Ящик приостановлен'
    error = await contact_send_error(db, recipient, lead if kind == 'campaign' else None)
    if error:
        return None, error
    if await db.scalar(select(MailOperation.id).where(MailOperation.operation_key == operation_key)):
        return None, 'Эта операция уже выполнялась; автоматический повтор запрещён'
    day, start, end = day_bounds()
    limit = max(0, compute_effective_daily_limit(inbox, for_date=datetime.fromisoformat(day).date()))
    existing = await db.scalar(select(func.count(EmailLog.id)).where(
        EmailLog.inbox_id == inbox.id, EmailLog.sent_at >= start, EmailLog.sent_at < end,
        EmailLog.message_id.isnot(None), EmailLog.message_id != '',
    )) or 0
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await db.execute(insert(SenderDayQuota).values(inbox_id=inbox.id, day=day, used=existing)
        .on_conflict_do_nothing(index_elements=['inbox_id', 'day']))
    last = await db.scalar(select(func.max(EmailLog.sent_at)).where(EmailLog.inbox_id == inbox.id))
    await db.execute(insert(SenderPacing).values(inbox_id=inbox.id,last_reserved_at=last or datetime(1970,1,1))
        .on_conflict_do_nothing(index_elements=['inbox_id']))
    now = utcnow()
    operation = MailOperation(operation_key=operation_key, inbox_id=inbox.id, recipient=recipient,
                              kind=kind, campaign_id=campaign_id, status='reserved')
    try:
        async with db.begin_nested():
            condition = [SenderPacing.inbox_id == inbox.id]
            if kind in {'campaign','diagnostic'}:
                condition.append(SenderPacing.last_reserved_at <= now-timedelta(minutes=max(0,inbox.wait_minutes_between or 0)))
            paced = await db.scalar(update(SenderPacing).where(*condition).values(last_reserved_at=now)
                .returning(SenderPacing.inbox_id))
            if paced is None:
                raise ValueError(DeferredReason('Пауза между автоматическими письмами ещё не закончилась'))
            db.add(operation)
            await db.flush()
            used = await db.scalar(update(SenderDayQuota).where(
                SenderDayQuota.inbox_id == inbox.id, SenderDayQuota.day == day, SenderDayQuota.used < limit,
            ).values(used=SenderDayQuota.used + 1).returning(SenderDayQuota.used))
            if used is None:
                raise ValueError(f'Дневной лимит ящика исчерпан ({limit})')
            if diagnostic_plan_id:
                from app.local_models import DiagnosticPlan
                from app.local_diagnostics import plan_budget
                plan = await db.get(DiagnosticPlan,diagnostic_plan_id,populate_existing=True)
                if not plan or not plan.enabled:
                    raise ValueError('Расписание диагностики остановлено')
                await plan_budget(db,plan,inbox.id if diagnostic_initial else None,commit=False)
    except IntegrityError:
        return None, 'Эта операция уже зарезервирована другим обработчиком'
    except ValueError as exc:
        return None, exc.args[0]
    await db.commit()
    return operation, ''


@bounded_io()
async def guarded_send(db, inbox, *, operation_key=None, kind='manual', lead=None, campaign_id=None, diagnostic_plan_id=None, diagnostic_initial=False, send, **kwargs):
    if kind == 'campaign':
        from app.collector.consent import marketing_error
        permission_error = await marketing_error(db, kwargs.get('to_email', '').lower())
        if permission_error:
            return SendFailure('local_block', permission_error)
    from app.maintenance import is_restoring
    if is_restoring():
        return SendFailure('local_deferred','Выполняется восстановление базы; отправка отложена')
    if not local_install():
        return await await_thread(send, **kwargs)
    from app.local_models import DiagnosticPlan
    if diagnostic_plan_id and not await db.scalar(select(DiagnosticPlan.enabled).where(DiagnosticPlan.id == diagnostic_plan_id)):
        return SendFailure('local_block', 'Расписание диагностики остановлено')
    key = operation_key or f'{kind}:{uuid.uuid4().hex}'
    operation, error = await reserve_operation(db, inbox, operation_key=key, recipient=kwargs.get('to_email', ''),
                                             kind=kind, lead=lead, campaign_id=campaign_id, diagnostic_plan_id=diagnostic_plan_id, diagnostic_initial=diagnostic_initial)
    if error:
        return SendFailure('local_deferred' if isinstance(error,DeferredReason) else 'local_block', str(error))
    # A stop pressed after reservation still prevents a send that has not begun.
    control = await db.get(SendControl, 1, populate_existing=True)
    plan_stopped = diagnostic_plan_id and not await db.scalar(select(DiagnosticPlan.enabled).where(DiagnosticPlan.id == diagnostic_plan_id))
    if is_restoring() or plan_stopped or (control and (control.emergency_stopped or (kind == 'campaign' and not control.campaigns_enabled))):
        operation.status = 'cancelled'
        operation.error = 'Отправка остановлена до обращения к почтовому серверу'
        operation.finished_at = utcnow()
        await db.commit()
        return SendFailure('local_block', operation.error)
    await db.commit()  # no idle SQL transaction while a socket is blocked
    try:
        result = await await_thread(send, **kwargs)
    except BaseException:
        # Cancellation/crash leaves a committed intent: never replay it blindly.
        operation.status = 'uncertain'
        operation.error = 'Соединение прервано; результат нужно проверить вручную'
        operation.finished_at = utcnow()
        await db.commit()
        raise
    operation.finished_at = utcnow()
    if isinstance(result, SendResult) or result is True:
        operation.status = 'accepted'
        operation.message_id = getattr(result, 'message_id', '') or ''
        # Keep every accepted SMTP message available in the local mailbox,
        # including diagnostics and manual test sends.  Campaign and Unibox
        # callers may upsert the same Message-ID again; the helper is
        # idempotent.  A mirror failure must not change the accepted result.
        if isinstance(result, SendResult) and getattr(inbox, 'provider', '') == 'smtp':
            try:
                async with db.begin_nested():
                    from app.unibox import upsert_sent_smtp_message

                    await upsert_sent_smtp_message(
                        db,
                        inbox_id=inbox.id,
                        thread_key=result.thread_id or result.message_id,
                        internet_message_id=result.message_id,
                        subject=kwargs.get('subject', '') or '',
                        to_email=kwargs.get('to_email', '') or '',
                        from_email=kwargs.get('from_email', '') or inbox.email or '',
                        body=kwargs.get('body', '') or '',
                        is_html=bool(kwargs.get('is_html', False)),
                    )
            except Exception:
                log.warning(
                    "SMTP accepted but local sent copy could not be saved inbox_id=%s",
                    inbox.id,
                    exc_info=True,
                )
    elif isinstance(result, SendFailure):
        operation.status = 'rejected'
        operation.error = result.message[:2000]
        operation.retry_safe = result.error_type in {'auth_failed','permission_denied'}
        if result.error_type in {'auth_failed', 'permission_denied'}:
            inbox.paused = True
        # Include this refusal in the persisted threshold calculation.
        await db.flush()
        # Conservative account-level circuit breaker, with no sender rotation.
        recent = (await db.execute(select(MailOperation.status).where(
            MailOperation.inbox_id == inbox.id, MailOperation.created_at >= utcnow() - timedelta(hours=24),
        ).order_by(MailOperation.created_at.desc()).limit(20))).scalars().all()
        if len(recent) >= 3 and recent.count('rejected') >= 3 and recent.count('rejected') / len(recent) >= .3:
            inbox.paused = True
            operation.error += ' · Ящик приостановлен: не менее 3 отказов и 30% отказов за сутки'
    else:
        operation.status = 'uncertain'
        operation.error = 'Нет достоверного подтверждения принятия сервером; повтор заблокирован'
    await db.commit()
    return result
