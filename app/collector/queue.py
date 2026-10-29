"""Durable independent collection queue; one checkpoint per small batch/page.

    Leases survive restarts. Claims and source budget debits are committed before
    IO. Expired work is replayed idempotently; each observation has a unique key.
"""
import asyncio
import hashlib
import json
import uuid
from datetime import timedelta
from sqlalchemy import func, or_, select, update
from app.database import AsyncSessionLocal
from app.maintenance import background_work
from app.collector.models import CollectionJob, CollectionTask, CollectionResult, Company, CollectorSource, CollectorSetting, WebsiteThrottle
from app.collector.repository import save_company, add_contacts, has_collectable_email
from app.collector.sources import ADAPTERS, ensure_sources, permissions, reserve_api_request, connection_state
from app.collector.validation import domain_of, website_url, normalize_record
from app.collector.websites import SourceError, extract_website
from app.time import utcnow


def task_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def enqueue(db, job_id, kind, source, payload):
    key = task_hash([kind, source, payload])
    if not await db.scalar(select(CollectionTask.id).where(CollectionTask.job_id == job_id, CollectionTask.task_key == key)):
        db.add(CollectionTask(job_id=job_id, kind=kind, source=source, task_key=key, payload=payload))
        await db.flush()


async def create_job(db, params, *, records=None, job_id=None):
    params = dict(params)
    params['only_email'] = True
    for key in ['regions', 'cities', 'okved', 'websites']:
        params[key] = [v.strip() for v in params.get(key, []) if v.strip()]
    await ensure_sources(db)
    if not params.get('rights_confirmed'):
        raise ValueError('Подтвердите права хранения и обработки выбранных данных')
    sources = params.get('sources', [])
    if not sources: raise ValueError('Выберите хотя бы один источник')
    if len(sources) > 10: raise ValueError('Слишком много источников')
    unavailable = []
    usable = []
    for source_key in sources:
        source = await db.get(CollectorSource, source_key)
        if not source: raise ValueError('Неизвестный источник')
        state, reason = connection_state(source)
        if state != 'ready':
            unavailable.append(source_key + ': ' + reason)
        else:
            permissions(source)
            usable.append(source_key)
    if 'website' in sources and not records and not params.get('websites'):
        raise ValueError('Введите сайты организаций или загрузите файл компаний')
    if any(s in ADAPTERS for s in sources) and not params.get('query'):
        raise ValueError('Для поиска в каталоге нужна отрасль или поисковая фраза')
    # Validate all origins BEFORE persisting unlicensed imported records in tasks.
    for record in records or []:
        origin = record.get('source') or ('fns' if sources == ['fns'] else 'import')
        if origin not in {'import', 'fns', 'website', 'yandex', 'twogis', 'spark', 'local_yandex', 'local_2gis', 'local_spark', 'local_rusprofile'}:
            raise ValueError('Неизвестный источник строки импорта')
        permissions(await db.get(CollectorSource, origin))
    job = CollectionJob(id=job_id or uuid.uuid4().hex, name=params.get('name') or 'Сбор компаний', params=dict(params),
                        errors=len(unavailable), last_error='; '.join(unavailable))
    db.add(job)
    await db.flush()
    limit = params.get('limit', 100)
    for offset in range(0, min(len(records or []), limit), 25):
        await enqueue(db, job.id, 'import', 'import', {'rows': records[offset:min(offset + 25, limit)]})
    for url in params.get('websites', [])[:limit]:
        url = website_url(url)
        await enqueue(db, job.id, 'import', 'website', {'rows': [{'name': domain_of(url), 'website': url, 'source': 'website'}]})
    locations = params.get('cities') or params.get('regions') or ['']
    for key in usable:
        if key in ADAPTERS:
            for location in locations[:100]:
                await enqueue(db, job.id, 'discover', key, {'query': ' '.join(p for p in [params.get('country', 'Россия'), location, params['query']] if p), 'cursor': 0})
    if not await db.scalar(select(CollectionTask.id).where(CollectionTask.job_id == job.id)):
        raise ValueError('Для выбранного источника загрузите файл компаний')
    if not params.get('parser_run'):
        setting = await db.get(CollectorSetting, 'last_params')
        if not setting: db.add(CollectorSetting(key='last_params', value=params))
        else: setting.value = params
    await db.flush()
    return job


async def claim_task(db):
    now = utcnow()
    # Recover a crashed process' unfinished lease, including a paused job.
    await db.execute(update(CollectionTask).where(CollectionTask.status == 'running', CollectionTask.lease_until < now)
        .values(status='pending', claim_token='', lease_until=None))
    task = await db.scalar(select(CollectionTask).join(CollectionJob).where(
        CollectionJob.status.in_(['queued', 'running']), CollectionTask.status.in_(['pending', 'retry']), CollectionTask.due_at <= now
    ).order_by(CollectionTask.id).limit(1).with_for_update(skip_locked=True))
    if not task:
        await db.commit()
        return None
    token = uuid.uuid4().hex
    # CAS also protects SQLite test runs and callers without row locks.
    changed = await db.scalar(update(CollectionTask).where(CollectionTask.id == task.id, CollectionTask.status.in_(['pending', 'retry']))
        .values(status='running', claim_token=token, lease_until=now + timedelta(minutes=5), attempts=CollectionTask.attempts + 1)
        .returning(CollectionTask.id).execution_options(synchronize_session=False))
    if not changed:
        await db.rollback()
        return None
    job = await db.get(CollectionJob, task.job_id)
    job.status = 'running'
    await db.commit()
    await db.refresh(task)
    return task


async def process_task(task):
    """Do bounded network work outside a held SQL transaction."""
    async with AsyncSessionLocal() as db:
        job = await db.get(CollectionJob, task.job_id)
        if not job or job.status not in {'queued', 'running'}: return
        params = job.params
        source = await db.get(CollectorSource, task.source)
        if task.kind != 'import': permissions(source)
        if task.kind == 'discover':
            source = await reserve_api_request(db, task.source)
        if task.kind == 'website':
            host = domain_of(task.payload['url'])
            throttle = await db.get(WebsiteThrottle, host)
            if throttle and throttle.next_request_at > utcnow():
                raise SourceError('Пауза между обращениями к сайту', retry_after=(throttle.next_request_at - utcnow()).total_seconds())
            if not throttle: db.add(WebsiteThrottle(host=host, next_request_at=utcnow() + timedelta(seconds=10)))
            else: throttle.next_request_at = utcnow() + timedelta(seconds=10)
        await db.commit()
    result = None
    if task.kind == 'discover':
        result = await asyncio.to_thread(ADAPTERS[task.source].search, source, task.payload['query'], task.payload['cursor'])
    elif task.kind == 'website':
        result = await asyncio.to_thread(extract_website, task.payload['url'], check_mx=params.get('check_mx', False))
    async with AsyncSessionLocal() as db:
        current = await db.get(CollectionTask, task.id, with_for_update=True)
        job = await db.get(CollectionJob, task.job_id, with_for_update=True)
        if not current or current.claim_token != task.claim_token: return
        # A pause/stop after a network request never commits its response.
        if job.status not in {'queued', 'running'}:
            current.status = 'pending' if job.status == 'paused' else 'cancelled'
            current.claim_token, current.lease_until = '', None
            await db.commit()
            return
        if task.kind == 'website':
            rights = permissions(await db.get(CollectorSource, 'website'))
            rights['basis'] = params.get('rights_basis') or rights['basis']
            if 'record' in task.payload:
                # Keep candidates in the durable task until website enrichment
                # finds an email. Empty/failed sites never create a Company.
                origin = task.payload['origin']
                origin_rights = permissions(await db.get(CollectorSource, origin))
                origin_rights['basis'] = params.get('rights_basis') or origin_rights['basis']
                company, duplicate = await save_company(db, task.payload['record'], source=origin,
                    rights=origin_rights, job_id=job.id, require_email=True,
                    enrichment={'contacts': result['contacts'], 'source': 'website', 'rights': rights})
                job.duplicates += int(duplicate)
                current.payload = {}
            else:
                company = await db.get(Company, task.payload['company_id'])
                if company:
                    await add_contacts(db, company.id, result['contacts'], source='website', rights=rights)
            job.sites_checked += 1
            if company:
                company.checked_at = utcnow()
        else:
            rows = result.records if result else task.payload['rows']
            for raw in rows:
                if job.processed >= params.get('limit', 100): break
                origin = task.source if task.kind == 'discover' else raw.get('source') or 'import'
                rights = permissions(await db.get(CollectorSource, origin))
                if origin in {'website', 'import', 'fns'} or origin.startswith('local_'): rights['basis'] = params.get('rights_basis') or rights['basis']
                job.processed += 1
                try:
                    async with db.begin_nested():
                        record = normalize_record(raw)
                        crawl = params.get('crawl_websites') or task.source == 'website'
                        if not await has_collectable_email(db, record['contacts']):
                            if crawl and record['website']:
                                await enqueue(db, job.id, 'website', 'website',
                                    {'url': record['website'], 'record': raw, 'origin': origin})
                            continue
                        company, duplicate = await save_company(db, raw, source=origin, rights=rights,
                            job_id=job.id, require_email=True)
                    job.duplicates += int(duplicate)
                    if company and (params.get('crawl_websites') or task.source == 'website') and company.website and not (duplicate and params.get('exclude_existing')):
                        await enqueue(db, job.id, 'website', 'website', {'company_id': company.id, 'url': company.website})
                except ValueError as error:
                    job.errors += 1
                    job.last_error = str(error)
            if result and result.next_cursor is not None and job.processed < params.get('limit', 100) and result.next_cursor < 10000:
                await enqueue(db, job.id, 'discover', task.source, {**task.payload, 'cursor': result.next_cursor})
        current.status, current.claim_token, current.lease_until = 'done', '', None
        current.error = ''
        # Do not retain duplicate raw contact files once their checkpoint is saved.
        if task.kind == 'import':
            current.payload = {}
        await db.commit()


async def fail_task(task, error):
    async with AsyncSessionLocal() as db:
        current = await db.get(CollectionTask, task.id, with_for_update=True)
        job = await db.get(CollectionJob, task.job_id, with_for_update=True)
        if not current or current.claim_token != task.claim_token: return
        message = str(error) if isinstance(error, (SourceError, ValueError)) else 'Внутренняя ошибка обработки; повторите запуск после проверки данных'
        current.error = message[:500]
        job.last_error = current.error
        job.errors += 1
        retry = isinstance(error, SourceError) and error.retry_after and current.attempts < 3
        current.status = 'retry' if retry else 'error'
        current.claim_token, current.lease_until = '', None
        if retry:
            current.due_at = utcnow() + timedelta(seconds=max(error.retry_after, 10 * 2 ** current.attempts))
        if task.kind == 'website' and isinstance(error, SourceError) and error.blocked:
            host = domain_of(task.payload['url'])
            throttle = await db.get(WebsiteThrottle, host)
            until = utcnow() + timedelta(seconds=error.retry_after or 3600)
            if not throttle: db.add(WebsiteThrottle(host=host, next_request_at=until))
            else: throttle.next_request_at = until
        # Budget/rights problems pause this source's work instead of burning retries.
        if isinstance(error, SourceError) and not error.retry_after and task.kind == 'discover':
            job.status, current.status = 'paused', 'pending'
        await db.commit()


async def finish_jobs():
    async with AsyncSessionLocal() as db:
        for job in (await db.scalars(select(CollectionJob).where(CollectionJob.status.in_(['queued', 'running'])))).all():
            remaining = await db.scalar(select(func.count()).select_from(CollectionTask).where(CollectionTask.job_id == job.id, CollectionTask.status.in_(['pending', 'retry', 'running'])))
            if not remaining:
                job.status = 'completed_with_errors' if job.errors else 'completed'
                job.finished_at = utcnow()
                # Reports are queued separately; never generated/sent implicitly.
                report = await db.get(CollectorSetting, 'report')
                job.report_status = 'pending' if report and report.value.get('enabled') and not job.params.get('parser_run') else 'disabled'
        await db.commit()


@background_work
async def run_collection_tick(max_tasks=4):
    from app.collector.local_parser import sync_finished_runs
    await sync_finished_runs()
    # Scheduler max_instances=1 plus DB claims keep reads independent from mail IO.
    for _ in range(max_tasks):
        async with AsyncSessionLocal() as db:
            task = await claim_task(db)
        if not task: break
        try:
            await process_task(task)
        except asyncio.CancelledError:
            raise  # Lease will recover after restart; no lost progress.
        except Exception as error:
            await fail_task(task, error)
    await finish_jobs()
    from app.collector.report_delivery import run_pending_reports
    await run_pending_reports()
