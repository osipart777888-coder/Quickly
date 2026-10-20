"""Local installation control, account overview and delivery ledger."""
from datetime import timedelta
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from app.auth import require_admin
from app.database import get_db
from app.local_models import DiagnosticRun, MailOperation, SendControl, SenderDayQuota, Suppression
from app.local_send import day_bounds
from app.models import EmailLog, Inbox, LeadReply, SmtpAccount, AppSetting, Lead, GmailAccount, Office365Account
from app.time import utcnow

router = APIRouter(prefix='/api/local', tags=['local'])


class ControlUpdate(BaseModel):
    emergency_stopped: bool | None = None
    campaigns_enabled: bool | None = None
    confirmation: str = ''


async def get_control(db):
    control = await db.get(SendControl, 1)
    if not control:
        if db.bind.dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        await db.execute(insert(SendControl).values(id=1, emergency_stopped=False, campaigns_enabled=False,
                         reason='Кампании ожидают разрешения пользователя', updated_at=utcnow()).on_conflict_do_nothing(index_elements=['id']))
        await db.flush()
        control = await db.get(SendControl, 1, populate_existing=True)
    return control


def control_json(control):
    return {'emergency_stopped': control.emergency_stopped, 'campaigns_enabled': control.campaigns_enabled,
            'reason': control.reason, 'updated_at': control.updated_at}


@router.get('/control')
async def control_status(db=Depends(get_db)):
    return control_json(await get_control(db))


@router.put('/control')
async def update_control(data: ControlUpdate, db=Depends(get_db), _admin=Depends(require_admin)):
    control = await get_control(db)
    if data.campaigns_enabled is True and data.confirmation != 'РАЗРЕШАЮ ОТПРАВКУ':
        raise HTTPException(400, 'Введите РАЗРЕШАЮ ОТПРАВКУ для явного разрешения реальных кампаний')
    if data.emergency_stopped is not None:
        control.emergency_stopped = data.emergency_stopped
    if data.campaigns_enabled is not None:
        control.campaigns_enabled = data.campaigns_enabled
    if control.emergency_stopped:
        control.campaigns_enabled = False
    control.reason = ('Аварийная остановка: новые отправки запрещены' if control.emergency_stopped else
                      'Кампании разрешены пользователем' if control.campaigns_enabled else 'Кампании заблокированы; ручная диагностика доступна')
    control.updated_at = utcnow()
    await db.flush()
    return control_json(control)


@router.get('/operations')
async def operations(inbox_id: int | None = None, limit: int = 100, db=Depends(get_db)):
    query = select(MailOperation).order_by(MailOperation.created_at.desc()).limit(min(max(limit, 1), 1000))
    if inbox_id:
        query = query.where(MailOperation.inbox_id == inbox_id)
    rows = (await db.execute(query)).scalars().all()
    return [{key: getattr(row, key) for key in ['id', 'inbox_id', 'campaign_id', 'recipient', 'kind', 'status',
                                              'message_id', 'error', 'created_at', 'finished_at']} for row in rows]


@router.get('/overview')
async def overview(db=Depends(get_db)):
    day, start, end = day_bounds()
    inboxes = (await db.execute(select(Inbox).order_by(Inbox.id))).scalars().all()
    import json
    health = {}
    safety_reasons = {x.key: x.value for x in (await db.execute(select(AppSetting).where(AppSetting.key.like('local_account_safety_%')))).scalars().all()}
    for setting in (await db.execute(select(AppSetting).where(AppSetting.key.like('local_connection_health_%')))).scalars().all():
        try:
            health[int(setting.key.rsplit('_', 1)[1])] = json.loads(setting.value)
        except (ValueError, TypeError):
            pass
    oauth_ids = set((await db.execute(select(GmailAccount.inbox_id))).scalars().all()) | set((await db.execute(select(Office365Account.inbox_id))).scalars().all())
    reply_counts = dict((await db.execute(select(EmailLog.inbox_id, func.count(func.distinct(LeadReply.id))).join(LeadReply,
        (LeadReply.lead_id == EmailLog.lead_id) & (LeadReply.campaign_id == EmailLog.campaign_id)).group_by(EmailLog.inbox_id))).all())
    accounts = {a.inbox_id: a for a in (await db.execute(select(SmtpAccount))).scalars().all()}
    quota = {q.inbox_id: q.used for q in (await db.execute(select(SenderDayQuota).where(SenderDayQuota.day == day))).scalars().all()}
    operations_today = (await db.execute(select(MailOperation).where(MailOperation.created_at >= start, MailOperation.created_at < end))).scalars().all()
    accepted = {}; last_success = {}
    for operation in operations_today:
        if operation.status == 'accepted':
            accepted[operation.inbox_id] = accepted.get(operation.inbox_id, 0) + 1
    all_success = (await db.execute(select(MailOperation.inbox_id, func.max(MailOperation.finished_at)).where(MailOperation.status == 'accepted').group_by(MailOperation.inbox_id))).all()
    last_success = dict(all_success)
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    month_start = datetime.fromisoformat(day).replace(day=1, tzinfo=ZoneInfo('Europe/Moscow')).astimezone(timezone.utc).replace(tzinfo=None)
    month_count = await db.scalar(select(func.count(MailOperation.id)).where(MailOperation.status == 'accepted', MailOperation.created_at >= month_start)) or 0
    from app.queue_logic import compute_effective_daily_limit
    rows = []
    for inbox in inboxes:
        account = accounts.get(inbox.id)
        rows.append({'id': inbox.id, 'email': inbox.email, 'provider': inbox.provider, 'paused': inbox.paused,
                     'max_emails_per_day': inbox.max_emails_per_day, 'effective_limit': compute_effective_daily_limit(inbox),
                     'ramp_up_enabled': inbox.ramp_up_enabled, 'sent_today': accepted.get(inbox.id, 0),
                     'attempts_today': quota.get(inbox.id, 0), 'last_success_at': last_success.get(inbox.id),
                     'last_error': safety_reasons.get(f'local_account_safety_{inbox.id}', '') or ((account.last_send_error or account.last_test_error or '') if account else health.get(inbox.id, {}).get('error', '')),
                     'connected': bool(account or inbox.id in oauth_ids),
                     'reply_count': reply_counts.get(inbox.id, 0),
                     'smtp_status': health.get(inbox.id, {}).get('smtp', 'unknown' if account else 'oauth' if inbox.id in oauth_ids else 'not_configured'),
                     'imap_status': health.get(inbox.id, {}).get('imap', 'unknown' if account and account.imap_host else 'oauth' if inbox.id in oauth_ids else 'not_configured'),
                     'smtp_host': account.smtp_host if account else '',
                     'imap_configured': bool(account and account.imap_host),
                     'last_tested_at': account.last_tested_at if account else None})
    timeline_start = start - timedelta(days=29)
    dated = (await db.execute(select(MailOperation.created_at, MailOperation.kind, MailOperation.status).where(MailOperation.created_at >= timeline_start))).all()
    from zoneinfo import ZoneInfo
    from datetime import timezone
    timeline = {}
    for offset in range(30):
        key = (datetime.fromisoformat(day) - timedelta(days=29) + timedelta(days=offset)).date().isoformat()
        timeline[key] = {'date': key, 'accepted': 0, 'errors': 0, 'diagnostic': 0}
    for created, kind, status in dated:
        key = created.replace(tzinfo=timezone.utc).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()
        if key in timeline:
            timeline[key]['accepted'] += status == 'accepted'
            timeline[key]['errors'] += status == 'rejected'
            timeline[key]['diagnostic'] += status == 'accepted' and kind == 'diagnostic'
    diagnostics = (await db.execute(select(DiagnosticRun).order_by(DiagnosticRun.created_at.desc()).limit(10))).scalars().all()
    return {'accounts': rows, 'timeline': list(timeline.values()),
            'replies': await db.scalar(select(func.count(LeadReply.id))) or 0,
            'unsubscribed': await db.scalar(select(func.count(Lead.id)).where(Lead.status == 'unsubscribed')) or 0,
            'diagnostics': [run_json(run) for run in diagnostics], 'today': sum(accepted.values()), 'month': month_count,
            'errors_today': sum(x.status == 'rejected' for x in operations_today),
            'uncertain_today': sum(x.status in {'reserved', 'uncertain'} for x in operations_today),
            'confirmed_delivery': None, 'accepted_by_server': sum(accepted.values()),
            'control': control_json(await get_control(db))}


class DiagnosticRequest(BaseModel):
    sender_id: int
    recipient_id: int
    owned_accounts_confirmed: bool = False


class PlanRequest(DiagnosticRequest):
    name: str = Field(min_length=1, max_length=255)
    inbox_ids: list[int] = Field(default_factory=list)
    enabled: bool = False
    interval_minutes: int = Field(default=1440, ge=15, le=43200)
    max_messages_per_day: int = Field(default=1, ge=1, le=100)
    sending_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    hours_start: str = Field(default='09:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    hours_end: str = Field(default='18:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')


def run_json(run):
    return {key: getattr(run, key) for key in ['id', 'plan_id', 'sender_id', 'recipient_id', 'status',
        'message_id', 'reply_message_id', 'folder', 'authentication_headers', 'duration_ms', 'error',
        'report', 'created_at', 'received_at']}


@router.get('/diagnostics/plans')
async def diagnostic_plans(db=Depends(get_db)):
    from app.local_models import DiagnosticPlan
    rows = (await db.execute(select(DiagnosticPlan).order_by(DiagnosticPlan.id.desc()))).scalars().all()
    return [{key: getattr(row, key) for key in ['id', 'name', 'inbox_ids', 'sender_id', 'recipient_id',
        'enabled', 'interval_minutes', 'max_messages_per_day', 'sending_days', 'hours_start', 'hours_end', 'next_run_at']} for row in rows]


@router.post('/diagnostics/plans')
async def create_plan(data: PlanRequest, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_models import DiagnosticPlan
    if not data.owned_accounts_confirmed:
        raise HTTPException(400, 'Подтвердите, что все выбранные ящики принадлежат вам')
    ids = sorted(set(data.inbox_ids + [data.sender_id, data.recipient_id]))
    connected = set((await db.execute(select(Inbox.id).where(Inbox.id.in_(ids)))).scalars().all())
    if connected != set(ids) or data.sender_id == data.recipient_id:
        raise HTTPException(400, 'Выберите разные подключённые ящики из вашей группы')
    if not data.sending_days or any(d not in range(7) for d in data.sending_days) or data.hours_start >= data.hours_end:
        raise HTTPException(400, 'Укажите дни и корректный интервал часов')
    plan = DiagnosticPlan(**data.model_dump(exclude={'owned_accounts_confirmed', 'inbox_ids'}),
                          inbox_ids=ids, next_run_at=utcnow())
    db.add(plan)
    await db.flush()
    return {'id': plan.id, 'ok': True}


class PlanToggle(BaseModel):
    enabled: bool


@router.put('/diagnostics/plans/{plan_id}')
async def toggle_plan(plan_id: int, data: PlanToggle, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_models import DiagnosticPlan
    plan = await db.get(DiagnosticPlan, plan_id)
    if not plan:
        raise HTTPException(404, 'Расписание не найдено')
    plan.enabled = data.enabled
    if data.enabled:
        plan.next_run_at = utcnow()
    await db.flush()
    return {'ok': True, 'enabled': plan.enabled}


@router.get('/diagnostics/runs')
async def diagnostic_runs(db=Depends(get_db)):
    rows = (await db.execute(select(DiagnosticRun).order_by(DiagnosticRun.created_at.desc()).limit(200))).scalars().all()
    return [run_json(row) for row in rows]


@router.post('/diagnostics/run')
async def run_diagnostic(data: DiagnosticRequest, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_diagnostics import start_diagnostic
    if not data.owned_accounts_confirmed:
        raise HTTPException(400, 'Диагностика разрешена только между вашими собственными ящиками')
    try:
        return run_json(await start_diagnostic(db, data.sender_id, data.recipient_id))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/diagnostics/runs/{run_id}/check')
async def diagnostic_check(run_id: str, reply: bool = False, db=Depends(get_db)):
    from app.local_diagnostics import check_receipt
    run = await db.get(DiagnosticRun, run_id)
    if not run:
        raise HTTPException(404, 'Проверка не найдена')
    return run_json(await check_receipt(db, run, reply=reply))


@router.post('/diagnostics/runs/{run_id}/reply')
async def diagnostic_reply(run_id: str, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_diagnostics import send_diagnostic_reply
    run = await db.get(DiagnosticRun, run_id)
    if not run:
        raise HTTPException(404, 'Проверка не найдена')
    try:
        return run_json(await send_diagnostic_reply(db, run))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get('/diagnostics/dns')
async def dns_diagnostics(domain: str, selector: str = '', db=Depends(get_db)):
    import asyncio
    import re
    import dns.asyncresolver
    domain = domain.strip().lower().rstrip('.')
    selector = selector.strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', domain) or '.' not in domain:
        raise HTTPException(400, 'Укажите домен почты, например example.ru')
    if selector and not re.fullmatch(r'[a-z0-9_-]{1,63}', selector):
        raise HTTPException(400, 'Некорректный DKIM selector')
    async def txt(host, prefix):
        try:
            answer = await dns.asyncresolver.resolve(host, 'TXT', lifetime=5)
            values = [''.join(v.decode('utf-8', errors='replace') for v in record.strings) for record in answer]
            matches = [value for value in values if value.lower().startswith(prefix)]
            return {'status': 'present' if matches else 'not_found', 'records': matches}
        except Exception as exc:
            return {'status': 'unknown', 'records': [], 'error': type(exc).__name__}
    spf, dmarc, dkim = await asyncio.gather(txt(domain, 'v=spf1'), txt('_dmarc.' + domain, 'v=dmarc1'),
                    txt(selector + '._domainkey.' + domain, '') if selector else asyncio.sleep(0, result={'status': 'selector_required', 'records': []}))
    return {'domain': domain, 'spf': spf, 'dmarc': dmarc, 'dkim': dkim,
            'note': 'Проверено наличие DNS-записей. Это не оценка репутации и не гарантия доставки во Входящие.',
            'reputation_api': {'configured': False, 'integration_option': 'Google Postmaster Tools: требуется собственный подтверждённый домен и отдельное OAuth-разрешение'}}


from fastapi import File, UploadFile


async def _import_file(file):
    from app.local_imports import MAX_BYTES
    raw = await file.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, 'Максимальный размер файла — 20 МБ')
    return raw


@router.post('/contacts/import')
async def contacts_import(file: UploadFile = File(...), db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_imports import parse_rows, import_contacts
    try:
        return await import_contacts(db, parse_rows(await _import_file(file), file.filename or 'contacts.csv'))
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


class ContactInput(BaseModel):
    email: str
    name: str = ''
    company: str = ''
    city: str = ''
    okved: str = ''
    title: str = ''
    source: str = ''
    consent_date: str = ''
    consent_basis: str = ''
    segment: str = ''
    custom_fields: dict[str, str] = Field(default_factory=dict)


@router.post('/contacts')
async def contact_add(data: ContactInput, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_imports import import_contacts
    try:
        row = {**data.custom_fields, **data.model_dump(exclude={'custom_fields'})}
        result = await import_contacts(db, [row])
        if result['invalid']:
            raise HTTPException(400, result['errors'][0]['error'])
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/accounts/import')
async def accounts_import(file: UploadFile = File(...), db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_imports import parse_rows, import_smtp_accounts
    if not (file.filename or '').lower().endswith('.csv'):
        raise HTTPException(400, 'Конфигурации отправителей импортируются из CSV')
    try:
        return await import_smtp_accounts(db, parse_rows(await _import_file(file), file.filename, xlsx=False))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class SuppressionInput(BaseModel):
    email: str
    reason: str = 'Просьба не писать'


@router.get('/suppressions')
async def suppressions(db=Depends(get_db)):
    from app.local_models import Suppression
    return [{'email': item.email, 'reason': item.reason, 'created_at': item.created_at}
            for item in (await db.execute(select(Suppression).order_by(Suppression.created_at.desc()))).scalars().all()]


@router.post('/suppressions')
async def suppress(data: SuppressionInput, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_models import Suppression
    from app.local_imports import email_address
    from app.models import Lead, CampaignLead, QueueSlot
    from sqlalchemy import delete, update
    try:
        email = email_address(data.email)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    item = await db.get(Suppression, email)
    if item:
        item.reason = data.reason
    else:
        db.add(Suppression(email=email, reason=data.reason))
    lead_ids = list((await db.execute(select(Lead.id).where(func.lower(Lead.email) == email))).scalars().all())
    if lead_ids:
        await db.execute(update(Lead).where(Lead.id.in_(lead_ids)).values(status='unsubscribed'))
        ids = select(CampaignLead.id).where(CampaignLead.lead_id.in_(lead_ids))
        await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id.in_(ids)))
        await db.execute(update(CampaignLead).where(CampaignLead.lead_id.in_(lead_ids)).values(enrollment_status='unsubscribed', sending_paused=True))
    await db.flush()
    return {'ok': True, 'email': email}


@router.post('/accounts')
async def account_add(data: dict, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_imports import import_smtp_accounts
    result = await import_smtp_accounts(db, [{key: str(value) for key, value in data.items()}])
    if not result['saved']:
        raise HTTPException(400, result['errors'][0]['error'] if result['errors'] else 'Не удалось сохранить ящик')
    return result


class TrustedHostInput(BaseModel):
    hostname: str = Field(max_length=255)


@router.post('/trusted-mail-hosts')
async def trust_mail_host(data: TrustedHostInput, db=Depends(get_db), _admin=Depends(require_admin)):
    import json
    import re
    from app.settings_manager import settings, save_setting_to_db
    host = data.hostname.strip().lower().rstrip('.')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host) or '.' not in host:
        raise HTTPException(400, 'Укажите точное имя собственного SMTP/IMAP-сервера')
    hosts = set(settings.smtp_trusted_proxy_hosts)
    hosts.add(host)
    await save_setting_to_db(db, 'local_smtp_trusted_proxy_hosts', json.dumps(sorted(hosts)))
    settings.smtp_trusted_proxy_hosts = hosts
    return {'ok': True, 'hostname': host}

@router.get('/campaigns/{campaign_id}/preflight')
async def campaign_preflight(campaign_id: int, db=Depends(get_db)):
    from app.models import Campaign, CampaignInbox, CampaignLead, Sequence
    from app.local_send import contact_send_error, public_links_error
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, 'Кампания не найдена')
    errors = []
    if await db.get(AppSetting, f'local_cancelled_campaign_{campaign_id}'):
        errors.append('Кампания отменена. Для нового запуска создайте её копию.')
    inboxes = (await db.execute(select(Inbox).join(CampaignInbox).where(CampaignInbox.campaign_id == campaign_id))).scalars().all()
    if not inboxes:
        errors.append('Не выбраны аккаунты отправителей')
    for inbox in inboxes:
        error = public_links_error(inbox)
        if error:
            errors.append(f'{inbox.email}: {error}')
        if inbox.paused:
            errors.append(f'{inbox.email}: ящик приостановлен')
    if not await db.scalar(select(Sequence.id).where(Sequence.campaign_id == campaign_id).limit(1)):
        errors.append('В последовательности нет писем')
    contacts = (await db.execute(select(Lead).join(CampaignLead).where(CampaignLead.campaign_id == campaign_id))).scalars().all()
    if not contacts:
        errors.append('В кампании нет получателей')
    # Batch policy validation avoids N+1 round-trips for large campaigns.
    suppressed = set((await db.execute(select(Suppression.email))).scalars().all())
    invalid = 0
    for lead in contacts:
        data = lead.custom_data or {}
        try:
            from datetime import date
            valid_date = date.fromisoformat(str(data.get('consent_date', ''))[:10]).isoformat() <= day_bounds()[0]
        except ValueError:
            valid_date = False
        if lead.email.lower() in suppressed or lead.status in {'unsubscribed','invalid','bounced','blacklisted'} or not data.get('consent_basis') or not valid_date:
            invalid += 1
    if invalid:
        errors.append(f'Контактов без разрешения или исключённых из отправки: {invalid}')
    control = await get_control(db)
    return {'ready': not errors, 'errors': errors, 'contacts': len(contacts), 'accounts': len(inboxes),
            'campaign_paused': campaign.paused, 'sending_enabled': control.campaigns_enabled and not control.emergency_stopped}


@router.post('/campaigns/{campaign_id}/cancel')
async def cancel_campaign(campaign_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
    from sqlalchemy import delete, update
    from app.models import Campaign, CampaignLead, QueueSlot
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, 'Кампания не найдена')
    campaign.paused = True
    key = f'local_cancelled_campaign_{campaign_id}'
    if not await db.get(AppSetting, key):
        db.add(AppSetting(key=key, value=utcnow().isoformat()))
    ids = select(CampaignLead.id).where(CampaignLead.campaign_id == campaign_id)
    await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id.in_(ids)))
    await db.execute(update(CampaignLead).where(CampaignLead.campaign_id == campaign_id).values(sending_paused=True))
    await db.flush()
    return {'cancelled': True, 'message': 'Кампания отменена, ожидающая очередь удалена. История сохранена. Уже принятые сервером письма отозвать нельзя.'}


@router.post('/accounts/{inbox_id}/test')
async def test_oauth_account(inbox_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
    import asyncio, json
    from app import unibox
    from app.local_diagnostics import send_arguments
    from app.smtp_utils import sanitize_connection_error
    inbox = await db.get(Inbox, inbox_id)
    if not inbox or inbox.provider == 'smtp':
        raise HTTPException(400, 'Для SMTP используйте штатную проверку SMTP/IMAP')
    try:
        args = await send_arguments(db, inbox)
        if inbox.provider == 'gmail':
            account = args['gmail_account']
            if not await asyncio.to_thread(unibox.refresh_access_token, account, args['google_client_id'], args['google_client_secret']):
                raise ValueError('Google OAuth требует повторного подключения')
            await asyncio.to_thread(unibox._gmail_get_profile, account.access_token)
        else:
            account = args['office365_account']
            if not await asyncio.to_thread(unibox.refresh_office365_token, account, args['office365_client_id'], args['office365_client_secret'], args['office365_tenant_id']):
                raise ValueError('Microsoft OAuth требует повторного подключения')
            await asyncio.to_thread(unibox._graph_request_json, 'GET', 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox', account.access_token)
        report = {'ok': True, 'smtp': {'ok': True}, 'imap': {'ok': True}}
        health = {'smtp': 'oauth_checked', 'imap': 'oauth_checked'}
    except Exception as exc:
        error = sanitize_connection_error(str(exc))
        report = {'ok': False, 'smtp': {'ok': False, 'error': error}, 'imap': {'ok': False, 'error': error}}
        health = {'smtp': 'error', 'imap': 'error', 'error': error}
        inbox.paused = True
    key = f'local_connection_health_{inbox_id}'
    setting = await db.get(AppSetting, key)
    if not setting:
        setting = AppSetting(key=key); db.add(setting)
    setting.value = json.dumps(health)
    await db.flush()
    return report

class PostmasterConnection(BaseModel):
    domain: str
    access_token: str = Field(min_length=10, max_length=16000)
    owned_domain_confirmed: bool = False


@router.post('/reputation/postmaster/connection')
async def postmaster_connection(data: PostmasterConnection, db=Depends(get_db), _admin=Depends(require_admin)):
    import json
    from app.local_reputation import mail_domain
    if not data.owned_domain_confirmed:
        raise HTTPException(400, 'Подтвердите принадлежность почтового домена')
    try:
        domain = mail_domain(data.domain)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, 'Нужен корректный почтовый домен') from exc
    row = await db.get(AppSetting, 'local_postmaster_connection')
    if not row:
        row = AppSetting(key='local_postmaster_connection'); db.add(row)
    row.value = json.dumps({'domain': domain, 'access_token': data.access_token})
    await db.flush()
    return {'ok': True, 'domain': domain}


@router.get('/reputation/postmaster')
async def postmaster_report(db=Depends(get_db), _admin=Depends(require_admin)):
    import json
    from app.local_reputation import fetch_postmaster
    row = await db.get(AppSetting, 'local_postmaster_connection')
    if not row:
        return await fetch_postmaster('example.com', '')
    try:
        data = json.loads(row.value)
        return await fetch_postmaster(data['domain'], data['access_token'])
    except Exception:
        raise HTTPException(502, 'API Postmaster недоступен. Проверьте сеть, домен и отдельный OAuth-токен.') from None
