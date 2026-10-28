"""Collectors for visible Yandex Maps and business-directory pages."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .sources import SOURCES, source_url
from .storage import contact_url
from .websites import extract_emails


class NeedsAccess(Exception):
    pass


class RateLimited(NeedsAccess):
    pass


def check_access(snapshot: dict):
    title = snapshot.get("title", "")
    text = snapshot.get("bodyText", snapshot.get("text", ""))
    if text.strip().lower() in ("limited", "too many requests", "rate limit exceeded") or re.search(
            r"HTTP\s*429|429\s+too many requests|слишком много запросов", title + " " + text[:600], re.I):
        raise RateLimited("Сервис временно ограничил запросы. Очередь сохранена; подождите и выберите продолжение сбора.")
    if re.search(r"403 forbidden|access denied|доступ.*огранич|captcha|капч|проверка браузера|вы не робот", title + " " + text[:600], re.I):
        raise NeedsAccess("Сайт ограничил доступ или запросил проверку. Откройте «Вход и выборка» для этого источника.")


def card_url(source: str, url: str) -> str | None:
    try:
        url = source_url(source, url)
    except ValueError:
        return None
    parts = urlsplit(url)
    if source == "yandex":
        match = re.search(r"(/maps/org/[^/]+/\d+/)", parts.path)
        return urlunsplit((parts.scheme, parts.netloc, match.group(1), "", "")) if match else None
    if source == "rusprofile":
        match = re.match(r"/(id|ip)/(\d+)/?$", parts.path)
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")) if match else None
    if source == "spark":
        route = parts.path + "#" + parts.fragment
        ids = parse_qs(parts.query)
        fragment_query = parts.fragment.partition("?")[2]
        ids.update(parse_qs(fragment_query))
        identified = any(key.casefold() in ("companyid", "organizationid", "contragentid", "entityid", "cardid")
                         and re.fullmatch(r"[a-z0-9-]{3,}", values[0], re.I) for key, values in ids.items() if values)
        if re.search(r"-inn-\d{10,12}\b|[/#](?:company|companies|organization|contragent|entity|card)/(?:card/|profile/)?(?:\d+|[a-f0-9-]{8,})(?:[/#?]|$)", route, re.I) or identified:
            fragment_is_route = bool(re.search(r"(?:company|organization|contragent|entity|card)[/?]", parts.fragment, re.I))
            fragment_has_id = any(key.casefold() in ("companyid", "organizationid", "contragentid", "entityid", "cardid") for key in parse_qs(fragment_query))
            if re.search(r"-inn-\d{10,12}\b", parts.path) or not (fragment_is_route or fragment_has_id):
                return parts._replace(fragment="").geturl()
            return url
    return None


def contact_text(text: str) -> str:
    match = re.search(r"(?:Контакты|Контактная информация|Контактные данные)\s*[:\n]", text, re.I)
    if not match:
        return ""
    section = text[match.end():]
    return re.split(r"\n\s*(?:Информация о компании|Сведения о|Виды деятельности|Описание|Связи|Похожие организации)\b", section, maxsplit=1, flags=re.I)[0][:6000]


def field_value(snapshot: dict, name: str) -> str:
    return next((f.get("text", "") for f in snapshot.get("fields", []) if f.get("name") == name), "")


def extract_card(source: str, snapshot: dict, task: dict) -> dict:
    check_access(snapshot)
    url = snapshot["url"]
    if not card_url(source, url):
        raise ValueError("Открыта не карточка компании: результат не сохраняется")
    name = snapshot.get("h1", "").strip()
    if not name:
        raise ValueError("Не найдено название компании; структура карточки требует проверки")
    main_text = snapshot.get("text", "")
    section = contact_text(main_text)
    links = [a for a in snapshot.get("links", []) if not a.get("inChrome")]
    emails = set()
    websites = set()
    phones = set()
    for link in links:
        href = link.get("href", "")
        if href.startswith("mailto:"):
            emails.update(extract_emails(href[7:].split("?", 1)[0]))
        elif href.startswith("tel:"):
            phones.add(unquote(href[4:]))
        elif (source == "yandex" and "business-urls-view__link" in link.get("cls", "")) or (source != "yandex" and (link.get("inContacts") or href in section)):
            if website := contact_url(href):
                websites.add(website)
    emails.update(extract_emails(section))
    emails.update(extract_emails(field_value(snapshot, "email")))
    for field in snapshot.get("fields", []):
        if field.get("name") == "url":
            if website := contact_url(field.get("href") or field.get("text", "")):
                websites.add(website)
    for candidate in re.findall(r"https?://[^\s<>\"']+", section):
        if website := contact_url(candidate.rstrip(".,;)")):
            websites.add(website)
    # Include only public text rendered in the contacts area, not support/footer data.
    if source == "yandex":
        phone_text = snapshot.get("phoneText", "")
        phones.update(re.findall(r"(?:\+7|8)[\s(\d)–‒—-]{9,25}\d", phone_text))
    inn = field_value(snapshot, "taxID")
    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        match = re.search(r"\bИНН\s*[:\n]?\s*(\d{12}|\d{10})\b", main_text)
        inn = match.group(1) if match else ""
    if source == "yandex":
        identity = re.search(r"/org/[^/]+/(\d+)", url).group(1)
    elif source == "rusprofile":
        identity = re.search(r"/(?:id|ip)/(\d+)", url).group(1)
    else:
        identity = inn or hashlib.sha256(url.encode()).hexdigest()[:24]
    address = snapshot.get("address") or field_value(snapshot, "address")
    if not address:
        match = re.search(r"(?:Юридический адрес|Адрес)\s*[:\n]\s*([^\n]+)", main_text)
        address = match.group(1).strip() if match else ""
    city = "" if task["city"] == "Россия" else task["city"]
    if source == "yandex":
        for raw in snapshot.get("ld", []):
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
                    for crumb in data.get("itemListElement", []):
                        item = crumb.get("item", {})
                        if re.fullmatch(r"/maps/\d+/[^/]+/?", urlsplit(item.get("@id", "")).path):
                            city = item.get("name", city)
            except (ValueError, TypeError):
                continue
    elif match := re.search(r"(?:\bг\.|\bгород)\s+([^,\n]+)", address):
        city = match.group(1).strip()
    portal_domains = (*SOURCES[source]["domains"], "interfax.ru")
    emails = {e for e in emails if not any(e.rsplit("@", 1)[-1] == domain or e.rsplit("@", 1)[-1].endswith("." + domain) for domain in portal_domains)}
    return {"schema": "company-contact/v1", "source": source, "external_id": identity,
            "name": name, "inn": inn, "city": city, "address": address,
            "rubrics": list(dict.fromkeys(snapshot.get("categories") or [])) if source == "yandex" else [task["query"]],
            "emails": sorted(e for e in emails if "*" not in e),
            "websites": sorted(websites), "phones": sorted(phones), "source_url": card_url(source, url)}


class YandexCollector:
    def __init__(self, browser, delay=1, checkpoint=None, recovered=None, cache=None):
        self.browser = browser
        self.delay = delay
        self.checkpoint = checkpoint
        self.recovered = recovered or {}
        self.cache = cache or {}
        self.state = {"links": [], "discovery_finished": False, "stop_reason": "", "failed": {}}
        if checkpoint and checkpoint.exists():
            try:
                self.state.update(json.loads(checkpoint.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        self.progress = None
        self.count = len(self.recovered)
        self.metrics = {"recovered": self.count, "visited": 0, "cache_hits": 0,
                        "discovered": len(self.state["links"]), "scrolls": 0, "retries": 0}
        self.state["failed"] = {key: value for key, value in self.state["failed"].items() if key not in self.recovered}

    def save(self, phase):
        from .checkpoints import atomic_json
        self.state["metrics"] = self.metrics
        if self.checkpoint:
            atomic_json(self.checkpoint, self.state)
        if self.progress:
            self.progress(self.count, phase=phase, **self.metrics)

    @staticmethod
    def identity(url):
        match = re.search(r"/org/[^/]+/(\d+)", url)
        return match.group(1) if match else ""

    def search_snapshot(self):
        # Scope to search results: recommendations elsewhere on the page aren't the selection.
        result = self.browser.execute_script(r"""(()=>{
            const list=document.querySelector('.search-list-view__list,.search-list-view');
            let scroll=list;while(scroll&&scroll.scrollHeight<=scroll.clientHeight+10)scroll=scroll.parentElement;
            const text=list?.innerText||document.body?.innerText||'';
            return {title:document.title,url:location.href,bodyText:document.body?.innerText.slice(0,1500)||'',
                links:Array.from((list||document).querySelectorAll('a[href*="/org/"]')).map(a=>({href:a.href,text:a.innerText})),
                scroll:scroll&&{top:scroll.scrollTop,height:scroll.clientHeight,total:scroll.scrollHeight},
                loading:/Загрузка\.\.\.|Loading\.\.\./i.test(text.slice(-600))||Array.from((list||document).querySelectorAll('[aria-busy=true],.spinner')).some(e=>e.getClientRects().length),
                empty:/ничего не найдено|ничего не нашлось|нет результатов|no results/i.test(text),
                tail:text.slice(-500)};
        })()""")
        if not isinstance(result, dict):
            raise ValueError("Не удалось прочитать список организаций Яндекса")
        check_access(result)
        return result

    def collect_links(self, task: dict) -> tuple[list[str], bool]:
        if self.state["discovery_finished"]:
            return self.state["links"][:task["limit"]], self.state["stop_reason"] == "record_limit"
        self.browser.load(task["url"])
        links = {self.identity(url): url for url in self.state["links"]}
        stable = 0
        started = time.monotonic()
        previous_height = None
        while time.monotonic() - started < 180:
            snap = self.search_snapshot()
            before = len(links)
            for a in snap["links"]:
                candidate = card_url("yandex", a["href"])
                if candidate:
                    links.setdefault(self.identity(candidate), candidate)
            self.state["links"] = list(links.values())
            self.metrics["discovered"] = len(links)
            self.save("discovery")
            if len(links) >= task["limit"]:
                self.state.update(discovery_finished=True, stop_reason="record_limit")
                break
            if snap.get("empty") and not links:
                self.state.update(discovery_finished=True, stop_reason="empty_results")
                break
            scroll = snap.get("scroll")
            at_bottom = scroll and scroll["top"] + scroll["height"] >= scroll["total"] - 3
            unchanged = len(links) == before and scroll and scroll["total"] == previous_height
            stable = stable + 1 if unchanged and at_bottom and not snap.get("loading") else 0
            previous_height = scroll["total"] if scroll else None
            if links and stable >= 4:
                self.state.update(discovery_finished=True, stop_reason="visible_list_exhausted")
                break
            if not scroll and time.monotonic() - started > 15:
                self.state["stop_reason"] = "scroll_container_missing"
                break
            self.browser.execute_script("""(()=>{
                let e=document.querySelector('.search-list-view__list,.search-list-view');
                while(e&&e.scrollHeight<=e.clientHeight+10)e=e.parentElement;
                if(e)e.scrollTop=e.scrollHeight;
            })()""")
            self.metrics["scrolls"] += 1
            time.sleep(max(1.5, self.delay))
        if not self.state["stop_reason"]:
            self.state["stop_reason"] = "discovery_timeout"
        self.save("discovery_finished")
        if not links and self.state["stop_reason"] != "empty_results":
            raise ValueError("Карточки Яндекс Карт не загрузились. Очередь сохранена для повторного запуска.")
        return list(links.values())[:task["limit"]], self.state["stop_reason"] == "record_limit"

    def detail(self, url, task):
        self.browser.load(url)
        deadline = time.monotonic() + 12
        while True:
            snap = self.browser.snapshot()
            check_access(snap)
            if snap.get("h1") and self.identity(snap.get("url", "")) == self.identity(url):
                return extract_card("yandex", snap, task)
            if time.monotonic() >= deadline:
                raise ValueError("Карточка не отобразилась за 12 секунд")
            time.sleep(.5)

    def collect(self, task, output, progress):
        import os
        self.progress = lambda n, **info: progress(n, **info)
        # Old callers can still pass a simple one-argument callback.
        try:
            self.save("starting")
        except TypeError:
            self.progress = lambda n, **info: progress(n)
        links, capped = self.collect_links(task)
        for url in links:
            identity = self.identity(url)
            if identity in self.recovered:
                continue
            time.sleep(self.delay)
            record = self.cache.get(identity)
            if record:
                self.metrics["cache_hits"] += 1
            else:
                record = None
                for attempt in range(3):
                    try:
                        self.state["current_url"] = url
                        self.save("card")
                        record = self.detail(url, task)
                        self.metrics["visited"] += 1
                        break
                    except NeedsAccess:
                        self.state["challenge_url"] = url
                        self.save("needs_auth")
                        raise
                    except Exception as exc:
                        self.state["failed"][identity] = f"{type(exc).__name__}: {exc}"
                        self.save("retry")
                        if attempt < 2:
                            self.metrics["retries"] += 1
                            time.sleep(1 + attempt)
                if not record:
                    continue
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
            output.flush()
            try:
                os.fsync(output.fileno())
            except (AttributeError, OSError):
                pass  # In-memory outputs used by unit tests.
            self.recovered[identity] = record
            self.state["failed"].pop(identity, None)
            self.count += 1
            self.save("card_saved")
        errors = list(self.state["failed"].values())
        if not self.state["discovery_finished"]:
            errors.append("Список выдачи не завершён: " + self.state["stop_reason"])
        status = "partial" if errors else "limited" if capped else "done" if self.count else "empty"
        self.save("finished")
        return {"status": status, "count": self.count, "errors": errors,
                "stop_reason": self.state["stop_reason"], "metrics": self.metrics,
                "coverage": "Доступная выдача поискового запроса; не полный реестр всех компаний города"}


def next_page(snapshot: dict, current: str) -> str | None:
    source = "rusprofile" if "rusprofile" in urlsplit(current).netloc else "spark"
    page = parse_qs(urlsplit(current).query).get("page", ["1"])[0]
    number = int(page) if page.isdigit() else 1
    for link in snapshot.get("links", []):
        text = link.get("text", "").strip().lower()
        if link.get("rel") == "next" or text in ("следующая", "следующая страница", "далее", "next", "›", "»") or text == str(number + 1):
            try:
                candidate = source_url(source, link["href"])
            except ValueError:
                continue
            if candidate != current and not card_url(source, candidate):
                return candidate
    return None


class DirectoryCollector:
    def __init__(self, browser, source, delay=1):
        self.browser = browser
        self.source = source
        self.delay = delay

    def collect(self, task, output, progress):
        url = task["url"]
        count = 0
        visited = set()
        visited_pages = set()
        errors = []
        while url:
            self.browser.load(url)
            snapshot = self.browser.snapshot()
            check_access(snapshot)
            current = snapshot["url"]
            if current in visited_pages:
                errors.append("Повтор страницы выдачи; обход остановлен")
                break
            visited_pages.add(current)
            if direct := card_url(self.source, current):
                candidates = [direct]
                next_url = None
            else:
                candidates = list(dict.fromkeys(c for a in snapshot["links"] if (c := card_url(self.source, a["href"]))))
                next_url = next_page(snapshot, current)
            if not candidates:
                raise NeedsAccess(f'Не найдены доступные карточки {SOURCES[self.source]["name"]}. '
                                  'Выполните вход, откройте выборку и сохраните её адрес. Закрытый интерфейс может потребовать отдельной адаптации.')
            for candidate in candidates:
                if candidate in visited:
                    continue
                visited.add(candidate)
                if candidate != current:
                    time.sleep(self.delay)
                    self.browser.load(candidate)
                    detail = self.browser.snapshot()
                else:
                    detail = snapshot
                try:
                    record = extract_card(self.source, detail, task)
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                count += 1
                progress(count)
                if re.search(r"(?:контакт[а-я\s]{0,60}(?:подписк|после входа|платн)|войдите.{0,80}(?:контакт|почт))", detail.get("bodyText", ""), re.I):
                    raise NeedsAccess("Основные сведения сохранены, но контакты требуют входа или подходящего тарифа")
                if count >= task["limit"]:
                    return {"status": "limited", "count": count, "errors": errors}
            url = next_url
        # A saved SPA selection without a next link isn't proof that the whole selection was read.
        exhaustive = card_url(self.source, task["url"]) is not None
        if not exhaustive:
            errors.append("Доступные ссылки выборки пройдены; полнота закрытого интерфейса не подтверждена")
        return {"status": "done" if exhaustive and not errors else "partial", "count": count, "errors": errors}
