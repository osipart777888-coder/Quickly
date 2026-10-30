"""Local installation control, account overview and delivery ledger."""
from datetime import timedelta
from typing import Literal
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
    mail_concurrency: int | None = Field(default=None,ge=1,le=16)


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
            'reason': control.reason, 'updated_at': control.updated_at, 'mail_concurrency': control.mail_concurrency}


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
    if data.mail_concurrency is not None:
        control.mail_concurrency=data.mail_concurrency
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
    observation_hours: Literal[24,48,72] = 48


class PlanRequest(DiagnosticRequest):
    mode: Literal['pair','group'] = 'pair'
    sender_id: int | None = Field(default=None, ge=1)
    recipient_id: int | None = Field(default=None, ge=1)
    per_account_daily_limit: int = Field(default=1, ge=1, le=100)
    auto_reply: bool = False
    poll_interval_seconds: int = Field(default=300,ge=60,le=3600)
    name: str = Field(min_length=1, max_length=255)
    inbox_ids: list[int] = Field(default_factory=list, max_length=2000)
    enabled: bool = False
    interval_minutes: int = Field(default=1440, ge=15, le=43200)
    max_messages_per_day: int = Field(default=1, ge=1, le=100000)
    sending_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    hours_start: str = Field(default='09:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    hours_end: str = Field(default='18:00', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')


def run_json(run):
    result={key: getattr(run, key) for key in ['id', 'plan_id', 'sender_id', 'recipient_id', 'status',
        'message_id', 'reply_message_id', 'folder', 'authentication_headers', 'duration_ms', 'error',
        'report', 'created_at', 'received_at', 'batch_id', 'started_at','accepted_at','reply_accepted_at',
        'sender_email','recipient_email','reply_started_at','last_checked_at','next_check_at','check_attempts','observation_deadline','reply_observation_deadline',
        'observation_status','check_error','folder_kind']}

    from app.diagnostic_observation import display_status
    result['display_status']=display_status(run)
    result['transport_status']=(run.report or {}).get('send_status') or ('accepted' if run.accepted_at or (run.report or {}).get('smtp_accepted') else run.status)
    return result


@router.get('/diagnostics/plans')
async def diagnostic_plans(db=Depends(get_db)):
    from app.local_models import DiagnosticPlan, DiagnosticDayQuota
    rows = (await db.execute(select(DiagnosticPlan).order_by(DiagnosticPlan.id.desc()))).scalars().all()
    counters = (await db.execute(select(DiagnosticRun.plan_id,DiagnosticRun.batch_id,DiagnosticRun.status,func.count())
        .join(DiagnosticPlan,DiagnosticPlan.id == DiagnosticRun.plan_id)
        .where(DiagnosticRun.batch_id == DiagnosticPlan.last_batch_id)
        .group_by(DiagnosticRun.plan_id,DiagnosticRun.batch_id,DiagnosticRun.status))).all()
    budgets = dict((await db.execute(select(DiagnosticDayQuota.plan_id,DiagnosticDayQuota.used)
        .where(DiagnosticDayQuota.day == day_bounds()[0]))).all())
    from app.local_group_diagnostics import group_members
    all_ids={iid for plan in rows for iid in plan.inbox_ids}
    members={m['id']:m for m in await group_members(db,list(all_ids))} if all_ids else {}
    paused=set((await db.execute(select(Inbox.id).where(Inbox.id.in_(all_ids),Inbox.paused==True))).scalars().all()) if all_ids else set()
    rollups=(await db.execute(select(DiagnosticRun.plan_id,
        func.count().filter((DiagnosticRun.accepted_at.is_not(None))|(DiagnosticRun.report['smtp_accepted'].as_boolean()==True)),
        func.count().filter(DiagnosticRun.received_at.is_not(None)),
        func.count().filter(DiagnosticRun.observation_status=='temporarily_unavailable'),
        func.count().filter(DiagnosticRun.status.in_(['uncertain','reply_pending'])),
        func.count().filter(DiagnosticRun.observation_status=='expired'))
        .join(DiagnosticPlan,DiagnosticRun.plan_id==DiagnosticPlan.id)
        .where(DiagnosticRun.batch_id==DiagnosticPlan.last_batch_id).group_by(DiagnosticRun.plan_id))).all()
    rollups={r[0]:r[1:] for r in rollups}
    result = []
    for row in rows:
        item = {key:getattr(row,key) for key in ['id','name','inbox_ids','sender_id','recipient_id','mode',
            'per_account_daily_limit','auto_reply','enabled','interval_minutes','max_messages_per_day',
            'sending_days','hours_start','hours_end','next_run_at','last_batch_id','observation_hours','poll_interval_seconds']}
        item['cycle_counts'] = {status:count for pid,batch,status,count in counters if pid == row.id and batch == row.last_batch_id}
        item['used_today'] = budgets.get(row.id,0)
        accepted,received,unavailable,uncertain,expired=rollups.get(row.id,(0,0,0,0,0))
        item['summary']={'total':len(row.inbox_ids),'available':sum(members.get(i,{}).get('eligible',False) for i in row.inbox_ids),
            'paused':sum(i in paused for i in row.inbox_ids),'queued':item['cycle_counts'].get('queued',0),
            'accepted':accepted,'received':received,'unavailable':unavailable,'uncertain':uncertain,'expired':expired}
        result.append(item)
    return result


@router.post('/diagnostics/plans')
async def create_plan(data: PlanRequest, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_models import DiagnosticPlan
    from app.local_group_diagnostics import group_members
    if not data.owned_accounts_confirmed:
        raise HTTPException(400,'Подтвердите, что все выбранные ящики принадлежат вам')
    if data.mode == 'pair' and (not data.sender_id or not data.recipient_id or data.sender_id == data.recipient_id):
        raise HTTPException(400,'Выберите разные подключённые ящики')
    ids = sorted(set(data.inbox_ids + ([data.sender_id,data.recipient_id] if data.mode == 'pair' else [])))
    if len(ids) < 2 or any(i <= 0 for i in ids):
        raise HTTPException(400,'Выберите минимум два собственных ящика')
    connected = set((await db.execute(select(Inbox.id).where(Inbox.id.in_(ids)))).scalars().all())
    if connected != set(ids):
        raise HTTPException(400,'В группе есть неизвестный почтовый аккаунт')
    if data.mode == 'group' and sum(item['eligible'] for item in await group_members(db,ids)) < 2:
        raise HTTPException(400,'Нужны минимум два доступных ящика с настроенным чтением почты')
    if not data.sending_days or any(d not in range(7) for d in data.sending_days) or data.hours_start >= data.hours_end:
        raise HTTPException(400,'Укажите дни и корректный интервал часов')
    values = data.model_dump(exclude={'owned_accounts_confirmed','inbox_ids','sender_id','recipient_id'})
    if data.mode == 'group':
        values['enabled'] = False  # Separate explicit enable action starts the saved schedule.
    plan = DiagnosticPlan(**values,inbox_ids=ids,sender_id=data.sender_id if data.mode == 'pair' else ids[0],
        recipient_id=data.recipient_id if data.mode == 'pair' else ids[1],next_run_at=utcnow())
    db.add(plan)
    await db.flush()
    return {'id':plan.id,'mode':plan.mode,'ok':True}


class GroupPreview(BaseModel):
    inbox_ids: list[int] = Field(min_length=2,max_length=2000)


@router.post('/diagnostics/group-preview')
async def group_preview(data: GroupPreview, db=Depends(get_db)):
    from app.local_group_diagnostics import group_members,ring_pairs
    members = await group_members(db,data.inbox_ids)
    pairs = ring_pairs(members)
    return {'selected':len(set(data.inbox_ids)),'eligible':sum(r['eligible'] for r in members),
            'members':members,'pairs':pairs,'messages_per_cycle':len(pairs)}


@router.post('/diagnostics/plans/{plan_id}/enqueue')
async def enqueue_group(plan_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_group_diagnostics import enqueue_group_cycle
    try:
        return await enqueue_group_cycle(db,plan_id)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


@router.post('/diagnostics/plans/{plan_id}/cancel-queue')
async def cancel_group_queue(plan_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
    from sqlalchemy import update
    from app.local_models import DiagnosticPlan
    if not await db.get(DiagnosticPlan,plan_id):
        raise HTTPException(404,'Расписание не найдено')
    result = await db.execute(update(DiagnosticRun).where(DiagnosticRun.plan_id == plan_id,DiagnosticRun.status == 'queued')
        .values(status='cancelled',error='Ожидающая проверка отменена пользователем'))
    await db.flush()
    return {'cancelled':result.rowcount}


class PlanToggle(BaseModel):
    enabled: bool


class GroupSettings(BaseModel):
    name: str = Field(min_length=1,max_length=255)
    interval_minutes: int = Field(ge=15,le=43200)
    per_account_daily_limit: int = Field(ge=1,le=100)
    max_messages_per_day: int = Field(ge=1,le=100000)
    auto_reply: bool
    observation_hours: Literal[24,48,72] | None = None
    poll_interval_seconds: int | None = Field(default=None,ge=60,le=3600)
    sending_days: list[int] = Field(min_length=1,max_length=7)
    hours_start: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    hours_end: str = Field(pattern=r'^([01]\d|2[0-3]):[0-5]\d$')


@router.put('/diagnostics/plans/{plan_id}/settings')
async def edit_group_settings(plan_id: int, data: GroupSettings, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_models import DiagnosticPlan
    plan = await db.scalar(select(DiagnosticPlan).where(DiagnosticPlan.id == plan_id).with_for_update())
    if not plan or plan.mode != 'group':
        raise HTTPException(404,'Групповое расписание не найдено')
    if any(day not in range(7) for day in data.sending_days) or data.hours_start >= data.hours_end:
        raise HTTPException(400,'Укажите дни и корректный интервал часов')
    for key,value in data.model_dump(exclude_none=True).items():
        setattr(plan,key,value)
    plan.enabled = False
    plan.next_run_at = utcnow()
    await db.flush()
    return {'ok':True,'enabled':False,'pending_preserved':True}


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
async def diagnostic_runs(plan_id: int | None = None, inbox_id: int | None = None, db=Depends(get_db)):
    query = select(DiagnosticRun).order_by(DiagnosticRun.created_at.desc()).limit(200)
    if plan_id:
        query = query.where(DiagnosticRun.plan_id == plan_id)
    if inbox_id:
        from sqlalchemy import or_
        query = query.where(or_(DiagnosticRun.sender_id == inbox_id,DiagnosticRun.recipient_id == inbox_id))
    rows = (await db.execute(query)).scalars().all()
    return [run_json(row) for row in rows]


@router.get('/diagnostics/export.csv')
async def export_diagnostics(plan_id:int|None=None,inbox_id:int|None=None,db=Depends(get_db)):
    import csv,io
    from app.log_redaction import redact_credentials
    from app.diagnostic_observation import STATUS_RU
    from fastapi.responses import StreamingResponse
    from sqlalchemy.orm import aliased
    sender,recipient=aliased(Inbox),aliased(Inbox)
    query=select(DiagnosticRun,sender.email,recipient.email).outerjoin(sender,sender.id==DiagnosticRun.sender_id).outerjoin(recipient,recipient.id==DiagnosticRun.recipient_id).order_by(DiagnosticRun.created_at,DiagnosticRun.id)
    if plan_id:query=query.where(DiagnosticRun.plan_id==plan_id)
    if inbox_id:query=query.where((DiagnosticRun.sender_id==inbox_id)|(DiagnosticRun.recipient_id==inbox_id))
    headers=['Проверка','Отправитель','Получатель','Статус','Статус отправки','Время попытки UTC','Принято сервером UTC','Получение подтверждено UTC','Папка','Последняя проверка UTC','Попыток чтения','Завершение наблюдения UTC','Причина']
    def safe(value):
        if value is None:return ''
        if hasattr(value,'isoformat'):return value.isoformat()+'Z'
        value=redact_credentials(str(value))
        return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
    async def generate():
        buf=io.StringIO();writer=csv.writer(buf)
        writer.writerow(headers);yield '\ufeff'+buf.getvalue();buf.seek(0);buf.truncate(0)
        result=await db.stream(query.execution_options(yield_per=500))
        async for run,sender_email,recipient_email in result:
            item=run_json(run)
            writer.writerow([safe(v) for v in [run.id,run.sender_email or sender_email,run.recipient_email or recipient_email,STATUS_RU.get(item['display_status'],item['display_status']),STATUS_RU.get(item['transport_status'],item['transport_status']),run.started_at,run.accepted_at,run.received_at,('Папка неизвестна' if run.folder_kind=='unknown' else run.folder),run.last_checked_at,run.check_attempts,run.reply_observation_deadline if run.reply_message_id else run.observation_deadline,run.check_error or run.error]])
            yield buf.getvalue();buf.seek(0);buf.truncate(0)
    return StreamingResponse(generate(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="quickly-diagnostics.csv"','Cache-Control':'no-store'})


@router.post('/diagnostics/run')
async def run_diagnostic(data: DiagnosticRequest, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_diagnostics import start_diagnostic
    if not data.owned_accounts_confirmed:
        raise HTTPException(400, 'Диагностика разрешена только между вашими собственными ящиками')
    try:
        return run_json(await start_diagnostic(db, data.sender_id, data.recipient_id,observation_hours=data.observation_hours))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post('/diagnostics/runs/{run_id}/check')
async def diagnostic_check(run_id: str, reply: bool = False, db=Depends(get_db)):
    from app.local_diagnostics import check_receipt
    run = await db.get(DiagnosticRun, run_id)
    if not run:
        raise HTTPException(404, 'Проверка не найдена')
    try:
        return run_json(await check_receipt(db, run, reply=reply))
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc


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
    from app.collector.models import ContactPermission
    from app.local_imports import email_address
    from hashlib import sha256
    refused = set((await db.scalars(select(ContactPermission.email).where(ContactPermission.status == 'refused'))).all())
    invalid = 0
    for lead in contacts:
        try:
            email = email_address(lead.email)
        except ValueError:
            invalid += 1
            continue
        if email in suppressed or email in refused or 'sha256:' + sha256(email.encode()).hexdigest() in suppressed or lead.status in {'unsubscribed','invalid','bounced','blacklisted'}:
            invalid += 1
    if invalid:
        errors.append(f'Контактов, исключённых из отправки: {invalid}')
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
    from app.mail_io import mail_thread
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
            if not await mail_thread(db,unibox.refresh_access_token, account, args['google_client_id'], args['google_client_secret']):
                raise ValueError('Google OAuth требует повторного подключения')
            await mail_thread(db,unibox._gmail_get_profile, account.access_token,mail_inbox_id=inbox.id)
        else:
            account = args['office365_account']
            if not await mail_thread(db,unibox.refresh_office365_token, account, args['office365_client_id'], args['office365_client_secret'], args['office365_tenant_id']):
                raise ValueError('Microsoft OAuth требует повторного подключения')
            await mail_thread(db,unibox._graph_request_json, 'GET', 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox', account.access_token,mail_inbox_id=inbox.id)
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
