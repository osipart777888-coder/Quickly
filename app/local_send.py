"""Fail-closed send controls, atomic daily budgets and durable replay prevention.

SMTP/API transport cannot promise exactly-once delivery across a network crash.
A committed reservation with an unknown result is deliberately never retried.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import os
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from app.local_models import MailOperation, SenderDayQuota, SendControl, Suppression
from app.models import EmailLog, Lead, Campaign, AppSetting
from app.queue_logic import compute_effective_daily_limit
from app.sender import SendFailure, SendResult
from app.time import utcnow


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
    contacts = (await db.execute(select(Lead).where(func.lower(Lead.email) == email))).scalars().all()
    if any(item.status in {'unsubscribed', 'invalid', 'bounced', 'blacklisted'} for item in contacts):
        return 'Получатель отписался, заблокирован или адрес недействителен'
    if lead is not None:
        data = lead.custom_data or {}
        if not str(data.get('consent_basis', '')).strip() or not str(data.get('consent_date', '')).strip():
            return 'Для контакта не указаны дата и основание разрешённой отправки'
        try:
            permission_date = datetime.fromisoformat(str(data['consent_date'])[:10]).date()
            if permission_date.isoformat() > day_bounds()[0]:
                return 'Дата разрешения на отправку ещё не наступила'
        except ValueError:
            return 'Нужна корректная дата разрешения в формате ГГГГ-ММ-ДД'
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


async def reserve_operation(db, inbox, *, operation_key, recipient, kind, lead=None, campaign_id=None):
    """Reserve quota and persist the intent before any network send.

    The conditional UPDATE serializes competing PostgreSQL transactions; the
    unique operation key protects the same campaign step across restarts.
    """
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
    await db.refresh(inbox, attribute_names=['paused', 'max_emails_per_day'])
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
    await db.execute(insert(SenderDayQuota).values(inbox_id=inbox.id, day=day, used=existing).on_conflict_do_nothing(index_elements=['inbox_id', 'day']))
    operation = MailOperation(operation_key=operation_key, inbox_id=inbox.id, recipient=recipient,
                              kind=kind, campaign_id=campaign_id, status='reserved')
    try:
        async with db.begin_nested():
            db.add(operation)
            await db.flush()
    except IntegrityError:
        return None, 'Эта операция уже зарезервирована другим обработчиком'
    used = await db.scalar(update(SenderDayQuota).where(
        SenderDayQuota.inbox_id == inbox.id, SenderDayQuota.day == day, SenderDayQuota.used < limit,
    ).values(used=SenderDayQuota.used + 1).returning(SenderDayQuota.used))
    if used is None:
        await db.delete(operation)
        await db.flush()
        return None, f'Дневной лимит ящика исчерпан ({limit})'
    await db.commit()
    return operation, ''


async def guarded_send(db, inbox, *, operation_key=None, kind='manual', lead=None, campaign_id=None, send, **kwargs):
    if not local_install():
        return await asyncio.to_thread(send, **kwargs)
    key = operation_key or f'{kind}:{uuid.uuid4().hex}'
    operation, error = await reserve_operation(db, inbox, operation_key=key, recipient=kwargs.get('to_email', ''),
                                             kind=kind, lead=lead, campaign_id=campaign_id)
    if error:
        return SendFailure('local_block', error)
    # A stop pressed after reservation still prevents a send that has not begun.
    control = await db.get(SendControl, 1, populate_existing=True)
    if control and (control.emergency_stopped or (kind == 'campaign' and not control.campaigns_enabled)):
        operation.status = 'cancelled'
        operation.error = 'Отправка остановлена до обращения к почтовому серверу'
        operation.finished_at = utcnow()
        await db.commit()
        return SendFailure('local_block', operation.error)
    try:
        result = await asyncio.to_thread(send, **kwargs)
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
    elif isinstance(result, SendFailure):
        operation.status = 'rejected'
        operation.error = result.message[:2000]
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
