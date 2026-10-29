"""Authenticated company collection API; source keys never leave the backend."""
import asyncio
from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Text, cast, delete, exists, func, or_, select, update
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.collector.models import (CollectorSource, CollectionJob, CollectionTask, CollectionResult, Company,
    CompanyContact, CompanyObservation, ContactPermission, ConsentEvent, CollectorSetting)
from app.collector.sources import SOURCE_INFO, ensure_sources, connection_state
from app.collector.queue import create_job
from app.collector.imports import MAX_BYTES, parse_company_file
from app.collector.reports import statistics, export_bytes
from app.collector.consent import set_permission, transfer_to_quickly
from app.collector.websites import SourceError
from app.time import utcnow

router = APIRouter(prefix='/api/collector', tags=['company collector'], dependencies=[Depends(get_current_user)])


class LocalParserInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: Literal['yandex', '2gis', 'spark', 'rusprofile'] = 'yandex'
    name: str = Field(default='Локальный сбор', max_length=255)
    query: str = Field(default='', max_length=512)
    cities: list[str] = Field(default_factory=list, max_length=30)
    rubrics: list[str] = Field(default_factory=list, max_length=10)
    urls: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=100, ge=1, le=10000)
    timeout: int = Field(default=900, ge=30, le=3600)
    delay: float = Field(default=1, ge=.5, le=60)
    visible: bool = False
    crawl_websites: bool = True
    rights_confirmed: bool = False
    rights_basis: str = Field(default='', max_length=2000)


@router.get('/local-parser')
async def local_parser_status(db=Depends(get_db)):
    from app.collector import local_parser as bridge
    try:
        state = await bridge.request('GET', '/status')
        runs = await bridge.request('GET', '/runs')
    except SourceError as error:
        return {'connected': False, 'message': str(error), 'runs': []}
    jobs = (await db.scalars(select(CollectionJob).where(CollectionJob.params['parser_run'].as_string().is_not(None)).order_by(CollectionJob.created_at.desc()).limit(500))).all()
    linked = {}
    for job in jobs:
        if job.params.get('parser_run') and job.params['parser_run'] not in linked:
            linked[job.params['parser_run']] = job
    progress = await bridge.email_progress(db, [j.id for key, j in linked.items() if key in {r['id'] for r in runs}])
    result = []
    for run in runs:
        job = linked.get(run['id'])
        result.append({**run, 'job_id': job.id if job else None, 'email_status': job.status if job else None,
                       'email_progress': progress.get(job.id) if job else None})
    result.sort(key=lambda run: (not run.get('saved'), run.get('created', 0)), reverse=True)
    return {**state, 'runs': result}


@router.get('/local-parser/catalog')
async def local_parser_catalog(source: Literal['yandex', '2gis'] = 'yandex', q: str = Query('', max_length=200), kind: Literal['cities', 'rubrics'] = 'cities'):
    from urllib.parse import urlencode
    from app.collector.local_parser import request
    try: return await request('GET', '/catalog?' + urlencode({'source': source, 'q': q, 'kind': kind}))
    except SourceError as error: raise HTTPException(503, str(error))


@router.post('/local-parser/runs')
async def local_parser_start(data: LocalParserInput, _admin=Depends(require_admin)):
    from app.collector.local_parser import request
    try: return await request('POST', '/runs', data.model_dump())
    except SourceError as error: raise HTTPException(400, str(error))


@router.post('/local-parser/access/{source}')
async def local_parser_access(source: Literal['spark', 'rusprofile'], _admin=Depends(require_admin)):
    from app.collector.local_parser import request
    try: return await request('POST', '/access/' + source, {})
    except SourceError as error: raise HTTPException(400, str(error))


class ParserImportInput(BaseModel):
    rights_confirmed: bool = False
    rights_basis: str = Field(default='', max_length=2000)


@router.post('/local-parser/runs/{run_id}/import')
async def local_parser_import(run_id: str, data: ParserImportInput, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.collector.local_parser import import_run, run_path
    try:
        run_path(run_id)
        return await import_run(db, run_id, **data.model_dump())
    except (SourceError, ValueError) as error: raise HTTPException(400, str(error))


@router.post('/local-parser/runs/{run_id}/{action}')
async def local_parser_action(run_id: str, action: Literal['pause', 'resume'], _admin=Depends(require_admin)):
    from app.collector.local_parser import request, run_path
    try: return await request('POST', run_path(run_id) + '/' + action, {})
    except (SourceError, ValueError) as error: raise HTTPException(400, str(error))


class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='Сбор компаний', max_length=255)
    sources: list[Literal['website', 'import', 'yandex', 'twogis', 'spark', 'fns']] = Field(default_factory=lambda: ['website'], max_length=6)
    country: str = Field(default='Россия', max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=100)
    cities: list[str] = Field(default_factory=list, max_length=100)
    okved: list[str] = Field(default_factory=list, max_length=50)
    query: str = Field(default='', max_length=512)
    employees_min: int | None = Field(default=None, ge=0)
    employees_max: int | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=10000)
    only_active: bool = False
    only_email: bool = True
    exclude_existing: bool = True
    crawl_websites: bool = True
    check_mx: bool = False
    websites: list[str] = Field(default_factory=list, max_length=1000)
    rights_confirmed: bool = False
    rights_basis: str = Field(default='', max_length=2000)


class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    api_key: str = Field(default='', max_length=2000)
    credentials: str = Field(default='', max_length=2000)
    clear_key: bool = False
    allow_store: bool = False
    allow_export: bool = False
    terms_url: str = Field(default='', max_length=2048)
    rights_basis: str = Field(default='', max_length=2000)
    paid_authorized: bool = False
    daily_request_limit: int = Field(default=0, ge=0, le=100000)
    config: dict = Field(default_factory=dict)


@router.get('/sources')
async def sources(db=Depends(get_db)):
    await ensure_sources(db)
    result = []
    for source in (await db.scalars(select(CollectorSource).order_by(CollectorSource.key))).all():
        state, message = connection_state(source)
        result.append({key: getattr(source, key) for key in ['key', 'enabled', 'config', 'allow_store', 'allow_export',
            'terms_url', 'rights_basis', 'paid_authorized', 'daily_request_limit', 'budget_day', 'requests_used']} | {
                'name': SOURCE_INFO[source.key][0], 'state': state, 'message': message,
                'has_api_key': bool(source.api_key), 'has_credentials': bool(source.credentials)})
    return result


@router.put('/sources/{key}')
async def save_source(key: str, data: SourceInput, db=Depends(get_db), _admin=Depends(require_admin)):
    if key not in SOURCE_INFO: raise HTTPException(404, 'Источник не найден')
    await ensure_sources(db)
    allowed_config = {'plan', 'daily_budget_rub', 'request_cost_rub', 'interval_seconds', 'contacts_access',
                      'employees_access', 'inn_access', 'region_id', 'rubric_id', 'wsdl_url', 'username'}
    if set(data.config) - allowed_config: raise HTTPException(400, 'Неизвестные настройки источника; секреты вводятся в отдельное поле')
    for field in ['daily_budget_rub', 'request_cost_rub', 'interval_seconds']:
        if field in data.config:
            try:
                value = float(data.config[field])
                if not 0 <= value <= 1000000: raise ValueError()
            except (ValueError, TypeError): raise HTTPException(400, 'Некорректный бюджет или интервал')
    for field in ['contacts_access', 'employees_access', 'inn_access']:
        if field in data.config and not isinstance(data.config[field], bool):
            raise HTTPException(400, 'Доступ к платным полям задаётся переключателем да/нет')
    if data.terms_url:
        from app.collector.validation import website_url
        try: website_url(data.terms_url)
        except ValueError as error: raise HTTPException(400, str(error))
    if data.allow_store and not data.rights_basis.strip(): raise HTTPException(400, 'Укажите документ / основание прав хранения')
    if data.allow_export and not data.allow_store: raise HTTPException(400, 'Экспорт требует права хранения')
    source = await db.get(CollectorSource, key)
    for field, value in data.model_dump(exclude={'api_key', 'credentials', 'clear_key'}).items(): setattr(source, field, value)
    if data.api_key: source.api_key = data.api_key
    if data.credentials: source.credentials = data.credentials
    if data.clear_key: source.api_key, source.credentials = '', ''
    await db.flush()
    return {'ok': True, 'state': connection_state(source)[0]}


@router.get('/settings')
async def settings(db=Depends(get_db)):
    rows = (await db.scalars(select(CollectorSetting))).all()
    return {row.key: row.value for row in rows}


class ReportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = False
    recipient: str = Field(default='', max_length=255)
    inbox_id: int | None = None
    attach_csv: bool = False


@router.put('/settings/report')
async def report_settings(data: ReportInput, db=Depends(get_db), _admin=Depends(require_admin)):
    from app.local_imports import email_address
    from app.models import Inbox, SmtpAccount
    if data.enabled:
        try: email_address(data.recipient)
        except ValueError as error: raise HTTPException(400, str(error))
        if not await db.get(Inbox, data.inbox_id or 0) or not await db.scalar(select(SmtpAccount.id).where(SmtpAccount.inbox_id == data.inbox_id)):
            raise HTTPException(400, 'Выберите подключённый SMTP-ящик для отчётов')
    row = await db.get(CollectorSetting, 'report')
    if not row: db.add(CollectorSetting(key='report', value=data.model_dump()))
    else: row.value = data.model_dump()
    await db.flush()
    return {'ok': True}


def job_json(job):
    return {c.name: getattr(job, c.name) for c in job.__table__.columns}


@router.post('/jobs')
async def start_job(data: JobInput, db=Depends(get_db), _admin=Depends(require_admin)):
    try: return job_json(await create_job(db, data.model_dump()))
    except (ValueError, SourceError) as error:
        raise HTTPException(400, str(error))


@router.post('/import')
async def import_companies(file: UploadFile = File(...), params: str = Form(...), db=Depends(get_db), _admin=Depends(require_admin)):
    try:
        data = JobInput.model_validate_json(params)
        raw = await file.read(MAX_BYTES + 1)
        rows = await asyncio.to_thread(parse_company_file, raw, file.filename or '')
        if not rows: raise ValueError('Файл пуст')
        job = await create_job(db, data.model_dump(), records=rows)
        return job_json(job) | {'rows': len(rows)}
    except (ValueError, SourceError) as error: raise HTTPException(400, str(error))
    finally: await file.close()


@router.get('/jobs')
async def jobs(page: int = Query(1, ge=1), db=Depends(get_db)):
    rows = (await db.scalars(select(CollectionJob).order_by(CollectionJob.created_at.desc()).offset((page-1)*20).limit(20))).all()
    results = []
    for job in rows:
        ids = list((await db.scalars(select(CollectionResult.company_id).where(CollectionResult.job_id == job.id))).all())
        counters = await statistics(db, ids)
        states = dict((await db.execute(select(CollectionTask.status, func.count()).where(CollectionTask.job_id == job.id).group_by(CollectionTask.status))).all())
        results.append(job_json(job) | {'stats': counters, 'tasks': states})
    return {'items': results, 'total': await db.scalar(select(func.count()).select_from(CollectionJob))}


@router.post('/jobs/{job_id}/{action}')
async def job_action(job_id: str, action: Literal['pause', 'resume', 'stop'], db=Depends(get_db), _admin=Depends(require_admin)):
    job = await db.get(CollectionJob, job_id, with_for_update=True)
    if not job: raise HTTPException(404, 'Запуск не найден')
    if action == 'pause' and job.status in {'queued', 'running'}: job.status = 'paused'
    elif action == 'resume' and job.status == 'paused': job.status = 'queued'
    elif action == 'stop' and job.status in {'queued', 'running', 'paused'}:
        job.status, job.finished_at = 'stopped', utcnow()
        await db.execute(update(CollectionTask).where(CollectionTask.job_id == job_id, CollectionTask.status.in_(['pending', 'retry'])).values(status='cancelled', payload={}))
    else: raise HTTPException(409, 'Это действие недоступно для текущего состояния')
    await db.flush()
    return job_json(job)


@router.get('/jobs/{job_id}/errors')
async def job_errors(job_id: str, db=Depends(get_db)):
    return [{'task_id': t.id, 'source': t.source, 'kind': t.kind, 'error': t.error, 'attempts': t.attempts, 'due_at': t.due_at}
            for t in (await db.scalars(select(CollectionTask).where(CollectionTask.job_id == job_id, CollectionTask.error != '').limit(100))).all()]


def filtered_query(q='', city='', region='', okved='', source='', status='', has_email: bool | None = None, consent='', job_id='', only_active=False, employees_min=None, employees_max=None, exclude_existing=False):
    query = select(Company)
    if q: query = query.where(or_(Company.name.ilike('%'+q+'%'), Company.inn.contains(q), Company.website.ilike('%'+q+'%')))
    if city: query = query.where(Company.city.in_([v.strip() for v in city.split(';') if v.strip()]))
    if region: query = query.where(Company.region.in_([v.strip() for v in region.split(';') if v.strip()]))
    if okved:
        codes = [v.strip() for v in okved.split(';') if v.strip()]
        query = query.where(or_(*[or_(Company.okved_main == code, Company.okved_main.startswith(code+'.'), cast(Company.okved_extra, Text).contains('"'+code+'"')) for code in codes]))
    if status: query = query.where(Company.status == status)
    if only_active: query = query.where(Company.active.is_(True))
    if employees_min is not None: query = query.where(Company.employees >= employees_min)
    if employees_max is not None: query = query.where(Company.employees <= employees_max)
    if source: query = query.where(exists().where(CompanyObservation.company_id == Company.id, CompanyObservation.source == source))
    if job_id:
        predicate = [CollectionResult.company_id == Company.id, CollectionResult.job_id == job_id]
        if exclude_existing: predicate.append(CollectionResult.was_existing.is_(False))
        query = query.where(exists().where(*predicate))
    email_predicate = exists().where(CompanyContact.company_id == Company.id, CompanyContact.kind == 'email')
    if has_email is not None: query = query.where(email_predicate if has_email else ~email_predicate)
    if consent:
        query = query.where(exists().where(CompanyContact.company_id == Company.id, CompanyContact.kind == 'email',
            ContactPermission.email == CompanyContact.value, ContactPermission.status == consent))
    return query


async def company_json(db, company):
    result = {c.name: getattr(company, c.name) for c in company.__table__.columns if not c.name.startswith('normalized_')}
    contacts = list((await db.scalars(select(CompanyContact).where(CompanyContact.company_id == company.id).order_by(CompanyContact.is_primary.desc(), CompanyContact.id))).all())
    result['contacts'] = []
    for contact in contacts:
        permission = await db.get(ContactPermission, contact.value) if contact.kind == 'email' else None
        result['contacts'].append({c.name: getattr(contact, c.name) for c in contact.__table__.columns} | {'consent': permission.status if permission else 'unknown'})
    result['sources'] = list((await db.scalars(select(CompanyObservation.source).where(CompanyObservation.company_id == company.id).distinct())).all())
    return result


@router.get('/companies')
async def companies(q: str = '', city: str = '', region: str = '', okved: str = '', source: str = '', status: str = '', has_email: bool | None = True,
                    consent: str = '', job_id: str = '', only_active: bool = False, employees_min: int | None = None, employees_max: int | None = None,
                    exclude_existing: bool = False, sort: Literal['name', 'city', 'checked_at', 'created_at'] = 'created_at',
                    descending: bool = True, page: int = Query(1, ge=1), db=Depends(get_db)):
    query = filtered_query(q, city, region, okved, source, status, has_email, consent, job_id, only_active, employees_min, employees_max, exclude_existing)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    column = getattr(Company, sort)
    rows = (await db.scalars(query.order_by(column.desc() if descending else column.asc(), Company.id).offset((page-1)*50).limit(50))).all()
    return {'items': [await company_json(db, c) for c in rows], 'total': total, 'page': page, 'page_size': 50}


@router.get('/stats')
async def stats(db=Depends(get_db)):
    counters = await statistics(db, select(CompanyContact.company_id).where(CompanyContact.kind == 'email'))
    for name in ['duplicates', 'sites_checked', 'errors']:
        counters[name] = await db.scalar(select(func.sum(getattr(CollectionJob, name)))) or 0
    return counters


@router.get('/companies/{company_id}')
async def company_detail(company_id: int, db=Depends(get_db)):
    company = await db.get(Company, company_id)
    if not company: raise HTTPException(404, 'Компания не найдена')
    observations = (await db.scalars(select(CompanyObservation).where(CompanyObservation.company_id == company_id))).all()
    data = await company_json(db, company)
    data['observations'] = [{c.name: getattr(o, c.name) for c in o.__table__.columns} for o in observations]
    data['consent_history'] = [{c.name: getattr(e, c.name) for c in e.__table__.columns} for e in (await db.scalars(
        select(ConsentEvent).where(ConsentEvent.email.in_([c['value'] for c in data['contacts'] if c['kind'] == 'email'])).order_by(ConsentEvent.id.desc()))).all()]
    return data


class CompanyUpdate(BaseModel):
    status: Literal['new', 'verified', 'excluded']


@router.patch('/companies/{company_id}')
async def update_company(company_id: int, data: CompanyUpdate, db=Depends(get_db), _admin=Depends(require_admin)):
    company = await db.get(Company, company_id)
    if not company: raise HTTPException(404, 'Компания не найдена')
    company.status = data.status
    if data.status == 'verified': company.needs_review = False
    await db.flush()
    return {'ok': True}


@router.put('/contacts/{contact_id}/primary')
async def primary_contact(contact_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
    contact = await db.get(CompanyContact, contact_id)
    if not contact or contact.kind != 'email': raise HTTPException(404, 'Email не найден')
    await db.execute(update(CompanyContact).where(CompanyContact.company_id == contact.company_id, CompanyContact.kind == 'email').values(is_primary=False))
    contact.is_primary = True
    await db.flush()
    return {'ok': True}


class ConsentInput(BaseModel):
    email: str
    status: Literal['unknown', 'confirmed', 'absent', 'refused']
    advertiser: str = Field(default='', max_length=512)
    evidence: str = Field(default='', max_length=2000)
    evidence_url: str = Field(default='', max_length=2048)
    granted_at: datetime | None = None


@router.post('/consents')
async def consent_update(data: ConsentInput, db=Depends(get_db), admin=Depends(require_admin)):
    values = data.model_dump()
    if values['granted_at'] and values['granted_at'].tzinfo:
        from datetime import timezone
        values['granted_at'] = values['granted_at'].astimezone(timezone.utc).replace(tzinfo=None)
    try: await set_permission(db, **values, actor_id=getattr(admin, 'id', None))
    except ValueError as error: raise HTTPException(400, str(error))
    return {'ok': True}


@router.get('/consents')
async def permissions_list(status: str = '', q: str = '', page: int = Query(1, ge=1), db=Depends(get_db)):
    query = select(ContactPermission)
    if status: query = query.where(ContactPermission.status == status)
    if q: query = query.where(ContactPermission.email.contains(q.lower()))
    return [{c.name: getattr(row, c.name) for c in row.__table__.columns} for row in (await db.scalars(query.order_by(ContactPermission.updated_at.desc()).offset((page-1)*50).limit(50))).all()]


class SelectionFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    q: str = ''
    city: str = ''
    region: str = ''
    okved: str = ''
    source: str = ''
    status: str = ''
    consent: str = ''
    job_id: str = ''
    only_active: bool = False
    employees_min: int | None = Field(default=None, ge=0)
    employees_max: int | None = Field(default=None, ge=0)
    exclude_existing: bool = False


class Selection(BaseModel):
    company_ids: list[int] = Field(default_factory=list, max_length=10000)
    campaign_id: int | None = None
    all_matching: bool = False
    filters: SelectionFilters = Field(default_factory=SelectionFilters)
    excluded_ids: list[int] = Field(default_factory=list, max_length=10000)


def selection_query(data: Selection, *, job_id=''):
    """Resolve all pages using the same filters as the visible company list."""
    if data.all_matching:
        if data.company_ids:
            raise HTTPException(400, 'Выберите весь список или отдельные компании')
        query = filtered_query(has_email=True, **data.filters.model_dump())
        if data.excluded_ids:
            query = query.where(Company.id.not_in(data.excluded_ids))
        return query
    query = filtered_query(has_email=True)
    if data.company_ids:
        return query.where(Company.id.in_(data.company_ids))
    if job_id:
        return query.where(Company.id.in_(select(CollectionResult.company_id).where(CollectionResult.job_id == job_id)))
    raise HTTPException(400, 'Выберите компании или нажмите «Выбрать все»')


@router.post('/transfer')
async def transfer(data: Selection, db=Depends(get_db), _admin=Depends(require_admin)):
    ids = selection_query(data).with_only_columns(Company.id)
    try: return await transfer_to_quickly(db, ids, campaign_id=data.campaign_id)
    except ValueError as error: raise HTTPException(400, str(error))


@router.post('/export/{format}')
async def export(format: Literal['csv', 'xlsx'], data: Selection, job_id: str = '', db=Depends(get_db)):
    query = selection_query(data, job_id=job_id)
    rows = list((await db.scalars(query.order_by(Company.id))).all())
    raw, omitted = await export_bytes(db, rows, format)
    mime = 'text/csv; charset=utf-8' if format == 'csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    return Response(raw, media_type=mime, headers={'Content-Disposition': f'attachment; filename="quickly-companies.{format}"', 'X-Collector-Omitted': str(omitted)})


@router.delete('/contacts/{contact_id}')
async def erase_contact(contact_id: int, db=Depends(get_db), _admin=Depends(require_admin)):
    contact = await db.get(CompanyContact, contact_id)
    if not contact: raise HTTPException(404, 'Контакт не найден')
    value = contact.value
    # Remove raw imported copies and cancel only tasks containing this contact.
    await db.execute(update(CollectionTask).where(cast(CollectionTask.payload, Text).contains(value))
        .values(payload={}, status='cancelled', claim_token='', lease_until=None, error=''))
    await db.execute(delete(CompanyContact).where(CompanyContact.kind == contact.kind, CompanyContact.value == value))
    if contact.kind == 'email':
        from app.local_models import Suppression, MailOperation
        from app.models import Lead, EmailLog, LeadReply, CampaignLead, QueueSlot, LeadUnsubscribeToken, EmailClick, EmailOpen, CustomEmailOverride
        import hashlib
        # Keep only a one-way opt-out fingerprint, so recollection cannot silently
        # re-enable this address while the clear-text personal data is erased.
        digest = 'sha256:' + hashlib.sha256(value.lower().encode()).hexdigest()
        if not await db.get(Suppression, digest): db.add(Suppression(email=digest, reason='Контакт удалён; не собирать повторно'))
        await db.execute(delete(Suppression).where(Suppression.email == value))
        await db.execute(delete(ContactPermission).where(ContactPermission.email == value))
        await db.execute(delete(ConsentEvent).where(ConsentEvent.email == value))
        lead_ids = list((await db.scalars(select(Lead.id).where(func.lower(Lead.email) == value))).all())
        enrollments = select(CampaignLead.id).where(CampaignLead.lead_id.in_(lead_ids))
        await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id.in_(enrollments)))
        await db.execute(delete(LeadUnsubscribeToken).where(LeadUnsubscribeToken.lead_id.in_(lead_ids)))
        await db.execute(delete(LeadReply).where(LeadReply.lead_id.in_(lead_ids)))
        log_ids = select(EmailLog.id).where(EmailLog.lead_id.in_(lead_ids))
        await db.execute(delete(EmailClick).where(EmailClick.email_log_id.in_(log_ids)))
        await db.execute(delete(EmailOpen).where(EmailOpen.email_log_id.in_(log_ids)))
        await db.execute(delete(EmailLog).where(EmailLog.lead_id.in_(lead_ids)))
        await db.execute(delete(CustomEmailOverride).where(CustomEmailOverride.campaign_lead_id.in_(enrollments)))
        await db.execute(delete(CampaignLead).where(CampaignLead.lead_id.in_(lead_ids)))
        await db.execute(delete(Lead).where(Lead.id.in_(lead_ids)))
        await db.execute(update(MailOperation).where(MailOperation.recipient == value).values(recipient='[удалено]'))
    await db.flush()
    return {'ok': True, 'scope': 'База компаний, согласия, контакты Quickly и содержащие адрес задания. Серверные почтовые архивы и ранее созданные резервные копии не изменяются.'}
