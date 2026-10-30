"""Run the existing parser with one global limit and durable pause/resume."""
import argparse
import json
import sys
from pathlib import Path


def run(parser_root, directory):
    sys.path.insert(0, str(parser_root))
    from company_emails.catalog import load_cities
    from company_emails.geography import yandex_cities, select_yandex_cities
    from company_emails.sources import build_tasks, LOCAL
    from company_emails.storage import Store
    from company_emails.runner import collect, run_lock
    from company_emails.exporting import export_results
    from company_emails.checkpoints import atomic_json
    from company_emails.signals import install_shutdown_handlers
    install_shutdown_handlers()
    request = json.loads((directory / 'request.json').read_text())
    source = request['source']
    result = {'status': 'error', 'message': 'Сбор не завершён'}
    store = None
    try:
        with run_lock(directory):
            if (directory / 'plan.json').exists():
                tasks = json.loads((directory / 'plan.json').read_text())
            else:
                cities = []
                if source in {'yandex', '2gis'} and (request.get('scope') == 'country' or request.get('country_code', 'RU') != 'RU'):
                    from company_emails.world import resolve
                    cities, coverage = resolve(parser_root, request)
                    atomic_json(directory / 'geography.json', coverage)
                elif source == 'yandex':
                    cities = select_yandex_cities(yandex_cities(), request['cities'])
                elif source == '2gis':
                    catalogue = load_cities()
                    for name in request['cities']:
                        matches = [c for c in catalogue if name.casefold() in (c['name'].casefold(), c['code'].casefold())]
                        if not matches:
                            raise ValueError('Город не найден в 2ГИС: ' + name)
                        cities.extend(c for c in matches if c not in cities)
                tasks = build_tasks(source, cities, request.get('rubrics', []),
                    [request['query']] if source in {'2gis', 'yandex'} and request['query'] else [], request['limit'],
                    urls=request.get('urls') or None, selection_name=request.get('name'))
                atomic_json(directory / 'plan.json', tasks)
            store = Store(directory)
            store.add_tasks(tasks)
            result = {'status': 'completed', 'message': ''}
    except (ValueError, OSError) as error:
        result = {'status': 'error', 'message': str(error)[:500]}
        atomic_json(directory / 'worker-result.json', result)
        if store: store.close()
        return
    try:
        with run_lock(directory):
            if source in {'spark', 'rusprofile'}:
                from company_emails.spark_browser import SparkBrowser
                from company_emails.rusprofile_browser import RusprofileBrowser
                factory = SparkBrowser if source == 'spark' else RusprofileBrowser
                with run_lock(LOCAL / 'profiles' / source):
                    with factory(): pass
            for task in tasks:
                count = store.summary()['companies']
                if count >= request['limit']:
                    result = {'status': 'limited', 'message': 'Достигнут общий лимит карточек за запуск. Непроверенные города остаются в плане; полный обход страны не завершён.'}
                    break
                task = dict(task, limit=request['limit'] - count + store.task_count(task['id']))
                ok = collect(store, [task], timeout=request['timeout'], visible=request['visible'],
                             delay=request['delay'], retry_failed=True)
                if not ok:
                    states = store.summary()['tasks']
                    if request.get('scope') == 'country' and not states.get('needs_auth') and not states.get('interrupted'):
                        result = {'status': 'completed_with_errors', 'message': 'Часть городов недоступна; подробности сохранены в истории.'}
                        continue
                    result = {'status': 'needs_auth' if states.get('needs_auth') else 'paused' if states.get('interrupted') else 'error',
                              'message': next((r[0] for r in store.db.execute("SELECT message FROM tasks WHERE message != '' ORDER BY updated DESC LIMIT 1")), 'Сбор приостановлен')}
                    break
            if result['status'] == 'completed' and any(store.summary()['tasks'].get(s) for s in ['partial', 'error', 'timeout']):
                result['status'] = 'completed_with_errors'
            if result['status'] == 'completed' and store.summary()['tasks'].get('limited'):
                result = {'status': 'limited', 'message': 'Достигнут лимит карточек. Доступная выдача карт может быть больше собранной.'}
    except KeyboardInterrupt:
        result = {'status': 'paused', 'message': 'Остановлено пользователем, прогресс сохранён'}
    except Exception as error:
        result = {'status': 'error', 'message': str(error)[:500]}
    finally:
        if store:
            try: export_results(store, directory / 'export')
            finally: store.close()
        atomic_json(directory / 'worker-result.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--parser-root', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    run(args.parser_root.resolve(), args.run_dir.resolve())
