"""Country-wide, coordinate-based plans. GeoNames data stays in a local cache."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

SOURCE = 'https://www.geonames.org/export/'
DUMP = 'https://download.geonames.org/export/dump/'


def build_database(root: Path, countries: bytes, cities_zip: bytes):
    destination = root / 'data/world-geography.sqlite'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as db:
            db.executescript('''CREATE TABLE countries(code TEXT PRIMARY KEY,name TEXT,iso3 TEXT);
                CREATE TABLE cities(id INTEGER PRIMARY KEY,country TEXT,name TEXT,ascii TEXT,lat REAL,lon REAL,pop INTEGER);
                CREATE INDEX city_country ON cities(country,pop DESC);
                CREATE TABLE metadata(payload TEXT);''')
            for line in countries.decode('utf-8-sig').splitlines():
                if not line or line.startswith('#'): continue
                row = line.split('\t')
                if len(row) >= 5 and re.fullmatch('[A-Z]{2}', row[0]):
                    db.execute('INSERT OR REPLACE INTO countries VALUES(?,?,?)', (row[0], row[4], row[1]))
            with zipfile.ZipFile(io.BytesIO(cities_zip)) as archive:
                info = archive.getinfo('cities500.txt')
                if info.file_size > 300 * 1024 * 1024: raise ValueError('Слишком большой каталог населённых пунктов')
                with archive.open(info) as source:
                    for line in io.TextIOWrapper(source, encoding='utf-8'):
                        row = line.rstrip('\n').split('\t')
                        if len(row) < 18 or not re.fullmatch('[A-Z]{2}', row[8]): continue
                        db.execute('INSERT OR REPLACE INTO cities VALUES(?,?,?,?,?,?,?)',
                                   (int(row[0]), row[8], row[1], row[2], float(row[4]), float(row[5]), int(row[14] or 0)))
            metadata = {'source': SOURCE, 'dataset': 'cities500', 'license': 'CC BY 4.0',
                        'downloaded_at': datetime.now(timezone.utc).isoformat(),
                        'sha256': hashlib.sha256(countries + cities_zip).hexdigest()}
            db.execute('INSERT INTO metadata VALUES(?)', (json.dumps(metadata),))
        db.close()
        temporary.chmod(0o600)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def install(root: Path):
    path = root / 'data/world-geography.sqlite'
    if path.exists(): return path
    def download(name, maximum):
        with urlopen(Request(DUMP + name, headers={'User-Agent': 'Quickly-geography/1.0'}), timeout=60) as response:
            data = response.read(maximum + 1)
        if len(data) > maximum: raise ValueError('Слишком большой файл географии')
        return data
    return build_database(root, download('countryInfo.txt', 1024 * 1024), download('cities500.zip', 30 * 1024 * 1024))


@contextmanager
def connect(root):
    path = Path(root) / 'data/world-geography.sqlite'
    if not path.is_file(): raise ValueError('Мировой каталог не установлен. Обновите локальный парсер Quickly.')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try: yield db
    finally: db.close()


def map_cities(root, source, code):
    if source == '2gis':
        rows = json.loads((Path(root) / 'vendor/parser_2gis/data/cities.json').read_text())
        return [dict(c, country_code=code.lower()) for c in rows if c.get('country_code', '').upper() == code and c['code'] != 'abkhazia']
    with connect(root) as db:
        return [dict(code='geo-' + str(r['id']), name=r['name'], ascii=r['ascii'], country_code=code.lower(),
                     latitude=r['lat'], longitude=r['lon']) for r in db.execute(
                         'SELECT * FROM cities WHERE country=? ORDER BY pop DESC,id', (code,))]


def countries(root, source):
    with connect(root) as db:
        rows = [dict(r) for r in db.execute('SELECT c.*,COUNT(p.id) cities FROM countries c LEFT JOIN cities p ON p.country=c.code GROUP BY c.code ORDER BY c.name')]
    counts = {}
    if source == '2gis':
        for city in json.loads((Path(root) / 'vendor/parser_2gis/data/cities.json').read_text()):
            code = city.get('country_code', '').upper()
            counts[code] = counts.get(code, 0) + 1
    return [dict(row, source_cities=counts.get(row['code'], 0) if source == '2gis' else row['cities']) for row in rows]


def resolve(root, request):
    """Resolve a fixed plan before starting Chrome; never fall back to Russia."""
    code = request.get('country_code', 'RU').upper()
    source = request['source']
    with connect(root) as db:
        country = db.execute('SELECT name FROM countries WHERE code=?', (code,)).fetchone()
        metadata = json.loads(db.execute('SELECT payload FROM metadata').fetchone()[0])
    if not country: raise ValueError('Неизвестный код страны: ' + code)
    catalogue = map_cities(root, source, code)
    scope = request.get('scope', 'cities')
    if scope == 'country':
        selected = catalogue
    elif code == 'RU' and source == 'yandex':
        from .geography import yandex_cities, select_yandex_cities
        selected = select_yandex_cities(yandex_cities(), request['cities'])
    else:
        selected = []
        for name in request['cities']:
            matches = [c for c in catalogue if name.casefold() in {c['name'].casefold(), c['code'].casefold(), c.get('ascii', '').casefold()}]
            if not matches and source == 'yandex':
                matches = [dict(name=name, code='manual-' + hashlib.sha256((code + name).encode()).hexdigest()[:12], country_code=code.lower())]
            if not matches: raise ValueError('Нет города в каталоге 2ГИС выбранной страны: ' + name)
            selected.extend(c for c in matches if c not in selected)
    if not selected:
        raise ValueError('У выбранного источника нет населённых пунктов этой страны. Выберите Яндекс Карты или другую страну.')
    name = request.get('country') or country['name']
    selected = [dict(c, country=name, country_code=code.lower(), search_name=c['name'] + ', ' + name) for c in selected]
    return selected, dict(scope=scope, country=name, country_code=code, planned=len(selected), source=SOURCE,
                          dataset=metadata['dataset'] if source == 'yandex' else '2gis-catalog',
                          note='Каталог GeoNames: населённые пункты от 500 жителей и административные центры. Полнота компаний зависит от карт.' if source == 'yandex' else 'Обход городов, представленных в каталоге 2ГИС.')
