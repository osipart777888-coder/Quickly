from __future__ import annotations

import html
import ipaddress
import re
import socket
import time
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

AGENT = "CompanyContactsBot/0.1"
EMAIL = re.compile(r"(?<![\w.+-])([a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9\u0080-\uffff](?:[a-z0-9\u0080-\uffff.-]*[a-z0-9\u0080-\uffff])?\.(?:xn--[a-z0-9-]{2,59}|[a-z\u0080-\uffff]{2,63}))(?![\w.-])", re.I)
BAD_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "woff", "woff2", "pdf", "test", "invalid", "local"}
CONTACT = re.compile(r"контакт|реквизит|связаться|о[ -]компании|contact|about|impressum", re.I)


def extract_emails(text: str) -> set[str]:
    text = html.unescape(unquote(text))
    text = re.sub(r"\s*[\[({]\s*(?:at|собака)\s*[\])}]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[({]\s*(?:dot|точка)\s*[\])}]\s*", ".", text, flags=re.I)
    result = set()
    for match in EMAIL.finditer(text):
        email = match.group(1).strip(".").lower()
        local, domain = email.rsplit("@", 1)
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            continue
        if (len(local) > 64 or len(email) > 254 or ".." in email or domain.rsplit(".", 1)[-1] in BAD_TLDS
                or domain in ("example.com", "example.org", "example.net", "example.ru", "domain.com", "email.com",
                              "yourdomain.com", "your-domain.com", "yoursite.com", "yoursitename.com")
                or domain.endswith((".sentry.io", ".sentry-next.wixpress.com"))):
            continue
        result.add(local + "@" + domain)
    return result


class ContactHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.mailtos: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.skip = 0
        self.link: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if self.skip:
            return
        if tag == "a":
            href = attrs.get("href", "") or ""
            self.link = [href, ""]
            if href.lower().startswith("mailto:"):
                self.mailtos.append(href[7:].split("?", 1)[0])
        encoded = attrs.get("data-cfemail")
        if encoded:
            try:
                data = bytes.fromhex(encoded)
                self.mailtos.append(bytes(b ^ data[0] for b in data[1:]).decode())
            except (ValueError, UnicodeError, IndexError):
                pass

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        if tag == "a" and self.link:
            self.links.append(tuple(self.link))
            self.link = None
        if not self.skip:
            self.text.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)
            if self.link is not None:
                self.link[1] += data

    def emails(self) -> set[str]:
        return extract_emails(" ".join(self.text + self.mailtos))


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    text: str
    status: int = 200
    content_type: str = "text/html"


def host_key(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def check_public_url(url: str):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise FetchError("Недопустимый адрес сайта")
    try:
        addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, ValueError) as exc:
        raise FetchError(f"Не найден домен: {parts.hostname}") from exc
    def blocked(address):
        ip = ipaddress.ip_address(address)
        # Network runtimes/VPNs can map public domain names to synthetic address ranges.
        # Reject literal IPs there; permit only the resolver's mapping for a domain name.
        fake_dns = ip.version == 4 and any(ip in ipaddress.ip_network(net) for net in ("198.18.0.0/15", "240.0.0.0/4"))
        literal = False
        try:
            ipaddress.ip_address(parts.hostname)
            literal = True
        except ValueError:
            pass
        return not ip.is_global and not (fake_dns and not literal)
    if not addresses or any(blocked(addr[4][0]) for addr in addresses):
        raise FetchError("Адрес не является публичным сайтом")


class HTTPClient:
    def __init__(self, timeout: float = 12, delay: float = 1):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = AGENT
        self.timeout = timeout
        self.delay = delay
        self.last_request: dict[str, float] = {}

    def fetch(self, url: str) -> Page:
        for _ in range(6):
            check_public_url(url)
            host = host_key(url)
            wait = self.delay - (time.monotonic() - self.last_request.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self.last_request[host] = time.monotonic()
            try:
                with self.session.get(url, timeout=(self.timeout, self.timeout), stream=True, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchError("Перенаправление без адреса")
                        url = urljoin(url, location)
                        continue
                    content_type = response.headers.get("Content-Type", "").lower()
                    if response.status_code >= 400:
                        return Page(url, "", response.status_code, content_type)
                    if not any(t in content_type for t in ("text/", "xhtml", "application/xml")):
                        raise FetchError("Страница не является текстом/HTML")
                    body = bytearray()
                    started = time.monotonic()
                    for chunk in response.iter_content(32768):
                        body.extend(chunk)
                        if len(body) > 2_000_000 or time.monotonic() - started > self.timeout:
                            raise FetchError("Страница превышает лимит размера или времени")
                    response._content = bytes(body)
                    encoding = response.encoding if "charset=" in content_type else response.apparent_encoding
                    return Page(response.url, body.decode(encoding or "utf-8", errors="replace"), response.status_code, content_type)
            except requests.RequestException as exc:
                raise FetchError(f"Ошибка соединения: {type(exc).__name__}") from exc
        raise FetchError("Слишком много перенаправлений")

    def close(self):
        self.session.close()


class WebsiteCrawler:
    def __init__(self, client=None, max_pages: int = 4):
        self.client = client or HTTPClient()
        self.max_pages = max_pages
        self.robots: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            rules = RobotFileParser(origin + "/robots.txt")
            try:
                page = self.client.fetch(origin + "/robots.txt")
                if page.status in (401, 403, 429) or page.status >= 500:
                    self.robots[origin] = None
                else:
                    rules.parse(page.text.splitlines() if page.status == 200 else [])
                    self.robots[origin] = rules
            except FetchError:
                self.robots[origin] = None
        rules = self.robots[origin]
        return rules is not None and rules.can_fetch(AGENT, url)

    def crawl(self, website: str) -> dict:
        queue = deque([website])
        visited = set()
        findings: set[tuple[str, str]] = set()
        pages = []
        errors = []
        root_host = host_key(website)
        while queue and len(visited) < self.max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            if not self.allowed(url):
                errors.append(f"Пропущено по robots.txt или robots.txt недоступен: {url}")
                continue
            try:
                page = self.client.fetch(url)
                if page.status in (401, 403, 429):
                    errors.append(f"HTTP {page.status}: {url}")
                    break
                if page.status != 200:
                    errors.append(f"HTTP {page.status}: {url}")
                    continue
                if not pages:  # Follow the company's public homepage redirect.
                    root_host = host_key(page.url)
                elif host_key(page.url) != root_host:
                    errors.append(f"Страница перенаправлена на другой сайт: {url}")
                    continue
                parser = ContactHTML()
                parser.feed(page.text)
                pages.append(page.url)
                findings.update((email, page.url) for email in parser.emails())
                if len(pages) == 1:
                    links = []
                    for href, label in parser.links:
                        target = urljoin(page.url, href)
                        parts = urlsplit(target)
                        target = parts._replace(fragment="").geturl()
                        if (parts.scheme in ("http", "https") and host_key(target) == root_host
                                and CONTACT.search(unquote(href) + " " + label) and not parts.query):
                            priority = 0 if re.search(r"контакт|реквизит|contact|requisite|impressum", unquote(href) + " " + label, re.I) else 1
                            links.append((priority, target))
                    queue.extend(dict.fromkeys(target for _, target in sorted(links, key=lambda pair: pair[0])))
                    if not links:
                        queue.extend(urljoin(page.url, path) for path in ("/contacts", "/contact", "/kontakty"))
            except (FetchError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
        return {"status": "ok" if pages and not errors else "partial" if pages else "error",
                "findings": sorted(findings), "pages": pages, "errors": errors}
