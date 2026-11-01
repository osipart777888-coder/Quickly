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
    for identity, code, name, lat, lon, population in [(1, 'UZ', 'Tashkent', 41.3, 69.2, 2000000), (2, 'UZ', 'Samarkand', 39.6, 66.9, 500000), (3, 'KZ', 'Tashkent', 50, 70, 1000), (4, 'RU', 'Moscow', 55.7, 37.6, 10000000), (5, 'RU', 'Екатеринбург', 56.85, 60.61, 1500000), (6, 'RU', 'Нижний Тагил', 57.9, 59.9, 300000), (7, 'RU', 'Челябинск', 55.1, 61.4, 1000000), (8, 'RU', 'Малое село', 57.1, 60.5, 600)]:
        row = [str(identity), name, name, '', str(lat), str(lon), 'P', 'PPLA', code, '', '', '', '', '', str(population), '', '', 'UTC', '2026-10-30']
        row[10] = {1: '01', 2: '02', 3: '01', 4: '48', 5: '71', 6: '71', 7: '13', 8: '71'}[identity]
        cities.append('\t'.join(row))
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive: archive.writestr('cities500.txt', '\n'.join(cities))
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/region-names-ru.json').write_text(json.dumps({'RU.71': 'Свердловская область', 'RU.13': 'Челябинская область'}))
    world.build_database(tmp_path, country_data, data.getvalue(), b'RU.71\tSverdlovsk\tSverdlovsk\t100\nRU.13\tChelyabinsk\tChelyabinsk\t101\nRU.48\tMoscow\tMoscow\t102\nUZ.01\tTashkent\tTashkent\t103\nUZ.02\tSamarkand\tSamarkand\t104\nKZ.01\tRegion\tRegion\t105\nUS.CA\tCalifornia\tCalifornia\t106\n')
    target = tmp_path / 'vendor/parser_2gis/data/cities.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps([{'code': 'tashkent', 'name': 'Ташкент', 'domain': 'uz', 'country_code': 'uz'}, {'code': 'moscow', 'name': 'Москва', 'domain': 'ru', 'country_code': 'ru'}, {'code': 'ekaterinburg', 'name': 'Екатеринбург', 'domain': 'ru', 'country_code': 'ru'}, {'code': 'ntagil', 'name': 'Нижний Тагил', 'domain': 'ru', 'country_code': 'ru'}, {'code': 'chelyabinsk', 'name': 'Челябинск', 'domain': 'ru', 'country_code': 'ru'}]))
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


def test_failed_geography_rebuild_keeps_existing_cache(geography):
    path = geography / 'data/world-geography.sqlite'
    original = path.read_bytes()
    with pytest.raises(zipfile.BadZipFile): world.build_database(geography, b'', b'invalid zip')
    assert path.read_bytes() == original


def test_region_plan_contains_smaller_places_and_excludes_neighboring_region(geography):
    request = dict(source='yandex', scope='regions', country_code='RU', country='Россия', region_codes=['RU.71'], cities=[])
    cities, coverage = world.resolve(geography, request)
    assert {c['name'] for c in cities} == {'Екатеринбург', 'Нижний Тагил', 'Малое село'}
    assert coverage['planned'] == 3 and coverage['regions'] == ['Свердловская область']
    tasks = build_tasks('yandex', cities, [], ['строительные компании'], 100)
    assert all(t['region'] == 'Свердловская область' and 'll=' in t['url'] for t in tasks)
    item = normalize_item(dict(schema='company-contact/v1', external_id='test', name='Fixture', source_url='https://yandex.ru/maps/org/123'), tasks[0])
    assert item['region'] == 'Свердловская область'
    more, _ = world.resolve(geography, dict(request, region_codes=['RU.71', 'RU.13']))
    assert len(more) == 4 and len({c['code'] for c in more}) == 4


def test_region_catalogue_counts_only_provider_coverage(geography):
    rows = {r['code']: r for r in world.regions(geography, '2gis', 'RU')}
    assert rows['RU.71']['source_cities'] == 2 and rows['RU.71']['cities'] == 3
    cities, _ = world.resolve(geography, dict(source='2gis', scope='regions', country_code='RU', region_codes=['RU.71']))
    tasks = build_tasks('2gis', cities, [], ['юристы'], 10)
    assert {t['city_code'] for t in tasks} == {'ekaterinburg', 'ntagil'}
    assert all(t['region'] == 'Свердловская область' for t in tasks)


def test_ambiguous_provider_city_name_is_not_assigned_to_a_region(geography):
    import sqlite3
    with sqlite3.connect(geography / 'data/world-geography.sqlite') as db:
        for identity, region in [(50, '71'), (51, '13')]:
            db.execute('INSERT INTO cities VALUES(?,?,?,?,?,?,?,?,?)', (identity, 'RU', 'Тёзка', 'Tezka', 55, 60, 1000, region, ''))
    path = geography / 'vendor/parser_2gis/data/cities.json'
    path.write_text(json.dumps(json.loads(path.read_text()) + [dict(code='unknown', name='Тёзка', country_code='ru', domain='ru')]))
    assert next(c for c in world.map_cities(geography, '2gis', 'RU') if c['code'] == 'unknown')['region_code'] == ''


@pytest.mark.parametrize('codes', [[], ['RU.999'], ['KZ.01']])
def test_unknown_or_foreign_region_cannot_expand_to_whole_country(geography, codes):
    with pytest.raises(ValueError, match='область / регион'):
        world.resolve(geography, dict(source='yandex', scope='regions', country_code='RU', region_codes=codes))


def test_uncovered_region_does_not_fall_back_to_another_place(geography):
    with pytest.raises(ValueError, match='нет городов'):
        world.resolve(geography, dict(source='2gis', scope='regions', country_code='US', region_codes=['US.CA']))
    cities, _ = world.resolve(geography, dict(source='yandex', scope='regions', country_code='KZ', region_codes=['KZ.01']))
    assert len(cities) == 1 and cities[0]['latitude'] == 50


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


def test_bridge_persists_region_plan_and_rejects_unknown_region_before_chrome(geography, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('region_test_bridge', PARSER.parents[1] / 'scripts/local_parser_bridge.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    python = geography / '.venv/bin/python'; python.parent.mkdir(parents=True); python.touch()
    bridge = module.Bridge({'parser_root': str(geography)}, geography / 'runs')
    launched = []; monkeypatch.setattr(bridge, 'launch', lambda path: launched.append(path))
    request = dict(source='yandex', scope='regions', country_code='RU', country='Россия', region_codes=['RU.71'],
                   query='строительство', cities=[], rights_confirmed=True, rights_basis='Synthetic test')
    result = bridge.start(request)
    assert result['coverage']['regions'] == ['Свердловская область']
    assert len(json.loads((launched[0] / 'plan.json').read_text())) == 3
    assert next(r for r in bridge.catalog('2gis', '', 'regions') if r['code'] == 'RU.71')['source_cities'] == 2
    with pytest.raises(ValueError): bridge.start(dict(request, region_codes=['RU.999']))
    assert len(launched) == 1


@pytest.mark.asyncio
async def test_same_named_companies_in_different_countries_do_not_merge(session):
    from app.collector.repository import save_company
    common = dict(name='Строительная компания', website='https://fixture.example', address='ул. Центральная, 1')
    first, _ = await save_company(session, dict(common, external_id='uz-1', country='Узбекистан'), source='local_yandex', rights={'store': True})
    second, _ = await save_company(session, dict(common, external_id='kz-1', country='Казахстан'), source='local_yandex', rights={'store': True})
    assert first.id != second.id and first.country == 'Узбекистан' and second.country == 'Казахстан'
