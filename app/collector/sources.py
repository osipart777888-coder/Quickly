"""Official provider adapters and fail-closed storage/export/request permissions."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from sqlalchemy import select
from app.collector.models import CollectorSource
from app.collector.websites import SourceError, get_json
from app.time import utcnow

SOURCE_INFO = {
    'website': ('Сайты организаций', 'https://www.rfc-editor.org/rfc/rfc9309'),
    'import': ('CSV / XLSX / GeoLeadScraper', 'https://github.com/ozhehkovski/geoleadscraper'),
    'yandex': ('Яндекс Карты', 'https://yandex.ru/maps-api/products/geosearch-api'),
    'twogis': ('2ГИС', 'https://docs.2gis.com/api/search/places/overview'),
    'spark': ('СПАРК-Интерфакс', 'https://spark-interfax.ru/integration'),
    'fns': ('ЕГРЮЛ / ЕГРИП — импорт официального файла', 'https://www.nalog.gov.ru/opendata/'),
}


async def ensure_sources(db):
    for key, (_, url) in SOURCE_INFO.items():
        if not await db.get(CollectorSource, key):
            local = key in {'website', 'import', 'fns'}
            db.add(CollectorSource(key=key, enabled=local, allow_store=local, allow_export=local,
                terms_url=url, rights_basis='Только данные с отдельным подтверждением прав в запуске' if local else ''))
    await db.flush()


def permissions(source):
    if not source or not source.enabled:
        raise SourceError('Источник не подключён')
    if not source.allow_store or not source.rights_basis.strip():
        raise SourceError('Не подтверждено право хранения данных источника')
    return {'store': True, 'export': bool(source.allow_export), 'terms_url': source.terms_url,
            'basis': source.rights_basis, 'recorded_at': utcnow().isoformat()}


def connection_state(source):
    if not source or not source.enabled:
        return 'not_connected', 'Источник не подключён'
    try:
        permissions(source)
        if source.key == 'spark':
            return 'not_connected', 'Нужны договор, WSDL и описание разрешённых SOAP-методов'
        if source.key in {'yandex', 'twogis'}:
            if not source.api_key:
                return 'not_connected', 'Введите API-ключ'
            if not source.paid_authorized or source.daily_request_limit <= 0:
                return 'blocked', 'Нужно разрешение запросов и суточный бюджет'
            cfg = source.config or {}
            if Decimal(str(cfg.get('daily_budget_rub', 0))) <= 0 or Decimal(str(cfg.get('request_cost_rub', 0))) <= 0:
                return 'blocked', 'Укажите стоимость запроса и максимальный расход в сутки'
            if source.key == 'twogis' and not cfg.get('region_id'):
                return 'blocked', 'Укажите region_id из договора / Regions API 2ГИС'
        return 'ready', 'Готов к работе' if source.key not in {'import', 'fns'} else 'Готов к импорту файла с подтверждёнными правами'
    except (SourceError, ValueError, ArithmeticError) as error:
        return 'blocked', str(error)


async def reserve_api_request(db, key):
    """Durably debit before IO. A network failure still consumes the local budget."""
    source = await db.scalar(select(CollectorSource).where(CollectorSource.key == key).with_for_update())
    state, reason = connection_state(source)
    if state != 'ready':
        raise SourceError(reason)
    now = utcnow()
    if source.next_request_at and source.next_request_at > now:
        raise SourceError('Интервал API: следующий запрос отложен', retry_after=max(1, (source.next_request_at - now).total_seconds()))
    day = now.date().isoformat()
    if source.budget_day != day:
        source.budget_day, source.requests_used = day, 0
    cfg = source.config or {}
    cost = Decimal(str(cfg.get('request_cost_rub', 0)))
    budget = Decimal(str(cfg.get('daily_budget_rub', 0)))
    if source.requests_used >= source.daily_request_limit or cost * (source.requests_used + 1) > budget:
        raise SourceError('Суточный бюджет API исчерпан; продолжите после изменения бюджета или на следующий день')
    source.requests_used += 1
    source.next_request_at = now + timedelta(seconds=max(1, min(60, float(cfg.get('interval_seconds', 2)))))
    await db.commit()
    return source


@dataclass
class SourcePage:
    records: list
    next_cursor: int | None


class SourceAdapter:
    """Future providers implement this one page-at-a-time interface."""
    key = ''
    def search(self, source, query, cursor=0):
        raise NotImplementedError


class YandexAdapter(SourceAdapter):
    key = 'yandex'
    def search(self, source, query, cursor=0):
        data = get_json('https://search-maps.yandex.ru/v1/', {'apikey': source.api_key,
            'text': query, 'lang': 'ru_RU', 'type': 'biz', 'results': 50, 'skip': cursor})
        rows = []
        for feature in data.get('features', []):
            item = feature.get('properties', {}).get('CompanyMetaData', {})
            if not item:
                continue
            rows.append({'name': item.get('name', ''), 'address': item.get('address', ''),
                'website': item.get('url', ''), 'phones': [p.get('formatted', '') for p in item.get('Phones', [])],
                'industry': '; '.join(p.get('name', '') for p in item.get('Categories', [])),
                'external_id': str(item.get('id', '')), 'source_url': 'https://yandex.ru/maps/org/' + str(item.get('id', ''))})
        return SourcePage(rows, cursor + 50 if len(rows) == 50 else None)


class TwoGisAdapter(SourceAdapter):
    key = 'twogis'
    def search(self, source, query, cursor=0):
        config = source.config or {}
        fields = ['items.adm_div', 'items.rubrics']
        if config.get('contacts_access'):
            fields.append('items.contact_groups')
        if config.get('employees_access'):
            fields.append('items.employees_org_count')
        if config.get('inn_access'):
            fields.append('items.itin')
        params = {'key': source.api_key, 'q': query, 'type': 'branch', 'page': cursor + 1,
                  'page_size': 50, 'region_id': config['region_id'], 'fields': ','.join(fields)}
        if config.get('rubric_id'):
            params['rubric_id'] = config['rubric_id']
        data = get_json('https://catalog.api.2gis.com/3.0/items', params)
        code = data.get('meta', {}).get('code', 200)
        if code != 200:
            if code == 404:
                return SourcePage([], None)
            raise SourceError('2ГИС отклонил запрос; проверьте права и бюджет', blocked=code in {403, 429}, retry_after=60 if code == 429 else 0)
        rows = []
        for item in data.get('result', {}).get('items', []):
            contacts = [contact for group in item.get('contact_groups', []) for contact in group.get('contacts', [])]
            websites = [c.get('url', c.get('value', '')) for c in contacts if c.get('type') == 'website']
            row = {'name': item.get('name', ''), 'address': item.get('full_name') or item.get('address_name', ''),
                'website': websites[0] if websites else '', 'external_id': str(item.get('id', '')),
                'source_url': 'https://2gis.ru/firm/' + str(item.get('id', '')),
                'emails': [c.get('value', '') for c in contacts if c.get('type') == 'email'],
                'phones': [c.get('value', '') for c in contacts if c.get('type') == 'phone'],
                'industry': '; '.join(r.get('name', '') for r in item.get('rubrics', [])),
                'inn': item.get('itin', '')}
            for region in item.get('adm_div', []):
                if region.get('type') == 'city': row['city'] = region.get('name', '')
                if region.get('type') == 'region': row['region'] = region.get('name', '')
            # Counts represented by ranges are left unknown, never guessed.
            count = item.get('employees_org_count')
            if isinstance(count, int): row['employees'] = count
            rows.append(row)
        return SourcePage(rows, cursor + 1 if len(rows) == 50 else None)


class SparkAdapter(SourceAdapter):
    key = 'spark'
    def search(self, source, query, cursor=0):
        raise SourceError('СПАРК не подключён: необходимы WSDL и документированные методы вашего договора')


ADAPTERS = {adapter.key: adapter for adapter in [YandexAdapter(), TwoGisAdapter(), SparkAdapter()]}
