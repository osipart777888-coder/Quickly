"""Authenticated loopback bridge to the Mac's parser/Chrome, never to SMTP.

No arbitrary commands, paths or remote URLs can be executed through this API.
Saved parser databases are read-only; each Quickly launch has a separate folder.
"""
import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SOURCES = {'yandex': 'Яндекс Карты', '2gis': '2ГИС', 'spark': 'СПАРК', 'rusprofile': 'Rusprofile'}
MAX_BODY = 65536


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as output:
        json.dump(value, output, ensure_ascii=False)
    temp.chmod(0o600)
    temp.replace(path)


def read_json(path, default=None):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return default


def validate_request(data):
    if not isinstance(data, dict) or data.get('source') not in SOURCES:
        raise ValueError('Выберите источник')
    if data.get('rights_confirmed') is not True or not str(data.get('rights_basis', '')).strip():
        raise ValueError('Подтвердите основание обработки данных')
    result = {key: str(data.get(key, '')).strip()[:size] for key, size in [('name', 255), ('query', 512), ('rights_basis', 2000)]}
    result.update(source=data['source'], rights_confirmed=True)
    for key, maximum in [('cities', 30), ('urls', 20), ('rubrics', 10)]:
        values = data.get(key, [])
        if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, str) or len(v) > 2048 for v in values):
            raise ValueError('Некорректный список: ' + key)
        result[key] = list(dict.fromkeys(v.strip() for v in values if v.strip()))
    for key, default, low, high in [('limit', 100, 1, 10000), ('timeout', 900, 30, 3600), ('delay', 1, .5, 60)]:
        try: value = float(data.get(key, default))
        except (ValueError, TypeError): raise ValueError('Некорректный параметр: ' + key)
        if not low <= value <= high: raise ValueError('Параметр вне диапазона: ' + key)
        result[key] = value if key == 'delay' else int(value)
    result['visible'] = data.get('visible', False) is True
    result['crawl_websites'] = data.get('crawl_websites', True) is True
    if result['source'] in {'yandex', '2gis'}:
        if not result['cities']: raise ValueError('Укажите хотя бы один город')
        if not result['query'] and not (result['source'] == '2gis' and result['rubrics']):
            raise ValueError('Укажите поисковую фразу или рубрику 2ГИС')
    else:
        allowed = ('spark-interfax.ru', 'spark-interfax.com') if result['source'] == 'spark' else ('rusprofile.ru',)
        for url in result['urls']:
            parts = urlsplit(url)
            if parts.scheme != 'https' or parts.username or parts.password or not any((parts.hostname or '') == d or (parts.hostname or '').endswith('.' + d) for d in allowed):
                raise ValueError('Нужна ссылка выборки выбранного источника')
            if any(k.casefold() in {'password', 'passwd', 'token', 'access_token', 'id_token', 'authorization', 'otp'} for k in parse_qs(parts.query)):
                raise ValueError('Не передавайте данные авторизации в ссылке')
    return result


class Bridge:
    def __init__(self, config, directory):
        self.root = Path(config['parser_root']).resolve()
        self.python = self.root / '.venv/bin/python'
        self.worker = Path(__file__).with_name('local_parser_worker.py').resolve()
        self.directory = directory
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.processes = {}

    def saved(self):
        runs = self.root / 'runs'
        if not runs.is_dir(): return {}
        # A fixed root, never a path supplied by the browser.
        return {'saved-' + hashlib.sha256(str(p.relative_to(runs)).encode()).hexdigest()[:20]: p.parent
                for p in sorted(runs.glob('*/contacts.sqlite'))[:200] if p.resolve().is_relative_to(runs.resolve())}

    def path(self, run_id):
        if re.fullmatch(r'[a-f0-9]{32}', run_id) and (self.directory / run_id / 'request.json').is_file():
            return self.directory / run_id
        path = self.saved().get(run_id)
        if not path: raise ValueError('Запуск не найден')
        return path

    def alive(self, directory):
        child = self.processes.get(directory.name)
        if child is not None and child.poll() is not None:
            self.processes.pop(directory.name, None)
            return False
        meta = read_json(directory / 'process.json', {})
        try:
            import psutil
            process = psutil.Process(meta['pid'])
            args = process.cmdline()
            return (abs(process.create_time() - meta['created']) < .1 and str(self.worker) in args
                    and str(directory) in args and process.status() != psutil.STATUS_ZOMBIE)
        except (OSError, KeyError, ImportError): return False
        except psutil.Error: return False

    def snapshot(self, run_id):
        directory = self.path(run_id)
        path = directory / 'contacts.sqlite'
        if not path.exists(): return {'rows': [], 'version': 'empty'}
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            rows = []
            db.execute('BEGIN')  # one consistent snapshot including email origins
            for identity, payload in db.execute('SELECT id,payload FROM companies ORDER BY id LIMIT 10000'):
                item = json.loads(payload)
                emails = {}
                for email, url in db.execute('SELECT email,source_url FROM emails WHERE company_id=? ORDER BY email,source_url', (identity,)):
                    emails.setdefault(email, []).append(url)
                row = {'external_id': identity, 'source': 'local_' + item.get('source', '2gis'),
                       'name': item.get('name', ''), 'inn': item.get('inn', ''), 'city': item.get('city', ''),
                       'region': item.get('region', ''), 'address': item.get('address', ''), 'country': 'Россия',
                       'website': next(iter(item.get('websites') or []), ''), 'phones': item.get('phones', []),
                       'emails': sorted(emails), 'email_sources': emails, 'source_url': item.get('source_url', ''),
                       'industry': '; '.join(item.get('rubrics', []))}
                if row['source'] not in {'local_' + key for key in SOURCES}: continue
                rows.append(row)
            return {'rows': rows, 'version': hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        finally: db.close()

    def describe(self, run_id):
        directory = self.path(run_id)
        request = read_json(directory / 'request.json', {}) if not run_id.startswith('saved-') else {}
        result = read_json(directory / 'worker-result.json', {})
        active = self.alive(directory) if request else False
        state = 'running' if active else result.get('status', 'interrupted' if request else 'saved')
        stats, tasks, message = {'companies': 0, 'emails': 0}, [], result.get('message', '')
        path = directory / 'contacts.sqlite'
        if path.exists():
            db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
            try:
                stats = {'companies': db.execute('SELECT COUNT(*) FROM companies').fetchone()[0],
                         'emails': db.execute('SELECT COUNT(DISTINCT email) FROM emails').fetchone()[0]}
                tasks = [{'city': json.loads(p).get('city', ''), 'query': json.loads(p).get('query', ''), 'status': s, 'count': c, 'message': m[:500]}
                         for p, s, c, m in db.execute('SELECT payload,status,count,message FROM tasks ORDER BY rowid LIMIT 100')]
            finally: db.close()
        if not message and not active:
            message = next((t['message'] for t in reversed(tasks) if t['message']), '')
        return {'id': run_id, 'name': request.get('name') or directory.name, 'source': request.get('source') or (next(iter(read_json(directory / 'plan.json', [{}])), {}).get('source', '2gis')),
                'status': state, 'message': message[:500], 'stats': stats, 'tasks': tasks,
                'checkpoint': (directory / 'worker-result.json').stat().st_mtime_ns if (directory / 'worker-result.json').exists() else 0,
                'request': request, 'saved': run_id.startswith('saved-'), 'created': directory.stat().st_mtime}

    def runs(self):
        ids = [p.name for p in self.directory.iterdir() if re.fullmatch(r'[a-f0-9]{32}', p.name) and (p / 'request.json').exists()]
        ids += list(self.saved())
        rows = []
        for run_id in ids:
            try: rows.append(self.describe(run_id))
            except (ValueError, OSError, sqlite3.Error): continue
        return sorted(rows, key=lambda r: r['created'], reverse=True)[:200]

    def start(self, data):
        request = validate_request(data)
        if not self.python.is_file(): raise ValueError('Среда парсера не установлена')
        # Validate the plan before launching Chrome; do not change the old parser.
        import sys
        if str(self.root) not in sys.path: sys.path.insert(0, str(self.root))
        from company_emails.catalog import load_cities
        from company_emails.sources import build_tasks
        if request['source'] == '2gis':
            catalogue = load_cities()
            cities = []
            for name in request['cities']:
                matches = [c for c in catalogue if name.casefold() in (c['name'].casefold(), c['code'].casefold())]
                if not matches: raise ValueError('Нет города в каталоге 2ГИС: ' + name)
                cities.extend(matches)
            build_tasks('2gis', cities, request['rubrics'], [request['query']] if request['query'] else [], request['limit'])
        elif request['source'] in {'spark', 'rusprofile'}:
            build_tasks(request['source'], [], [], [], request['limit'], urls=request['urls'] or None)
        with self.lock:
            if any(self.alive(p) for p in self.directory.iterdir() if p.is_dir()):
                raise ValueError('Один сбор уже запущен. Дождитесь завершения или поставьте его на паузу')
            run_id = uuid.uuid4().hex
            directory = self.directory / run_id
            write_json(directory / 'request.json', request)
            self.launch(directory)
            return self.describe(run_id)

    def launch(self, directory):
        import psutil
        (directory / 'worker-result.json').unlink(missing_ok=True)
        with (directory / 'worker.log').open('a') as output:
            process = subprocess.Popen([str(self.python), '-u', str(self.worker), '--parser-root', str(self.root), '--run-dir', str(directory)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=output, cwd=self.root, start_new_session=True)
        self.processes[directory.name] = process
        write_json(directory / 'process.json', {'pid': process.pid, 'created': psutil.Process(process.pid).create_time()})
        (directory / 'worker.log').chmod(0o600)

    def open_access(self, source):
        if source not in {'spark', 'rusprofile'}: raise ValueError('Вход в браузере доступен для СПАРК и Rusprofile')
        with self.lock:
            if any(self.alive(p) for p in self.directory.iterdir() if p.is_dir()): raise ValueError('Сначала поставьте сбор на паузу')
            script = Path(__file__).with_name('local_parser_access.py')
            output = self.root / '.local/quickly-access-result.json'
            write_json(output, {'status': 'starting', 'message': 'Открывается отдельное окно Chrome. Войдите самостоятельно, затем вставьте ссылку выборки в Quickly.'})
            with (self.directory / 'access.log').open('a') as log:
                subprocess.Popen([str(self.python), str(script), '--parser-root', str(self.root), '--source', source],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=self.root, start_new_session=True)
            return {'ok': True}

    def action(self, run_id, action):
        with self.lock:
            directory = self.path(run_id)
            if run_id.startswith('saved-'): raise ValueError('Старый запуск доступен только для чтения и импорта')
            active = self.alive(directory)
            if action == 'pause':
                if not active: raise ValueError('Этот запуск уже остановлен')
                os.kill(read_json(directory / 'process.json')['pid'], signal.SIGINT)
            elif action == 'resume':
                if any(self.alive(p) for p in self.directory.iterdir() if p.is_dir()): raise ValueError('Сначала остановите текущий сбор')
                self.launch(directory)
            else: raise ValueError('Неизвестное действие')
            return self.describe(run_id)

    def catalog(self, source, query, kind):
        if source not in {'yandex', '2gis'}: raise ValueError('Каталог доступен для карт')
        path = self.root / ('data/yandex-cities.json' if source == 'yandex' else 'vendor/parser_2gis/data/cities.json')
        if kind == 'rubrics':
            rubrics = read_json(self.root / 'vendor/parser_2gis/data/rubrics.json', {})
            return [{'code': code, 'name': value.get('label')} for code, value in rubrics.items()
                    if value.get('label') and not value.get('children') and query.casefold() in value['label'].casefold()][:100]
        values = read_json(path, [])
        if isinstance(values, dict): values = values.get('cities', [])
        return [{'code': c['code'], 'name': c['name']} for c in values if c.get('country_code') == 'ru' and c['code'] != 'abkhazia'
                and query.casefold() in (c['name'] + ' ' + c['code']).casefold()][:1200]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # Never log headers or returned contacts.

    def respond(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token):
            self.respond(401, {'detail': 'Нет доступа'}); return
        # No browser-origin requests, even with a leaked cross-origin response.
        if self.headers.get('Origin'):
            self.respond(403, {'detail': 'Доступ только через Quickly'}); return
        route = urlsplit(self.path)
        parts = route.path.strip('/').split('/')
        bridge = self.server.bridge
        try:
            if self.command == 'GET':
                if parts == ['status']:
                    result = {'connected': True, 'sources': SOURCES, 'parser_available': bridge.python.is_file(), 'version': 1,
                              'access': read_json(bridge.root / '.local/quickly-access-result.json', {})}
                elif parts == ['runs']: result = bridge.runs()
                elif parts == ['catalog']:
                    query = parse_qs(route.query)
                    result = bridge.catalog(query.get('source', ['yandex'])[0], query.get('q', [''])[0][:200], query.get('kind', ['cities'])[0])
                elif len(parts) == 3 and parts[0] == 'runs' and parts[2] == 'snapshot': result = bridge.snapshot(parts[1])
                else: self.respond(404, {'detail': 'Не найдено'}); return
            else:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY: raise ValueError('Некорректный размер запроса')
                data = json.loads(self.rfile.read(length))
                if parts == ['runs']: result = bridge.start(data)
                elif len(parts) == 2 and parts[0] == 'access': result = bridge.open_access(parts[1])
                elif len(parts) == 3 and parts[0] == 'runs': result = bridge.action(parts[1], parts[2])
                else: self.respond(404, {'detail': 'Не найдено'}); return
            self.respond(200, result)
        except (ValueError, TypeError, KeyError) as error: self.respond(400, {'detail': str(error)[:500]})
        except (OSError, sqlite3.Error): self.respond(503, {'detail': 'Локальный парсер временно недоступен; повторите обновление'})

    do_GET = dispatch
    do_POST = dispatch


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    server = ThreadingHTTPServer(('127.0.0.1', int(config['port'])), Handler)
    server.token = config['api_key']
    server.bridge = Bridge(config, args.config.parent / 'parser-runs')
    server.serve_forever()
