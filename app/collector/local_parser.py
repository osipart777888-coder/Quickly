"""Narrow bridge client and idempotent conversion into the existing import queue."""
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from app.collector.models import CollectionJob, CollectionResult, CollectionTask, CompanyContact
from app.collector.websites import SourceError

LOCAL_SOURCES = {'local_yandex', 'local_2gis', 'local_spark', 'local_rusprofile'}


async def email_progress(db, job_ids):
    """Show persisted website/enrichment results, not just browser-card counts."""
    if not job_ids: return {}
    result = {job_id: {'companies': 0, 'unique_emails': 0, 'with_email': 0, 'websites': {}} for job_id in job_ids}
    query = select(CollectionResult.job_id,
        func.count(func.distinct(CollectionResult.company_id)),
        func.count(func.distinct(CompanyContact.value)).filter(CompanyContact.kind == 'email'),
        func.count(func.distinct(CompanyContact.company_id)).filter(CompanyContact.kind == 'email'))
    query = query.outerjoin(CompanyContact, CompanyContact.company_id == CollectionResult.company_id).where(
        CollectionResult.job_id.in_(job_ids)).group_by(CollectionResult.job_id)
    for job_id, companies, emails, with_email in (await db.execute(query)).all():
        result[job_id].update(companies=companies, unique_emails=emails, with_email=with_email)
    for job_id, status, count in (await db.execute(select(CollectionTask.job_id, CollectionTask.status, func.count())
            .where(CollectionTask.job_id.in_(job_ids), CollectionTask.kind == 'website')
            .group_by(CollectionTask.job_id, CollectionTask.status))).all():
        result[job_id]['websites'][status] = count
    for item in result.values():
        item['sites_pending'] = sum(item['websites'].get(s, 0) for s in ['pending', 'running', 'retry'])
        item['sites_checked'] = item['websites'].get('done', 0)
        item['sites_failed'] = item['websites'].get('error', 0)
    return result


def connection():
    path = os.environ.get('QUICKLY_PARSER_CONNECTION_FILE', '')
    if not path: raise SourceError('Локальный парсер не подключён на этом компьютере')
    try:
        config = json.loads(Path(path).read_text())
        parts = urlsplit(config['base_url'])
        if parts.scheme != 'http' or parts.hostname not in {'host.docker.internal', '127.0.0.1', 'localhost'} or not config.get('api_key'):
            raise ValueError()
        return config['base_url'].rstrip('/'), config['api_key']
    except (OSError, ValueError, KeyError): raise SourceError('Проверьте локальные настройки соединения с парсером')


async def request(method, path, data=None):
    base, token = connection()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=3), trust_env=False) as client:
            response = await client.request(method, base + path, json=data, headers={'Authorization': 'Bearer ' + token})
        if response.status_code >= 400:
            detail = response.json().get('detail', 'Ошибка локального парсера')
            raise SourceError(str(detail)[:500])
        return response.json()
    except (httpx.HTTPError, ValueError): raise SourceError('Нет связи с парсером на Mac. Запустите Quickly повторно и нажмите «Обновить»')


def run_path(run_id):
    if not re.fullmatch(r'(?:[a-f0-9]{32}|saved-[a-f0-9]{20})', run_id): raise ValueError('Некорректный запуск')
    return '/runs/' + run_id


async def import_run(db, run_id, *, rights_basis=None, rights_confirmed=False):
    from app.collector.queue import create_job
    runs = await request('GET', '/runs')
    run = next((r for r in runs if r['id'] == run_id), None)
    if not run: raise ValueError('Запуск не найден')
    if run['status'] == 'running': raise ValueError('Сначала дождитесь завершения или поставьте сбор на паузу')
    params = run.get('request', {})
    if run.get('saved'):
        if not rights_confirmed or not (rights_basis or '').strip(): raise ValueError('Укажите основание импорта старой выгрузки')
        params = dict(params, rights_confirmed=True, rights_basis=rights_basis, limit=10000, crawl_websites=True)
    snapshot = await request('GET', run_path(run_id) + '/snapshot')
    rows = snapshot['rows']
    if not rows: return {'job_id': None, 'rows': 0}
    job_id = hashlib.sha256(('local-parser:' + run_id + ':' + snapshot['version']).encode()).hexdigest()[:32]
    existing = await db.get(CollectionJob, job_id)
    if existing:
        existing.params = {**existing.params, 'parser_checkpoint': run.get('checkpoint', 0)}
        await db.flush()
        return {'job_id': existing.id, 'rows': len(rows), 'already_imported': True}
    params = {**params, 'only_email': True, 'name': run['name'], 'sources': sorted({r['source'] for r in rows}), 'websites': [],
              'check_mx': False, 'exclude_existing': False, 'parser_run': run_id, 'parser_version': snapshot['version'], 'parser_checkpoint': run.get('checkpoint', 0)}
    async with db.begin_nested():
        job = await create_job(db, params, records=rows, job_id=job_id)
    return {'job_id': job.id, 'rows': len(rows)}


async def sync_finished_runs():
    """Automatic import survives closing the page and restarting Quickly."""
    if not os.environ.get('QUICKLY_PARSER_CONNECTION_FILE'): return
    from app.database import AsyncSessionLocal
    try:
        runs = await request('GET', '/runs')
        for run in runs:
            if run.get('saved') or run['status'] == 'running' or not run['stats']['companies']: continue
            # Each version's deterministic job ID also serializes concurrent imports.
            async with AsyncSessionLocal() as db:
                try:
                    previous = (await db.scalars(select(CollectionJob).where(CollectionJob.params['parser_run'].as_string() == run['id']))).all()
                    if any(j.params.get('parser_checkpoint') == run.get('checkpoint', 0) for j in previous): continue
                    await import_run(db, run['id'])
                    await db.commit()
                except (SourceError, ValueError, IntegrityError): await db.rollback()
    except SourceError:
        return  # UI reports bridge errors; collector and all mail work continue.
