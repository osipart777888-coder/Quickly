"""Local parser integration, no Chrome or external mail/network required."""
import importlib.util
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import func, select
from app.collector import local_parser, queue
from app.collector.models import CollectionJob, CollectionTask, Company, CompanyContact, ContactPermission

SPEC = importlib.util.spec_from_file_location('parser_bridge', Path(__file__).resolve().parents[1] / 'scripts/local_parser_bridge.py')
bridge_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge_module)


def valid(**extra):
    return dict(source='yandex', name='Test', query='Юристы', cities=['Тула'], limit=3,
                rights_confirmed=True, rights_basis='Owned test records', **extra)


@pytest.mark.parametrize('patch', [dict(cities=[]), dict(limit=0), dict(limit=10001), dict(delay=.1),
    dict(rights_confirmed=False), dict(rights_basis=''), dict(source='other'), dict(cities='Москва')])
def test_request_validation(patch):
    with pytest.raises(ValueError): bridge_module.validate_request(valid() | patch)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://evil.ru', 'https://spark-interfax.ru.evil.ru',
    'https://user:password@spark-interfax.ru/', 'https://spark-interfax.ru/?access_token=secret'])
def test_selection_urls_are_restricted(url):
    with pytest.raises(ValueError): bridge_module.validate_request(valid() | {'source': 'spark', 'urls': [url]})


def make_database(path):
    path.mkdir(parents=True)
    db = sqlite3.connect(path / 'contacts.sqlite')
    db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE companies(id TEXT PRIMARY KEY, payload TEXT, updated TEXT);
        CREATE TABLE emails(company_id TEXT, email TEXT, source_url TEXT, kind TEXT, found_at TEXT);
        CREATE TABLE tasks(payload TEXT, status TEXT, count INTEGER, message TEXT);''')
    item = dict(id='yandex:123', source='yandex', name='Компания', city='Тула', websites=['https://fixture-company.ru'],
                source_url='https://yandex.ru/maps/org/123', rubrics=['Юристы'])
    db.execute('INSERT INTO companies VALUES(?,?,?)', ('yandex:123', json.dumps(item), '2026-10-28'))
    db.execute('INSERT INTO emails VALUES(?,?,?,?,?)', ('yandex:123', 'office@fixture-company.ru', 'https://fixture-company.ru/contacts', 'website', '2026-10-28'))
    db.execute('INSERT INTO tasks VALUES(?,?,?,?)', (json.dumps(dict(city='Тула', query='Юристы')), 'done', 1, ''))
    db.commit()
    return db


def test_snapshot_reads_wal_preserves_origins_and_does_not_modify_old_db(tmp_path):
    root = tmp_path / 'parser'
    db = make_database(root / 'runs' / 'old')
    bridge = bridge_module.Bridge({'parser_root': str(root)}, tmp_path / 'new-runs')
    run = bridge.runs()[0]
    assert run['saved'] and run['stats'] == {'companies': 1, 'emails': 1}
    a = bridge.snapshot(run['id'])
    assert a['rows'][0]['source'] == 'local_yandex'
    assert a['rows'][0]['email_sources']['office@fixture-company.ru'] == ['https://fixture-company.ru/contacts']
    assert a == bridge.snapshot(run['id'])
    assert db.execute('SELECT COUNT(*) FROM companies').fetchone()[0] == 1
    with pytest.raises(ValueError): bridge.path('../../etc/passwd')
    with pytest.raises(ValueError): bridge.action(run['id'], 'resume')
    db.close()


def test_bridge_requires_token_and_blocks_browser_origins(tmp_path):
    server = bridge_module.ThreadingHTTPServer(('127.0.0.1', 0), bridge_module.Handler)
    server.token = 'fixture-token'
    server.bridge = bridge_module.Bridge({'parser_root': str(tmp_path)}, tmp_path / 'runs')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url='http://127.0.0.1:' + str(server.server_port), trust_env=False) as client:
            assert client.get('/status').status_code == 401
            assert client.get('/status', headers={'Authorization': 'Bearer fixture-token', 'Origin': 'https://evil.test'}).status_code == 403
            assert client.get('/status', headers={'Authorization': 'Bearer fixture-token'}).json()['connected']
    finally: server.shutdown(); server.server_close(); thread.join()


@pytest.mark.asyncio
async def test_import_idempotent_origin_and_unknown_consent(session, monkeypatch):
    run_id = 'f' * 32
    row = dict(name='Компания', external_id='yandex:123', source='local_yandex', website='https://fixture-company.ru',
               emails=['office@fixture-company.ru'], email_sources={'office@fixture-company.ru': ['https://fixture-company.ru/contacts']},
               source_url='https://yandex.ru/maps/org/123')
    run = dict(id=run_id, name='Тест', source='yandex', status='completed', saved=False, checkpoint=1,
               request=dict(rights_confirmed=True, rights_basis='Test ownership', limit=3, crawl_websites=False))
    async def request(method, path, data=None):
        return {'rows': [row], 'version': 'v1'} if path.endswith('/snapshot') else [run]
    monkeypatch.setattr(local_parser, 'request', request)
    first = await local_parser.import_run(session, run_id)
    repeated = await local_parser.import_run(session, run_id)
    assert first['job_id'] == repeated['job_id'] and repeated['already_imported']
    assert await session.scalar(select(func.count()).select_from(CollectionJob)) == 1
    assert await session.scalar(select(func.count()).select_from(CollectionTask)) == 1
    task = await session.scalar(select(CollectionTask))
    await session.commit()
    # Exercise the actual queue with the shared fixture session factory.
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def factory(): yield session
    monkeypatch.setattr(queue, 'AsyncSessionLocal', factory)
    task = await queue.claim_task(session)
    await queue.process_task(task)
    assert await session.scalar(select(func.count()).select_from(Company)) == 1
    contact = await session.scalar(select(CompanyContact))
    assert contact.origins[0]['url'] == 'https://fixture-company.ru/contacts'
    assert contact.origins[0]['source'] == 'local_yandex'
    assert (await session.get(ContactPermission, contact.value)).status == 'unknown'
    progress = await local_parser.email_progress(session, [first['job_id']])
    assert progress[first['job_id']]['unique_emails'] == 1
    assert progress[first['job_id']]['with_email'] == 1
    session.add(CollectionTask(job_id=first['job_id'], task_key='website-fixture', kind='website', source='website',
                               payload={'company_id': contact.company_id}, status='pending'))
    await session.flush()
    progress = await local_parser.email_progress(session, [first['job_id']])
    assert progress[first['job_id']]['sites_pending'] == 1
    await session.delete(await session.scalar(select(CollectionTask).where(CollectionTask.task_key == 'website-fixture')))
    await session.flush()
    from app.collector.reports import export_bytes
    raw, omitted = await export_bytes(session, list((await session.scalars(select(Company))).all()), 'csv')
    assert not omitted and b'office@fixture-company.ru' in raw
    from app.collector.models import CollectorSetting
    assert not await session.get(CollectorSetting, 'last_params')
    session.add(CollectorSetting(key='report', value={'enabled': True}))
    await session.flush()
    await queue.finish_jobs()
    assert (await session.get(CollectionJob, first['job_id'])).report_status == 'disabled'


@pytest.mark.asyncio
async def test_saved_import_requires_explicit_rights_and_running_cannot_import(session, monkeypatch):
    run = dict(id='saved-' + 'a'*20, name='Old', source='yandex', status='saved', saved=True, request={})
    async def request(*args, **kwargs): return [run]
    monkeypatch.setattr(local_parser, 'request', request)
    with pytest.raises(ValueError, match='основание'): await local_parser.import_run(session, run['id'])
    run['status'] = 'running'
    with pytest.raises(ValueError, match='паузу'): await local_parser.import_run(session, run['id'])


def test_connection_file_is_host_only_and_never_exposes_credentials(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    monkeypatch.setenv('QUICKLY_PARSER_CONNECTION_FILE', str(path))
    path.write_text(json.dumps(dict(base_url='http://evil.test', api_key='private')))
    with pytest.raises(Exception, match='настройки'): local_parser.connection()
    path.write_text(json.dumps(dict(base_url='http://host.docker.internal:8877', api_key='private')))
    assert local_parser.connection()[0] == 'http://host.docker.internal:8877'


def test_worker_global_limit_and_pause_resume(tmp_path, monkeypatch):
    """Two cities share one budget; resume consumes only the remaining budget."""
    import sys
    parser_root = Path(__file__).resolve().parents[1] / 'integrations/company_parser'
    monkeypatch.syspath_prepend(str(parser_root))
    # The Docker backend intentionally has no host-process dependencies.
    # Only lock PID identity is needed by this in-process runner unit test.
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(Process=lambda *_: SimpleNamespace(create_time=lambda: 1), Error=RuntimeError))
    from company_emails import runner, exporting, signals
    from company_emails.storage import Store
    worker_spec = importlib.util.spec_from_file_location('quickly_parser_worker', parser_root.parents[1] / 'scripts/local_parser_worker.py')
    worker = importlib.util.module_from_spec(worker_spec)
    worker_spec.loader.exec_module(worker)
    directory = tmp_path / 'run'
    directory.mkdir()
    data = valid() | {'cities': ['Тула', 'Рязань'], 'timeout': 30, 'visible': False, 'delay': 1}
    (directory / 'request.json').write_text(json.dumps(data))
    monkeypatch.setattr(signals, 'install_shutdown_handlers', lambda: None)
    monkeypatch.setattr(exporting, 'export_results', lambda *a: None)
    calls = []
    def collect(store, tasks, **options):
        task = tasks[0]
        calls.append(task['limit'])
        store.add_tasks(tasks)
        count = 1 if len(calls) == 1 else task['limit']
        for index in range(count):
            identity = 'company-' + str(index)
            store.db.execute('INSERT OR IGNORE INTO companies VALUES(?,?,?)', (identity, json.dumps(dict(id=identity, websites=[])), '2026-10-28'))
            store.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)', (identity, task['id'], task['query'], task['city']))
        store.db.commit()
        store.mark(task['id'], 'interrupted' if len(calls) == 1 else 'limited', count)
        return len(calls) != 1
    monkeypatch.setattr(runner, 'collect', collect)
    worker.run(parser_root, directory)
    assert json.loads((directory / 'worker-result.json').read_text())['status'] == 'paused'
    worker.run(parser_root, directory)
    assert json.loads((directory / 'worker-result.json').read_text())['status'] == 'completed'
    store = Store(directory)
    try: assert store.summary()['companies'] == data['limit']
    finally: store.close()
    assert calls == [3, 3]  # already saved row is included in the resumed task cap
