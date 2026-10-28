"""Normalisation only: no guessed employee addresses or SMTP recipient probes."""
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from app.local_imports import email_address


def normalized_text(value):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', str(value or '')).casefold()).strip()


def website_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if '://' not in value:
        value = 'https://' + value
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Нужен публичный HTTP/HTTPS URL без логина и пароля')
    if parsed.port and parsed.port not in {80, 443}:
        raise ValueError('Для обхода сайтов разрешены только порты 80 и 443')
    host = parsed.hostname.encode('idna').decode('ascii').lower().rstrip('.')
    return urlunsplit((parsed.scheme, host, parsed.path or '/', parsed.query, ''))


def domain_of(value):
    return (urlsplit(value).hostname or '').lower().removeprefix('www.')


def contact_email(value):
    email = email_address(value)
    local, domain = email.split('@')
    if domain in {'example.com', 'example.org', 'example.net', 'domain.com', 'email.com', 'test.com'} or domain.endswith(('.invalid', '.test', '.example')):
        raise ValueError('Тестовый или шаблонный адрес')
    if email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.js', '.css')) or local in {'noreply', 'no-reply', 'donotreply', 'mailer-daemon', 'postmaster'}:
        raise ValueError('Служебный или мусорный email')
    return email


def contact_phone(value):
    raw = str(value or '').strip()
    digits = re.sub(r'\D', '', raw)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) == 10:
        digits = '7' + digits
    if not 11 <= len(digits) <= 15 or len(set(digits)) < 3:
        raise ValueError('Некорректный телефон')
    return '+' + digits


def split_values(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r'[;,|\n]+', str(value or '')) if p.strip()]


def valid_inn(value):
    value = re.sub(r'\s', '', str(value or ''))
    if not value:
        return ''
    if not value.isdigit() or len(value) not in {10, 12}:
        raise ValueError('ИНН должен содержать 10 или 12 цифр')
    digits = list(map(int, value))
    def check(weights):
        return sum(w * d for w, d in zip(weights, digits)) % 11 % 10
    if len(value) == 10 and check([2, 4, 10, 3, 5, 9, 4, 6, 8]) != digits[9]:
        raise ValueError('Неверная контрольная сумма ИНН')
    if len(value) == 12 and (check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) != digits[10] or check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) != digits[11]):
        raise ValueError('Неверная контрольная сумма ИНН')
    return value


def normalize_record(raw):
    row = dict(raw)
    out = {k: str(row.get(k) or '').strip()[:maxlen] for k, maxlen in {
        'name': 512, 'kpp': 9, 'ogrn': 15, 'country': 100, 'region': 255, 'city': 255,
        'address': 1024, 'industry': 1000, 'okved_main': 12, 'okved_as_of': 32,
    }.items()}
    if not out['name']:
        raise ValueError('У компании отсутствует название')
    out['inn'] = valid_inn(row.get('inn'))
    if out['kpp'] and not re.fullmatch(r'\d{9}', out['kpp']):
        raise ValueError('КПП должен содержать 9 цифр')
    if out['ogrn'] and (not out['ogrn'].isdigit() or len(out['ogrn']) not in {13, 15}):
        raise ValueError('ОГРН должен содержать 13 или 15 цифр')
    out['okved_extra'] = split_values(row.get('okved_extra'))
    for code in [out['okved_main'], *out['okved_extra']]:
        if code and not re.fullmatch(r'\d{2}(?:\.\d{1,2}){0,2}', code):
            raise ValueError('Некорректный код ОКВЭД')
    # Rubrics/categories are deliberately never converted to OKVED codes.
    out['website'] = website_url(row.get('website'))
    out['employees'] = int(row['employees']) if row.get('employees') not in {'', None} else None
    active = row.get('active')
    out['active'] = active if isinstance(active, bool) else True if str(active).lower() in {'1', 'true', 'да', 'действующее', 'active'} else False if str(active).lower() in {'0', 'false', 'нет', 'ликвидировано', 'closed'} else None
    out['contacts'] = []
    for kind, keys, validator in [('email', ['email', 'emails'], contact_email), ('phone', ['phone', 'phones'], contact_phone)]:
        for key in keys:
            for value in split_values(row.get(key)):
                try:
                    entry = {'kind': kind, 'value': validator(value), 'url': str(row.get('source_url') or out['website'])}
                    if entry not in out['contacts']:
                        out['contacts'].append(entry)
                except ValueError:
                    continue
    out['source_url'] = str(row.get('source_url') or out['website'])[:2048]
    out['external_id'] = str(row.get('external_id') or '')[:255]
    return out
