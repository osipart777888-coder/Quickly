"""Public 2GIS search pages and durable per-card continuation."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .adapters import NeedsAccess, check_access
from .checkpoints import atomic_json
from .sources import source_url


class CollectionError(Exception):
    pass


def identity(item):
    return str(item.get("id", "")).split("_")[0]


class TwoGisCollector:
    def __init__(self, browser, delay=1, checkpoint: Path | None = None, recovered=None):
        self.browser, self.delay, self.checkpoint = browser, delay, checkpoint
        self.recovered = recovered or {}
        self.state = {"links": [], "pages": [], "signatures": [], "excluded": [], "failed": {},
                      "next_url": None, "initialized": False, "advertised_total": None, "stop_reason": ""}
        if checkpoint and checkpoint.exists():
            try:
                self.state.update(json.loads(checkpoint.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass  # Flushed JSONL still prevents reopening saved cards.
        self.state["failed"] = {key: val for key, val in self.state["failed"].items() if key not in self.recovered}
        self.count = len(self.recovered)
        self.metrics = {"recovered": self.count, "visited": 0, "discovered": len(self.state["links"]),
                        "pages": len(self.state["pages"]), "retries": 0, "excluded": len(self.state["excluded"]), "ui_transitions": 0}
        self.progress = None

    def save(self, phase):
        self.state["metrics"] = self.metrics
        if self.checkpoint:
            atomic_json(self.checkpoint, self.state)
        if self.progress:
            self.progress(self.count, phase=phase, **self.metrics)

    def navigate(self, url):
        self.browser.load(source_url("2gis", url))
        deadline = time.monotonic() + 15
        while True:
            visible = self.browser.execute_script("({title:document.title,bodyText:document.body?.innerText.slice(0,2000)||''})")
            check_access(visible or {})
            state = self.browser.execute_script("window.initialState")
            if isinstance(state, dict) and isinstance(state.get("data"), dict):
                return state
            if time.monotonic() >= deadline:
                raise CollectionError("Не найдены данные открытой страницы 2ГИС; возможна смена интерфейса")
            time.sleep(.5)

    def search_view(self):
        view = self.browser.execute_script("""(()=>({
            url:location.href,title:document.title,bodyText:document.body?.innerText.slice(0,2000)||'',
            firms:Array.from(document.querySelectorAll('a[href*="/firm/"]')).filter(a=>a.getClientRects().length).map(a=>a.href),
            pages:Array.from(document.querySelectorAll('a[href*="/page/"]')).filter(a=>a.getClientRects().length).map(a=>a.href)
        }))()""")
        if not isinstance(view, dict):
            raise CollectionError("Не удалось прочитать ссылки выдачи 2ГИС")
        check_access(view)
        return view

    @staticmethod
    def page_base(url):
        return re.sub(r"/page/\d+/?$", "", unquote(urlsplit(url).path)).rstrip("/")

    def open_search(self, url):
        state = self.navigate(url)
        view = self.search_view()
        wanted = unquote(urlsplit(url).path).rstrip("/")
        current = unquote(urlsplit(view.get("url", url)).path).rstrip("/")
        client_navigation = False
        deadline = time.monotonic() + 120
        # 2GIS server navigation can reset deep pages to page 1; follow the public UI links.
        while current != wanted:
            if self.page_base(view.get("url", url)) != self.page_base(url):
                raise CollectionError("2ГИС открыл другую поисковую выборку")
            target_number = int(re.search(r"/page/(\d+)", wanted).group(1)) if "/page/" in wanted else 1
            current_number = int(re.search(r"/page/(\d+)", current).group(1)) if "/page/" in current else 1
            choices = []
            for candidate in view["pages"]:
                candidate = urljoin(url, candidate)
                match = re.search(r"/page/(\d+)", urlsplit(candidate).path)
                if match and self.page_base(candidate) == self.page_base(url) and current_number < int(match.group(1)) <= target_number:
                    choices.append((int(match.group(1)), candidate))
            if not choices or time.monotonic() >= deadline:
                raise CollectionError("Не найден доступный переход к нужной странице 2ГИС")
            _, target = max(choices)
            before = view["firms"]
            clicked = self.browser.execute_script("""(()=>{const target=""" + json.dumps(target) + """;
                const a=Array.from(document.querySelectorAll('a[href*="/page/"]')).find(a=>a.href===target);
                if(!a)return false;a.click();return true})()""")
            if not clicked:
                raise CollectionError("Кнопка страницы 2ГИС исчезла")
            until = time.monotonic() + 20
            while True:
                time.sleep(.5)
                view = self.search_view()
                current = unquote(urlsplit(view.get("url", "")).path).rstrip("/")
                if current == unquote(urlsplit(target).path).rstrip("/") and view["firms"] and view["firms"] != before:
                    break
                if time.monotonic() >= until:
                    raise CollectionError("2ГИС не загрузил выбранную страницу после нажатия")
            self.metrics["ui_transitions"] += 1
            client_navigation = True
        return state, view, client_navigation

    def search_page(self, url):
        state, view, client_navigation = self.open_search(url)
        data = state["data"]
        ids = list(dict.fromkeys(match.group(1) for link in view["firms"] if (match := re.search(r"/firm/(\d+)", link))))
        profiles = {identity(p["data"]): p["data"] for p in data.get("entity", {}).get("profile", {}).values()
                    if isinstance(p.get("data"), dict) and identity(p["data"])}
        # After a client-side transition initialState still describes page 1.
        # New visible IDs are read from the DOM; categories are verified in each full card.
        items = [profiles.get(key, {"id": key}) for key in ids]
        current = re.search(r"/page/(\d+)", urlsplit(url).path)
        number = int(current.group(1)) if current else 1
        searches = [p["data"] for p in data.get("search", {}).get("profile", {}).values() if p.get("data")]
        search = next((p for p in searches if p.get("currentPage") == number), searches[0] if searches else {})
        if not client_navigation and search.get("currentPage") is not None and search["currentPage"] != number:
            raise CollectionError("2ГИС открыл другой номер страницы выдачи")
        total = search.get("total")
        if total == 0:
            return [], None, 0
        if not items:
            raise CollectionError("Карточки выдачи не найдены: возможна блокировка или изменение сайта")
        base = self.page_base(url)
        next_url = None
        for candidate in view["pages"]:
            candidate = urljoin(url, candidate)
            path = unquote(urlsplit(candidate).path).rstrip("/")
            if path == f"{base}/page/{number + 1}":
                next_url = source_url("2gis", candidate)
                break
        page_count = search.get("pages")
        if not next_url and ((page_count or 0) > number or (not page_count and not client_navigation and search.get("hasPagesToLoad"))):
            raise CollectionError("Есть ещё результаты, но ссылка на следующую страницу 2ГИС не найдена")
        return items, next_url, total

    def detail(self, url, expected_id):
        state = self.navigate(url)
        for profile in state["data"].get("entity", {}).get("profile", {}).values():
            item = profile.get("data", {})
            if identity(item) == expected_id and (item.get("name") or item.get("name_ex", {}).get("primary")):
                return item
        raise CollectionError(f"Карточка {expected_id} не получена")

    def retry(self, operation, phase):
        for attempt in range(3):
            try:
                return operation()
            except NeedsAccess:
                raise
            except Exception:
                if attempt == 2:
                    raise
                self.metrics["retries"] += 1
                self.save(phase)
                time.sleep(1 + attempt)

    def collect(self, task, output, progress):
        self.progress = progress
        try:
            self.save("starting")
        except TypeError:
            self.progress = lambda n, **info: progress(n)
        if self.state.get("task_id") not in (None, task["id"]):
            raise CollectionError("Сохранённая очередь относится к другому заданию")
        self.state["task_id"] = task["id"]
        if not self.state["initialized"]:
            self.state.update(initialized=True, next_url=task["url"])
        attempted = set()
        excluded = set(self.state["excluded"])
        try:
            while self.count < task["limit"]:
                pending = [key for key in self.state["links"] if key not in self.recovered and key not in attempted and key not in excluded]
                if not pending:
                    page_url = self.state["next_url"]
                    if not page_url:
                        break
                    self.state["current_url"] = page_url
                    self.save("discovery")
                    items, next_url, total = self.retry(lambda: self.search_page(page_url), "retry")
                    signature = [identity(item) for item in items]
                    if signature and signature in self.state["signatures"]:
                        raise CollectionError("2ГИС повторил страницу выдачи; сбор остановлен с сохранением результата")
                    self.state["signatures"].append(signature)
                    self.state["pages"].append(page_url)
                    self.state["advertised_total"] = total if total is not None else self.state["advertised_total"]
                    self.state["next_url"] = next_url
                    for brief in items:
                        key = identity(brief)
                        if not key or key in self.state["links"]:
                            continue
                        # A rubric is the actual category from 2GIS, including for adverts.
                        rubric_ids = {str(r.get("id")) for r in brief.get("rubrics") or []}
                        if task.get("rubric") and rubric_ids and task["rubric"] not in rubric_ids:
                            excluded.add(key)
                        else:
                            self.state["links"].append(key)
                    self.state["excluded"] = sorted(excluded)
                    self.metrics.update(discovered=len(self.state["links"]), pages=len(self.state["pages"]), excluded=len(excluded))
                    self.save("page_saved")
                    continue
                for key in pending:
                    if self.count >= task["limit"]:
                        break
                    attempted.add(key)
                    time.sleep(self.delay)
                    url = f'https://2gis.ru/{task["city_code"]}/firm/{key}'
                    self.state["current_url"] = url
                    self.save("card")
                    try:
                        detail = self.retry(lambda: self.detail(url, key), "retry")
                        self.metrics["visited"] += 1
                    except NeedsAccess:
                        raise
                    except Exception as exc:
                        self.state["failed"][key] = f"{type(exc).__name__}: {exc}"
                        self.save("card_failed")
                        continue
                    if task.get("rubric") and task["rubric"] not in {str(r.get("id")) for r in detail.get("rubrics") or []}:
                        excluded.add(key)
                        self.state["excluded"] = sorted(excluded)
                        self.metrics["excluded"] = len(excluded)
                        self.state["failed"].pop(key, None)
                        self.save("card_excluded")
                        continue
                    output.write(json.dumps(detail, ensure_ascii=False) + "\n")
                    output.flush()
                    try:
                        os.fsync(output.fileno())
                    except (OSError, AttributeError):
                        pass
                    self.recovered[key] = detail
                    self.state["failed"].pop(key, None)
                    self.count += 1
                    self.save("card_saved")
        except NeedsAccess:
            self.state["challenge_url"] = self.state.get("current_url", task["url"])
            self.save("needs_auth")
            raise
        errors = list(self.state["failed"].values())
        self.state["stop_reason"] = "record_limit" if self.count >= task["limit"] else "pagination_exhausted"
        self.save("finished")
        return {"status": "limited" if self.count >= task["limit"] else "partial" if errors else "done" if self.count else "empty",
                "count": self.count, "errors": errors, "advertised_total": self.state["advertised_total"],
                "stop_reason": self.state["stop_reason"], "metrics": self.metrics,
                "coverage": "Доступные страницы выбранного проекта 2ГИС; не полный реестр компаний города"}
