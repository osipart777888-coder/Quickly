import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

PARSER = Path(__file__).resolve().parents[1] / 'integrations/company_parser'
sys.path.insert(0, str(PARSER))
from company_emails import world
from company_emails.sources import build_tasks
from company_emails.storage import normalize_item


@pytest.fixture
def geography(tmp_path):
    country_data = b'UZ\tUZB\t860\tUZ\tUzbekistan\nKZ\tKAZ\t398\tKZ\tKazakhstan\nRU\tRUS\t643\tRS\tRussia\nUS\tUSA\t840\tUS\tUnited States\n'
    cities = []
    for identity, code, name, lat, lon, population in [(1, 'UZ', 'Tashkent', 41.3, 69.2, 2000000), (2, 'UZ', 'Samarkand', 39.6, 66.9, 500000), (3, 'KZ', 'Tashkent', 50, 70, 1000), (4, 'RU', 'Moscow', 55.7, 37.6, 10000000)]:
        row = [str(identity), name, name, '', str(lat), str(lon), 'P', 'PPLA', code, '', '', '', '', '', str(population), '', '', 'UTC', '2026-10-30']
        cities.append('\t'.join(row))
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive: archive.writestr('cities500.txt', '\n'.join(cities))
    world.build_database(tmp_path, country_data, data.getvalue())
    target = tmp_path / 'vendor/parser_2gis/data/cities.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps([{'code': 'tashkent', 'name': 'Ташкент', 'domain': 'uz', 'country_code': 'uz'}, {'code': 'moscow', 'name': 'Москва', 'domain': 'ru', 'country_code': 'ru'}]))
    return tmp_path


def test_country_plan_uses_all_catalogue_places_with_country_and_coordinates(geography):
    cities, coverage = world.resolve(geography, dict(source='yandex', scope='country', country_code='UZ', country='Узбекистан', cities=[]))
    assert [c['code'] for c in cities] == ['geo-1', 'geo-2']
    assert coverage['planned'] == 2 and coverage['country_code'] == 'UZ'
    tasks = build_tasks('yandex', cities, [], ['строительные компании'], 100)
    assert len(tasks) == 2
    assert all(task['country'] == 'Узбекистан' and task['country_code'] == 'uz' for task in tasks)
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(tasks[0]['url']).query)['ll'] == ['69.2,41.3']
    assert 'Узбекистан' in parse_qs(urlsplit(tasks[0]['url']).query)['text'][0]
    item = normalize_item({'schema': 'company-contact/v1', 'external_id': '123', 'name': 'Fixture', 'source_url': 'https://yandex.ru/maps/org/123'}, tasks[0])
    assert item['country'] == 'Узбекистан'


def test_2gis_uses_its_country_coverage_and_domain(geography):
    cities, coverage = world.resolve(geography, dict(source='2gis', scope='country', country_code='UZ', country='Узбекистан', cities=[]))
    task = build_tasks('2gis', cities, [], ['строительство'], 100)[0]
    assert task['url'].startswith('https://2gis.uz/tashkent/')
    item = normalize_item({'id': '100', 'name': 'Fixture'}, task)
    assert item['country'] == 'Узбекистан' and item['source_url'].startswith('https://2gis.uz/')
    with pytest.raises(ValueError, match='нет населённых пунктов'):
        world.resolve(geography, dict(source='2gis', scope='country', country_code='US', cities=[]))


def test_country_catalogue_does_not_substitute_russia(geography):
    rows = {row['code']: row for row in world.countries(geography, 'yandex')}
    assert rows['UZ']['source_cities'] == 2 and rows['KZ']['source_cities'] == 1
    with pytest.raises(ValueError, match='Неизвестный код страны'):
        world.resolve(geography, dict(source='yandex', scope='country', country_code='XX', cities=[]))
    cities, _ = world.resolve(geography, dict(source='yandex', scope='cities', country_code='KZ', cities=['Tashkent']))
    assert len(cities) == 1 and cities[0]['latitude'] == 50


def test_missing_catalogue_gives_actionable_error(tmp_path):
    with pytest.raises(ValueError, match='Мировой каталог не установлен'):
        world.resolve(tmp_path, dict(source='yandex', scope='country', country_code='UZ', cities=[]))


def test_bridge_builds_persistent_foreign_plan_before_launching_browser(geography, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('country_test_bridge', PARSER.parents[1] / 'scripts/local_parser_bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    python = geography / '.venv/bin/python'
    python.parent.mkdir(parents=True)
    python.touch()
    bridge = module.Bridge({'parser_root': str(geography)}, geography / 'runs')
    launched = []
    monkeypatch.setattr(bridge, 'launch', lambda path: launched.append(path))
    result = bridge.start(dict(source='yandex', scope='country', country_code='UZ', country='Узбекистан',
                              query='строительные компании', cities=[], rights_confirmed=True, rights_basis='Synthetic test'))
    plan = json.loads((launched[0] / 'plan.json').read_text())
    assert len(plan) == 2 and result['coverage']['planned'] == 2
    assert all(task['country_code'] == 'uz' for task in plan)


@pytest.mark.asyncio
async def test_same_named_companies_in_different_countries_do_not_merge(session):
    from app.collector.repository import save_company
    common = dict(name='Строительная компания', website='https://fixture.example', address='ул. Центральная, 1')
    first, _ = await save_company(session, dict(common, external_id='uz-1', country='Узбекистан'), source='local_yandex', rights={'store': True})
    second, _ = await save_company(session, dict(common, external_id='kz-1', country='Казахстан'), source='local_yandex', rights={'store': True})
    assert first.id != second.id and first.country == 'Узбекистан' and second.country == 'Казахстан'
