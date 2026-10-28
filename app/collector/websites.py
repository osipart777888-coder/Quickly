"""Bounded public-page reader with pinned DNS, robots and no JS execution."""
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlencode, unquote
from urllib.robotparser import RobotFileParser
from app.collector.validation import website_url, domain_of, contact_email, contact_phone

USER_AGENT = 'QuicklyCompanyCollector/1.0'
MAX_PAGE_BYTES = 2 * 1024 * 1024


class SourceError(Exception):
    def __init__(self, message, *, retry_after=0, blocked=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.blocked = blocked


@dataclass
class Page:
    url: str
    status: int
    headers: dict
    text: str


def public_addresses(host):
    if host.lower() == 'localhost' or host.lower().endswith(('.localhost', '.local', '.internal')):
        raise SourceError('Внутренние адреса запрещены')
    try:
        addresses = sorted({r[4][0] for r in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    except OSError:
        raise SourceError('Не удалось определить публичный адрес сайта', retry_after=30) from None
    # Some desktop VPNs replace public DNS with synthetic addresses. Resolve
    # only that special case through a fixed, certificate-verified DoH endpoint;
    # RFC1918, loopback, metadata, mixed public/private answers still fail closed.
    synthetic = [ipaddress.ip_network('198.18.0.0/15'), ipaddress.ip_network('240.0.0.0/4')]
    if addresses and all(any(ipaddress.ip_address(ip) in subnet for subnet in synthetic) for ip in addresses):
        addresses = doh_addresses(host)
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise SourceError('Сайт указывает на частную или служебную сеть')
    return addresses


def doh_addresses(host):
    connection = http.client.HTTPConnection('cloudflare-dns.com', 443, timeout=8)
    try:
        raw_socket = socket.create_connection(('1.1.1.1', 443), timeout=8)
        try:
            connection.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname='cloudflare-dns.com')
        except BaseException:
            raw_socket.close()
            raise
        connection.request('GET', '/dns-query?' + urlencode({'name':host, 'type':'A'}),
            headers={'Host':'cloudflare-dns.com', 'Accept':'application/dns-json', 'Connection':'close'})
        response = connection.getresponse()
        data = response.read(65537)
        if response.status != 200 or len(data) > 65536: raise ValueError()
        answer = json.loads(data)
        addresses = sorted({item['data'] for item in answer.get('Answer', []) if item.get('type') == 1})
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses): raise ValueError()
        return addresses
    except (OSError, ValueError, http.client.HTTPException):
        raise SourceError('VPN подменяет DNS, а публичный DNS недоступен; чтение отложено', retry_after=60) from None
    finally:
        connection.close()


def fetch_public(url, *, params=None, allowed_domain=None, timeout=12, url_guard=None):
    """Validate every redirect, connect to the validated IP, verify original SNI.

    No proxy environment, cookies, credentials, downloaded scripts or browser
    profile is used. Error messages never include query parameters/API keys.
    """
    url = website_url(url)
    if params:
        url += ('&' if '?' in url else '?') + urlencode(params)
    deadline = time.monotonic() + 35
    for _ in range(5):
        if time.monotonic() > deadline:
            raise SourceError('Истекло общее время чтения сайта', retry_after=30)
        parsed = urlsplit(url)
        if allowed_domain and domain_of(url) != allowed_domain:
            raise SourceError('Переадресация на другой домен остановлена')
        if url_guard and not url_guard(url):
            raise SourceError('Доступ к странице запрещён robots.txt', blocked=True)
        address = public_addresses(parsed.hostname)[0]
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        sock = None
        try:
            sock = socket.create_connection((address, port), timeout=timeout)
            if parsed.scheme == 'https':
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path, headers={'Host': parsed.hostname, 'User-Agent': USER_AGENT,
                'Accept': 'text/html,application/json,text/plain', 'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = connection.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                url = website_url(urljoin(url, headers.get('location', '')))
                continue
            if response.status in {401, 403}:
                raise SourceError('Доступ запрещён источником (401/403); обход остановлен', blocked=True)
            if response.status == 429:
                delay = headers.get('retry-after', '60')
                delay = min(86400, max(60, int(delay))) if delay.isdigit() else 300
                raise SourceError('Источник ограничил частоту запросов (429)', retry_after=delay, blocked=True)
            if response.status >= 500 or response.status == 408:
                raise SourceError('Временная ошибка источника', retry_after=30)
            if int(headers.get('content-length', '0') or '0') > MAX_PAGE_BYTES:
                raise SourceError('Страница превышает предел 2 МБ')
            raw = response.read(MAX_PAGE_BYTES + 1)
            if len(raw) > MAX_PAGE_BYTES or headers.get('content-encoding', 'identity') != 'identity':
                raise SourceError('Слишком большая или сжатая страница; чтение остановлено')
            encoding = re.search(r'charset=([\w-]+)', headers.get('content-type', ''))
            codec = encoding.group(1) if encoding else 'utf-8'
            try:
                body = raw.decode(codec, errors='replace')
            except LookupError:
                body = raw.decode('utf-8', errors='replace')
            return Page(url, response.status, headers, body)
        except SourceError:
            raise
        except (OSError, http.client.HTTPException):
            raise SourceError('Ошибка или тайм-аут подключения к источнику', retry_after=30) from None
        finally:
            connection.close()
            if sock:
                sock.close()
    raise SourceError('Слишком много переадресаций')


def get_json(url, params):
    page = fetch_public(url, params=params, allowed_domain=domain_of(url))
    if page.status != 200:
        raise SourceError('API вернул ошибку запроса; проверьте настройки доступа')
    try:
        return json.loads(page.text)
    except ValueError:
        raise SourceError('API вернул некорректный JSON') from None


class ContactHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.texts, self.mailtos, self.phones, self.forms = [], [], [], [], []
        self.hidden = 0
        self.current_href = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'noscript', 'template'}:
            self.hidden += 1
        if tag == 'a':
            href = attrs.get('href', '')
            self.current_href = href
            self.links.append((href, ''))
            if href.lower().startswith('mailto:'):
                self.mailtos.extend(re.split('[,;]', unquote(href[7:].split('?')[0])))
            if href.lower().startswith('tel:'):
                self.phones.append(unquote(href[4:]))
        if tag == 'form':
            self.forms.append(attrs.get('action', ''))

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript', 'template'}:
            self.hidden = max(0, self.hidden - 1)
        if tag == 'a':
            self.current_href = ''

    def handle_data(self, data):
        if not self.hidden:
            self.texts.append(data)
            if self.current_href and self.links:
                href, label = self.links[-1]
                self.links[-1] = (href, label + data)


def parse_contacts(page):
    parser = ContactHTMLParser()
    parser.feed(page.text)
    text = ' '.join(parser.texts)
    emails = parser.mailtos + re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", text)
    phones = parser.phones + re.findall(r'(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}', text)
    contacts = []
    for kind, values, validator in [('email', emails, contact_email), ('phone', phones, contact_phone)]:
        for value in values:
            try:
                item = {'kind': kind, 'value': validator(value.strip('.,;:()<>')), 'url': page.url}
                if item not in contacts:
                    contacts.append(item)
            except ValueError:
                continue
    # Only contact-looking forms, with a URL to the containing page. No submit.
    if parser.forms and re.search(r'контакт|связ|contact|feedback', page.url + text, re.I):
        contacts.append({'kind': 'form', 'value': page.url, 'url': page.url})
    return contacts, parser.links


def extract_website(website, *, fetch=fetch_public, sleep=time.sleep, max_pages=4, check_mx=False):
    start = website_url(website)
    domain = domain_of(start)
    robots_url = urljoin(start, '/robots.txt')
    robots = fetch(robots_url, allowed_domain=domain)
    policy = RobotFileParser(robots_url)
    if robots.status == 200:
        policy.parse(robots.text.splitlines())
    elif robots.status in {404, 410}:
        policy.parse([])
    else:
        raise SourceError('Не удалось проверить robots.txt; обход отложен', retry_after=60)
    delay = max(1, policy.crawl_delay(USER_AGENT) or policy.crawl_delay('*') or 1)
    request_rate = policy.request_rate(USER_AGENT) or policy.request_rate('*')
    if request_rate:
        delay = max(delay, request_rate.seconds / request_rate.requests)
    if delay > 30:
        raise SourceError('robots.txt требует интервал свыше 30 секунд; нужен отдельный медленный запуск', blocked=True)
    pending, visited, contacts = [start], set(), []
    while pending and len(visited) < max_pages:
        url = pending.pop(0)
        if url in visited or domain_of(url) != domain:
            continue
        visited.add(url)
        if not policy.can_fetch(USER_AGENT, url):
            if url == start:
                raise SourceError('Обход запрещён robots.txt', blocked=True)
            continue
        sleep(delay)
        page = fetch(url, allowed_domain=domain, url_guard=lambda target: policy.can_fetch(USER_AGENT, target))
        if not policy.can_fetch(USER_AGENT, page.url):
            raise SourceError('Переадресация ведёт в запрещённый robots.txt раздел', blocked=True)
        if page.status != 200:
            continue
        if re.search(r'<title[^>]*>[^<]*(?:captcha|access denied|проверка.*робот)', page.text, re.I) or re.search(r'(?:g-recaptcha|h-captcha)[^>]*data-sitekey', page.text, re.I):
            raise SourceError('Сайт требует CAPTCHA; обход остановлен', blocked=True)
        if 'html' not in page.headers.get('content-type', 'text/html'):
            continue
        found, links = parse_contacts(page)
        for item in found:
            if item not in contacts:
                contacts.append(item)
        for href, label in links:
            if re.search(r'контакт|о компании|о нас|contact|about|requisites|rekvizit', href + ' ' + label, re.I):
                try:
                    target = website_url(urljoin(page.url, href))
                except ValueError:
                    continue
                if domain_of(target) == domain and target not in visited and target not in pending:
                    pending.append(target)
        pending = pending[:max_pages]
    if check_mx:
        import dns.resolver
        for item in contacts:
            if item['kind'] == 'email':
                try:
                    answers = dns.resolver.resolve(item['value'].split('@')[1], 'MX', lifetime=3)
                    item['mx_status'] = 'available' if any(str(a.exchange) != '.' for a in answers) else 'no_mx'
                except dns.resolver.NoAnswer:
                    item['mx_status'] = 'no_mx'
                except Exception:
                    item['mx_status'] = 'unknown'
    return {'contacts': contacts, 'pages_checked': len(visited)}
