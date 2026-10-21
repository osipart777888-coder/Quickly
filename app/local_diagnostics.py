"""Explicitly labelled diagnostics between configured accounts owned by the user."""
from __future__ import annotations
from app.maintenance import background_work
from app.mail_io import bounded_io, await_thread
from app.diagnostic_observation import policy as observation_policy, deadline_for, retry_delay, folder_kind

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
from app.local_models import DiagnosticDayQuota, DiagnosticSenderDayQuota, DiagnosticPlan, DiagnosticRun, MailOperation, SendControl
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


async def plan_budget(db, plan, sender_id=None, *, commit=True):
    day, _, _ = day_bounds()
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    # Both counters commit together, before transport. A failed account budget
    # does not consume the group counter, and concurrent workers serialize in SQL.
    async with db.begin_nested():
        await db.execute(insert(DiagnosticDayQuota).values(plan_id=plan.id, day=day, used=0)
                         .on_conflict_do_nothing(index_elements=['plan_id', 'day']))
        if plan.mode == 'group' and sender_id:
            await db.execute(insert(DiagnosticSenderDayQuota).values(plan_id=plan.id,inbox_id=sender_id,day=day,used=0)
                .on_conflict_do_nothing(index_elements=['plan_id','inbox_id','day']))
            used = await db.scalar(update(DiagnosticSenderDayQuota).where(
                DiagnosticSenderDayQuota.plan_id == plan.id,DiagnosticSenderDayQuota.inbox_id == sender_id,
                DiagnosticSenderDayQuota.day == day,DiagnosticSenderDayQuota.used < plan.per_account_daily_limit)
                .values(used=DiagnosticSenderDayQuota.used+1).returning(DiagnosticSenderDayQuota.used))
            if used is None:
                raise ValueError('Дневной предел исходных проверок этого ящика в группе исчерпан')
        used = await db.scalar(update(DiagnosticDayQuota).where(
            DiagnosticDayQuota.plan_id == plan.id, DiagnosticDayQuota.day == day,
            DiagnosticDayQuota.used < plan.max_messages_per_day,
        ).values(used=DiagnosticDayQuota.used + 1).returning(DiagnosticDayQuota.used))
        if used is None:
            raise ValueError('Дневной предел диагностических писем группы исчерпан')
    if commit:
        await db.commit()


async def start_diagnostic(db, sender_id, recipient_id, plan=None, *, observation_hours=48):
    if sender_id == recipient_id:
        raise ValueError('Выберите два разных собственных ящика')
    if not await db.get(Inbox,sender_id) or not await db.get(Inbox,recipient_id):
        raise ValueError('Оба ящика должны быть подключены в Quickly')
    run = DiagnosticRun(id=uuid.uuid4().hex,sender_id=sender_id,recipient_id=recipient_id,
        plan_id=plan.id if plan else None,status='created',report={'observation_hours':observation_hours})
    db.add(run)
    await db.commit()
    return await execute_diagnostic(db,run,plan)


async def execute_diagnostic(db, run, plan=None):
    run.started_at = utcnow()
    await db.commit()
    try:
        sender = await db.get(Inbox,run.sender_id,populate_existing=True)
        recipient = await db.get(Inbox,run.recipient_id,populate_existing=True)
        if not sender or not recipient or sender.paused or recipient.paused:
            raise ValueError('Участник проверки удалён или приостановлен')
        if plan is not None:
            await db.refresh(plan)
            if not plan.enabled:
                raise ValueError('Расписание диагностики остановлено')
        run.sender_email=sender.email
        run.recipient_email=recipient.email
        args = await send_arguments(db,sender)
        result = await guarded_send(db,sender,operation_key=f'diagnostic:{run.id}',kind='diagnostic',
            diagnostic_plan_id=plan.id if plan else None,diagnostic_initial=True,send=send_email,
            to_email=recipient.email,from_email=sender.email,from_name=sender.display_name,
            subject=f'[Quickly DIAGNOSTIC {run.id}] Проверка собственного ящика',
            body=('Автоматическое диагностическое сообщение Quickly.\n'
                  'Отправлено между собственными ящиками владельца установки.\n'
                  'Это проверка транспорта, а не переписка с заинтересованным клиентом.\n'
                  f'Идентификатор проверки: {run.id}\n'),provider=sender.provider,**args)
        operation = await db.scalar(select(MailOperation).where(MailOperation.operation_key == f'diagnostic:{run.id}'))
        run.operation_id = operation.id if operation else None
        run.report={**(run.report or {}),'send_status':operation.status if operation else ('accepted' if isinstance(result,SendResult) else 'unknown')}
        if isinstance(result,SendResult):
            run.status = 'accepted'
            run.accepted_at = operation.finished_at if operation else utcnow()
            run.observation_deadline = run.accepted_at+timedelta(hours=observation_policy(plan)[0] if plan else (run.report or {}).get('observation_hours',48))
            run.message_id = result.message_id or ''
            run.report = {'send_status':'accepted','smtp_accepted':True,'received_confirmed':False,
                'note':'Принято сервером отправителя. Получение пока не подтверждено.'}
        elif getattr(result,'error_type','') == 'local_deferred' and plan and plan.mode == 'group':
            run.status = 'queued'
            run.started_at = None
            run.error = result.message
        else:
            run.status = 'uncertain' if operation and operation.status == 'uncertain' else 'skipped' if getattr(result,'error_type','') == 'local_block' else 'failed'
            run.error = getattr(result,'message','') or 'Результат отправки неизвестен; автоматического повтора не будет'
    except ValueError as exc:
        run.status = 'skipped'
        run.error = str(exc)[:2000]
    except Exception as exc:
        operation = await db.scalar(select(MailOperation).where(MailOperation.operation_key == f'diagnostic:{run.id}'))
        run.operation_id = operation.id if operation else None
        run.status = 'uncertain' if operation else 'failed'
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


@bounded_io(reading=True)
async def find_received(db, inbox, run, *, reply=False):
    if inbox.provider == 'smtp':
        account = await db.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id == inbox.id))
        if not account or not account.imap_host:
            return {'found': False, 'unavailable': 'IMAP не настроен; получение подтвердить нельзя'}
        return await await_thread(_imap_find, account, run.id, reply_to=run.message_id if reply else '')
    # Reuse Quickly's official OAuth API clients; no mailbox passwords for Google/Microsoft.
    from app import unibox
    if inbox.provider == 'gmail':
        account = await db.scalar(select(GmailAccount).where(GmailAccount.inbox_id == inbox.id))
        if not account:
            return {'found': False, 'unavailable': 'Google OAuth не подключён'}
        cid, secret = await get_google_oauth_credentials(db)
        if account.token_expiry is None or account.token_expiry <= utcnow() + timedelta(minutes=2):
            if not await await_thread(unibox.refresh_access_token, account, cid, secret):
                return {'found': False, 'unavailable': 'Google OAuth требует повторного подключения'}
            await db.flush()
        query = f'in:anywhere subject:{run.id}'
        result = await await_thread(unibox._gmail_request_json, 'GET', f'{unibox.GMAIL_API_ROOT}/messages',
                                        account.access_token, params={'q': query, 'maxResults': 10})
        for item in result.get('messages', []):
            data = await await_thread(unibox._gmail_get_message, account.access_token, item['id'])
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
        if not await await_thread(unibox.refresh_office365_token, account, cid, secret, tenant):
            return {'found': False, 'unavailable': 'Microsoft OAuth требует повторного подключения'}
        await db.flush()
    result = await await_thread(unibox._graph_request_json, 'GET', 'https://graph.microsoft.com/v1.0/me/messages',
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


async def check_receipt(db, run, *, reply=False, manual=True):
    from app.local_models import MailboxReadState
    now=utcnow()
    plan=await db.get(DiagnosticPlan,run.plan_id) if run.plan_id else None
    deadline=deadline_for(run,plan,reply=reply)
    if not manual and deadline<=now:
        run.observation_status='expired'
        run.status='reply_expired' if reply else 'expired'
        run.next_check_at=None
        await db.commit()
        return run
    if run.status in {'queued','created','dispatching','failed','skipped','cancelled'}:
        raise ValueError('Письмо ещё не отправлено; проверять получение нельзя')
    inbox=await db.get(Inbox,run.sender_id if reply else run.recipient_id)
    if not inbox:
        raise ValueError('Получающий ящик удалён')
    if not manual and inbox.paused:
        run.observation_status='temporarily_unavailable'
        run.check_error='Получающий ящик приостановлен; автоматическое чтение остановлено'
        run.next_check_at=now+timedelta(seconds=observation_policy(plan)[1])
        await db.commit()
        return run
    state=await db.get(MailboxReadState,inbox.id)
    if state and state.retry_after and state.retry_after>now:
        run.observation_status='temporarily_unavailable'
        run.check_error='Провайдер ограничил чтение. Следующая попытка после '+state.retry_after.isoformat()+' UTC'
        run.next_check_at=state.retry_after
        await db.commit()
        return run
    run.last_checked_at=now
    run.check_attempts=(run.check_attempts or 0)+1
    # Persist a lease before network access; a crash does not trigger a tight retry loop.
    base=observation_policy(plan)[1]
    run.next_check_at=now+timedelta(seconds=base)
    await db.commit()
    delay=None
    try:
        result=await find_received(db,inbox,run,reply=reply)
    except Exception as exc:
        code=getattr(exc,'status_code',0)
        delay=retry_delay(getattr(exc,'retry_after',None),now)
        import imaplib
        quota=code==403 and any(word in str(getattr(exc,'body','')).lower() for word in ['ratelimitexceeded','quotaexceeded','dailylimitexceeded'])
        auth_error=code in {401,403} and not quota or isinstance(exc,imaplib.IMAP4.error) and any(word in str(exc).lower() for word in ['auth','login','credentials'])
        if auth_error:
            inbox.paused=True
            result={'found':False,'unavailable':'Ошибка авторизации чтения. Исправьте подключение и вручную снимите паузу.'}
        elif code in {429,503} or quota or delay:
            result={'found':False,'unavailable':'Провайдер временно ограничил чтение почты'}
            delay=delay or timedelta(seconds=min(3600,base*2**min(run.check_attempts,5)))
        else:
            result={'found':False,'error':sanitize_connection_error(str(exc)) or 'Проверка временно недоступна'}
    report=dict(run.report or {})
    report['reply_check' if reply else 'receipt_check']=result
    if result.get('found'):
        run.observation_status='received'
        run.check_error=''
        run.next_check_at=None
        if reply:
            run.status='reply_received'
            report['reply_received_at']=utcnow().isoformat()
        elif run.status not in {'reply_sent','reply_received'}:
            run.status='received'
        if not reply:
            run.received_at=run.received_at or utcnow()
            run.duration_ms=max(0,int((run.received_at-(run.accepted_at or run.created_at)).total_seconds()*1000))
            run.folder=result.get('folder','')
            run.folder_kind=result.get('folder_kind') or folder_kind(run.folder,provider=inbox.provider)
            run.authentication_headers=result.get('headers',{})
            report['received_confirmed']=True
            report['timing_note']='Время до обнаружения; включает интервал опроса'
    else:
        unavailable=result.get('unavailable') or result.get('error')
        if result.get('unavailable_folders'):
            unavailable=unavailable or 'Часть папок недоступна; отсутствие письма не подтверждено'
        run.observation_status='temporarily_unavailable' if unavailable else 'not_found'
        run.check_error=unavailable or ''
        delay=delay or timedelta(seconds=min(3600,base*2**min(max(0,run.check_attempts-1),5)))
        run.next_check_at=now+delay
        if result.get('unavailable')=='Провайдер временно ограничил чтение почты':
            if db.bind.dialect.name=='postgresql':
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            from sqlalchemy import case
            stmt=insert(MailboxReadState).values(inbox_id=inbox.id,retry_after=run.next_check_at)
            await db.execute(stmt.on_conflict_do_update(index_elements=['inbox_id'],set_={'retry_after':case((MailboxReadState.retry_after>run.next_check_at,MailboxReadState.retry_after),else_=run.next_check_at)}))
        if deadline<=utcnow():
            run.observation_status='expired'
            run.status='reply_expired' if reply else 'expired'
            run.next_check_at=None
    run.report=report
    await db.commit()
    return run


async def send_diagnostic_reply(db, run, *, allow_claimed=False):
    plan = None
    if run.status not in ({'received','reply_received','reply_pending'} if allow_claimed else {'received','reply_received'}):
        raise ValueError('Сначала подтвердите получение исходного диагностического письма')
    if run.reply_message_id:
        raise ValueError('Диагностический ответ уже отправлен')
    if await db.scalar(select(MailOperation.id).where(MailOperation.operation_key == f'diagnostic-reply:{run.id}')):
        raise ValueError('Ответ уже выполнялся; автоматический повтор запрещён')
    if run.plan_id:
        plan = await db.get(DiagnosticPlan, run.plan_id)
        if not plan or not plan.enabled:
            raise ValueError('Расписание группы остановлено')
    sender = await db.get(Inbox, run.recipient_id)
    recipient = await db.get(Inbox, run.sender_id)
    if not sender or not recipient or sender.paused or recipient.paused:
        raise ValueError('Участник проверки удалён или приостановлен')
    args = await send_arguments(db, sender)
    result = await guarded_send(db, sender, operation_key=f'diagnostic-reply:{run.id}', kind='diagnostic', diagnostic_plan_id=plan.id if plan else None, send=send_email,
        to_email=recipient.email, from_email=sender.email, from_name=sender.display_name,
        subject=f'[Quickly DIAGNOSTIC REPLY {run.id}] Подтверждение проверки',
        body=f'Автоматическое диагностическое подтверждение получения.\nНе является ответом клиента или заинтересованностью.\nПроверка: {run.id}',
        provider=sender.provider, reply_to_msg_id=run.message_id, references=run.message_id, **args)
    if not isinstance(result, SendResult):
        if getattr(result,'error_type','') == 'local_deferred':
            raise DiagnosticDeferred(result.message)
        raise ValueError(getattr(result, 'message', '') or 'Результат ответа неизвестен; повтор заблокирован')
    run.reply_message_id = result.message_id
    run.reply_accepted_at = utcnow()
    run.reply_observation_deadline = run.reply_accepted_at+timedelta(hours=observation_policy(plan)[0])
    run.next_check_at = None
    run.observation_status = 'pending'
    run.status = 'reply_sent'
    run.report = {**(run.report or {}), 'diagnostic_reply_accepted': True}
    await db.commit()
    return run


@background_work
async def run_diagnostic_job():
    from app.local_group_diagnostics import run_group_jobs
    await run_group_jobs()


class DiagnosticDeferred(ValueError):
    """An automatic reply remains pending until its mailbox pacing allows it."""
