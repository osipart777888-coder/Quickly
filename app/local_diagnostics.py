"""Explicitly labelled diagnostics between configured accounts owned by the user."""
from __future__ import annotations
import asyncio
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import re
import uuid
from zoneinfo import ZoneInfo
from datetime import timezone
from sqlalchemy import func, select, update
from app.app_settings import get_google_oauth_credentials, get_office365_oauth_credentials
from app.database import AsyncSessionLocal
from app.local_models import DiagnosticDayQuota, DiagnosticPlan, DiagnosticRun, MailOperation, SendControl
from app.local_send import day_bounds, guarded_send
from app.models import GmailAccount, Inbox, Office365Account, SmtpAccount
from app.sender import send_email, SendResult
from app.smtp_utils import _imap_connect, sanitize_connection_error
from app.time import utcnow


async def send_arguments(db, inbox):
    if inbox.provider == 'smtp':
        account = await db.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id == inbox.id))
        if not account:
            raise ValueError('SMTP-аккаунт не настроен')
        return {'smtp_account': account}
    if inbox.provider == 'office365':
        account = await db.scalar(select(Office365Account).where(Office365Account.inbox_id == inbox.id))
        if not account:
            raise ValueError('Microsoft OAuth не подключён')
        cid, secret, tenant = await get_office365_oauth_credentials(db)
        return {'office365_account': account, 'office365_client_id': cid,
                'office365_client_secret': secret, 'office365_tenant_id': tenant}
    account = await db.scalar(select(GmailAccount).where(GmailAccount.inbox_id == inbox.id))
    if not account:
        raise ValueError('Google OAuth не подключён')
    cid, secret = await get_google_oauth_credentials(db)
    return {'gmail_account': account, 'google_client_id': cid, 'google_client_secret': secret}


async def plan_budget(db, plan):
    day, _, _ = day_bounds()
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    await db.execute(insert(DiagnosticDayQuota).values(plan_id=plan.id, day=day, used=0).on_conflict_do_nothing(index_elements=['plan_id', 'day']))
    used = await db.scalar(update(DiagnosticDayQuota).where(
        DiagnosticDayQuota.plan_id == plan.id, DiagnosticDayQuota.day == day,
        DiagnosticDayQuota.used < plan.max_messages_per_day,
    ).values(used=DiagnosticDayQuota.used + 1).returning(DiagnosticDayQuota.used))
    await db.commit()
    if used is None:
        raise ValueError('Дневной предел диагностических писем группы исчерпан')


async def start_diagnostic(db, sender_id, recipient_id, plan=None):
    if sender_id == recipient_id:
        raise ValueError('Выберите два разных собственных ящика')
    sender = await db.get(Inbox, sender_id)
    recipient = await db.get(Inbox, recipient_id)
    if not sender or not recipient:
        raise ValueError('Оба ящика должны быть подключены в Quickly')
    if plan is not None:
        await plan_budget(db, plan)
    run = DiagnosticRun(id=uuid.uuid4().hex, sender_id=sender_id, recipient_id=recipient_id,
                        plan_id=plan.id if plan else None, status='created')
    db.add(run)
    await db.commit()
    try:
        args = await send_arguments(db, sender)
        result = await guarded_send(db, sender, operation_key=f'diagnostic:{run.id}', kind='diagnostic', send=send_email,
                                    to_email=recipient.email, from_email=sender.email, from_name=sender.display_name,
                                    subject=f'[Quickly DIAGNOSTIC {run.id}] Проверка собственного ящика',
                                    body=('Автоматическое диагностическое сообщение Quickly.\n'
                                          'Отправлено между собственными ящиками владельца установки.\n'
                                          'Это проверка транспорта, а не переписка с заинтересованным клиентом.\n'
                                          f'Идентификатор проверки: {run.id}\n'),
                                    provider=sender.provider, **args)
        operation = await db.scalar(select(MailOperation).where(MailOperation.operation_key == f'diagnostic:{run.id}'))
        run.operation_id = operation.id if operation else None
        if isinstance(result, SendResult):
            run.status = 'accepted'
            run.message_id = result.message_id or ''
            run.report = {'smtp_accepted': True, 'received_confirmed': False,
                          'note': 'Принято сервером отправителя. Получение пока не подтверждено.'}
        else:
            run.status = 'failed'
            run.error = getattr(result, 'message', '') or 'Результат отправки неизвестен; автоматического повтора не будет'
    except Exception as exc:
        run.status = 'failed'
        run.error = sanitize_connection_error(str(exc)) or 'Не удалось выполнить диагностику'
    await db.commit()
    return run


def _header_report(message):
    return {name: '\n'.join(str(v) for v in message.get_all(name, []))[:10000]
            for name in ['Authentication-Results', 'ARC-Authentication-Results', 'DKIM-Signature', 'Received-SPF']
            if message.get_all(name)}


def _imap_find(account, marker, *, reply_to=''):
    import time
    deadline = time.monotonic() + 20
    client = _imap_connect(account, timeout=8)
    folders = ['INBOX']
    warnings = []
    try:
        typ, items = client.list()
        if typ == 'OK':
            for item in items or []:
                if not isinstance(item, bytes) or b'\\Noselect' in item:
                    continue
                match = re.search(rb'\)\s+(?:NIL|"[^"]*")\s+(.+)$', item)
                if match:
                    folder = match.group(1).decode('ascii', errors='replace')
                    if folder.strip('"').upper() != 'INBOX':
                        folders.append(folder)
        for folder in folders[:50]:
            if time.monotonic() > deadline:
                warnings.append('Достигнуто ограничение времени проверки папок')
                break
            try:
                if client.select(folder, readonly=True)[0] != 'OK':
                    warnings.append(folder)
                    continue
                if reply_to:
                    typ, matches = client.uid('SEARCH', None, 'HEADER', 'In-Reply-To', '"' + reply_to.replace('"', '') + '"')
                else:
                    typ, matches = client.uid('SEARCH', None, 'HEADER', 'Subject', '"' + marker + '"')
                if typ != 'OK' or not matches or not matches[0]:
                    continue
                for uid in matches[0].split()[-10:]:
                    typ, entries = client.uid('FETCH', uid, '(BODY.PEEK[])')
                    for entry in entries or []:
                        if not isinstance(entry, tuple) or not isinstance(entry[1], bytes):
                            continue
                        message = BytesParser(policy=policy.default).parsebytes(entry[1])
                        subject = str(message.get('Subject', ''))
                        if marker not in subject:
                            continue
                        if not reply_to and 'DIAGNOSTIC REPLY' in subject:
                            continue
                        return {'found': True, 'folder': folder.strip('"'),
                                'headers': _header_report(message), 'message_id': str(message.get('Message-ID', '')),
                                'unavailable_folders': warnings}
            except Exception:
                warnings.append(folder)
        return {'found': False, 'checked_folders': folders[:50], 'unavailable_folders': warnings}
    finally:
        try:
            client.logout()
        except Exception:
            pass


async def find_received(db, inbox, run, *, reply=False):
    if inbox.provider == 'smtp':
        account = await db.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id == inbox.id))
        if not account or not account.imap_host:
            return {'found': False, 'unavailable': 'IMAP не настроен; получение подтвердить нельзя'}
        return await asyncio.to_thread(_imap_find, account, run.id, reply_to=run.message_id if reply else '')
    # Reuse Quickly's official OAuth API clients; no mailbox passwords for Google/Microsoft.
    from app import unibox
    if inbox.provider == 'gmail':
        account = await db.scalar(select(GmailAccount).where(GmailAccount.inbox_id == inbox.id))
        if not account:
            return {'found': False, 'unavailable': 'Google OAuth не подключён'}
        cid, secret = await get_google_oauth_credentials(db)
        if account.token_expiry is None or account.token_expiry <= utcnow() + timedelta(minutes=2):
            if not await asyncio.to_thread(unibox.refresh_access_token, account, cid, secret):
                return {'found': False, 'unavailable': 'Google OAuth требует повторного подключения'}
            await db.flush()
        query = f'in:anywhere subject:{run.id}'
        result = await asyncio.to_thread(unibox._gmail_request_json, 'GET', f'{unibox.GMAIL_API_ROOT}/messages',
                                        account.access_token, params={'q': query, 'maxResults': 10})
        for item in result.get('messages', []):
            data = await asyncio.to_thread(unibox._gmail_get_message, account.access_token, item['id'])
            headers = {h['name'].lower(): h['value'] for h in data.get('payload', {}).get('headers', [])}
            if reply and run.message_id not in headers.get('in-reply-to', ''):
                continue
            if not reply and 'DIAGNOSTIC REPLY' in headers.get('subject', ''):
                continue
            return {'found': True, 'folder': ', '.join(data.get('labelIds', [])),
                    'headers': {k: v for k, v in headers.items() if k in {'authentication-results', 'dkim-signature', 'received-spf'}},
                    'message_id': headers.get('message-id', '')}
        return {'found': False, 'checked_folders': ['Gmail: in:anywhere']}
    account = await db.scalar(select(Office365Account).where(Office365Account.inbox_id == inbox.id))
    if not account:
        return {'found': False, 'unavailable': 'Microsoft OAuth не подключён'}
    cid, secret, tenant = await get_office365_oauth_credentials(db)
    if account.token_expiry is None or account.token_expiry <= utcnow() + timedelta(minutes=2):
        if not await asyncio.to_thread(unibox.refresh_office365_token, account, cid, secret, tenant):
            return {'found': False, 'unavailable': 'Microsoft OAuth требует повторного подключения'}
        await db.flush()
    result = await asyncio.to_thread(unibox._graph_request_json, 'GET', 'https://graph.microsoft.com/v1.0/me/messages',
                                    account.access_token, params={'$filter': f"contains(subject,'{run.id}')", '$top': 20,
                                        '$select': 'subject,internetMessageId,internetMessageHeaders,parentFolderId'})
    for data in result.get('value', []):
        headers = {h['name'].lower(): h['value'] for h in data.get('internetMessageHeaders', [])}
        if reply and run.message_id not in headers.get('in-reply-to', ''):
            continue
        if not reply and 'DIAGNOSTIC REPLY' in data.get('subject', ''):
            continue
        return {'found': True, 'folder': 'Microsoft folder ID: ' + data.get('parentFolderId', ''),
                'headers': {k: v for k, v in headers.items() if k in {'authentication-results', 'dkim-signature', 'received-spf'}},
                'message_id': data.get('internetMessageId', '')}
    return {'found': False, 'checked_folders': ['Microsoft /me/messages: доступные сообщения']}


async def check_receipt(db, run, *, reply=False):
    inbox = await db.get(Inbox, run.sender_id if reply else run.recipient_id)
    try:
        result = await find_received(db, inbox, run, reply=reply)
    except Exception as exc:
        result = {'found': False, 'error': sanitize_connection_error(str(exc))}
    report = dict(run.report or {})
    report['reply_check' if reply else 'receipt_check'] = result
    if result.get('found'):
        run.status = 'reply_received' if reply else 'received'
        if not reply:
            run.received_at = utcnow()
            run.duration_ms = max(0, int((run.received_at - run.created_at).total_seconds() * 1000))
            run.folder = result.get('folder', '')
            run.authentication_headers = result.get('headers', {})
            report['received_confirmed'] = True
            report['timing_note'] = 'Время до обнаружения при проверке; включает интервал опроса'
    run.report = report
    await db.commit()
    return run


async def send_diagnostic_reply(db, run):
    if run.status not in {'received', 'reply_received'}:
        raise ValueError('Сначала подтвердите получение исходного диагностического письма')
    if run.reply_message_id:
        raise ValueError('Диагностический ответ уже отправлен')
    if run.plan_id:
        plan = await db.get(DiagnosticPlan, run.plan_id)
        if not plan or not plan.enabled:
            raise ValueError('Расписание группы остановлено')
        await plan_budget(db, plan)
    sender = await db.get(Inbox, run.recipient_id)
    recipient = await db.get(Inbox, run.sender_id)
    args = await send_arguments(db, sender)
    result = await guarded_send(db, sender, operation_key=f'diagnostic-reply:{run.id}', kind='diagnostic', send=send_email,
        to_email=recipient.email, from_email=sender.email, from_name=sender.display_name,
        subject=f'[Quickly DIAGNOSTIC REPLY {run.id}] Подтверждение проверки',
        body=f'Автоматическое диагностическое подтверждение получения.\nНе является ответом клиента или заинтересованностью.\nПроверка: {run.id}',
        provider=sender.provider, reply_to_msg_id=run.message_id, references=run.message_id, **args)
    if not isinstance(result, SendResult):
        raise ValueError(getattr(result, 'message', '') or 'Результат ответа неизвестен; повтор заблокирован')
    run.reply_message_id = result.message_id
    run.report = {**(run.report or {}), 'diagnostic_reply_accepted': True}
    await db.commit()
    return run


async def run_diagnostic_job():
    """DB-backed schedules; advance the due time atomically before doing any work."""
    async with AsyncSessionLocal() as db:
        control = await db.get(SendControl, 1)
        if control and control.emergency_stopped:
            return
        plans = (await db.execute(select(DiagnosticPlan).where(DiagnosticPlan.enabled == True,
                    DiagnosticPlan.next_run_at <= utcnow()).limit(20))).scalars().all()
        for plan in plans:
            local_now = utcnow().replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Moscow'))
            if local_now.weekday() not in plan.sending_days or not (plan.hours_start <= local_now.strftime('%H:%M') < plan.hours_end):
                continue
            now = utcnow()
            claimed = await db.scalar(update(DiagnosticPlan).where(DiagnosticPlan.id == plan.id,
                DiagnosticPlan.enabled == True, DiagnosticPlan.next_run_at <= now).values(
                next_run_at=now + timedelta(minutes=plan.interval_minutes)).returning(DiagnosticPlan.id))
            await db.commit()
            if claimed:
                try:
                    await start_diagnostic(db, plan.sender_id, plan.recipient_id, plan)
                except ValueError:
                    pass  # A cap/stop is a normal outcome, never move it to another sender.
        pending = (await db.execute(select(DiagnosticRun).where(DiagnosticRun.status == 'accepted',
                    DiagnosticRun.created_at >= utcnow() - timedelta(hours=24)).limit(20))).scalars().all()
        for run in pending:
            await check_receipt(db, run)
