"""Checkpointed collection of visible company lists shared by directory providers."""
from __future__ import annotations

import json
import os
import re
import time

from .adapters import NeedsAccess, card_url, field_value
from .checkpoints import atomic_json
from .sources import source_url


class SelectionCollector:
    def __init__(self, browser, delay=1, checkpoint=None, recovered=None, *, adapter):
        self.adapter = adapter
        self.source, self.name, self.error = adapter.SOURCE, adapter.NAME, adapter.LayoutError
        self.browser, self.delay, self.checkpoint = browser, delay, checkpoint
        self.records = recovered or {}
        self.count = len(self.records)
        self.state = {"links": [], "pages": [], "next": None, "initialized": False, "failed": {}, "finished": False}
        if checkpoint and checkpoint.exists():
            try:
                self.state.update(json.loads(checkpoint.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                pass
        self.metrics = {"recovered": self.count, "visited": 0, "discovered": len(self.state["links"]), "pages": len(self.state["pages"]), "retries": 0}
        self.progress = None
        self._list_cache = None

    def save(self, phase):
        self.state["metrics"] = self.metrics
        if self.checkpoint:
            atomic_json(self.checkpoint, self.state)
        if self.progress:
            self.progress(self.count, phase=phase, **self.metrics)

    def read(self, url):
        self.browser.load(source_url(self.source, url))
        deadline = time.monotonic() + 12
        while True:
            snap = self.adapter.snapshot(self.browser)
            self.adapter.access_check(snap)
            if card_url(self.source, snap.get("url", "")):
                identified = field_value(snap, "taxID") or self.adapter.named_value(snap, {"инн"}) or re.search(
                    r"-inn-\d{10,12}\b|\bИНН\s*[:\n ]\s*\d{10,12}\b", snap.get("url", "") + "\n" + snap.get("text", ""), re.I)
                if self.adapter.company_name(snap) and identified:
                    return snap
            elif self.adapter.candidates(snap) or snap.get(self.source, {}).get("empty"):
                return snap
            if time.monotonic() >= deadline:
                raise self.error(f"Не распознаны карточки {self.name}. Войдите и сохраните адрес выборки; закрытый интерфейс ещё не подтверждён.")
            time.sleep(.5)

    def detail(self, url, task):
        self._list_cache = None
        snap = self.read(url)
        if self.adapter.contact_section(snap.get("text", "")):
            return self.adapter.extract(snap, task)
        # Open only a read-only contacts tab, never a portal's general contact page.
        for control in snap.get(self.source, {}).get("controls", []):
            href = control.get("href", "")
            if re.fullmatch(r"Контакты|Контактная информация|Контактные данные", control.get("label", ""), re.I) and not control.get("disabled"):
                if not href or href.startswith("#") or card_url(self.source, href) == card_url(self.source, snap["url"]):
                    if self.adapter.click(self.browser, control):
                        until = time.monotonic() + 8
                        while True:
                            time.sleep(.5)
                            snap = self.adapter.snapshot(self.browser)
                            self.adapter.access_check(snap)
                            if self.adapter.contact_section(snap.get("text", "")) or time.monotonic() >= until:
                                break
                    break
        return self.adapter.extract(snap, task)

    def load_cursor(self, cursor):
        if self._list_cache:
            previous, old = self._list_cache
            if cursor["url"] == previous["url"] and cursor.get("steps", 0) == previous.get("steps", 0) + 1:
                snap = self.adapter.advance(self.browser, old)
                self._list_cache = (dict(cursor), snap)
                return snap
        snap = self.read(cursor["url"])
        guard = self.state.get("selection_fingerprint", [])
        if guard and cursor["url"] == self.state.get("selection_url"):
            visible = [card_url(self.source, snap["url"])] if card_url(self.source, snap["url"]) else self.adapter.candidates(snap)
            if not set(guard).intersection(visible):
                raise self.error(f"Сохранённая выборка не совпала: фильтры могли сброситься. Откройте сохранённый список {self.name} через «Вход и выборка».")
        for _ in range(cursor.get("steps", 0)):
            snap = self.adapter.advance(self.browser, snap)
        self._list_cache = (dict(cursor), snap)
        return snap

    def collect(self, task, output, progress):
        self.progress = progress
        if self.state.get("task_id") not in (None, task["id"]):
            raise self.error("Сохранённая очередь относится к другой выборке")
        self.state["task_id"] = task["id"]
        self.state["selection_fingerprint"] = task.get("selection_fingerprint", [])
        self.state["selection_url"] = task["url"]
        if not self.state["initialized"]:
            self.state.update(initialized=True, next={"url": task["url"], "steps": 0})
        self.save("starting")
        completed = {key for key, record in self.records.items() if record.get("contacts_status") in ("available", "partial", "imported")}
        attempted = set()
        try:
            while len(completed) < task["limit"]:
                pending = [url for url in self.state["links"] if url not in attempted
                           and not any(r.get("source_url") == url and key in completed for key, r in self.records.items())]
                if self.state["next"] and len(self.state["links"]) < task["limit"]:
                    pending = []  # Discover consecutive pages while the list remains open.
                if not pending:
                    cursor = self.state["next"]
                    if not cursor:
                        break
                    self.save("discovery")
                    snap = self.load_cursor(cursor)
                    direct = card_url(self.source, snap["url"])
                    links = [direct] if direct else self.adapter.candidates(snap)
                    if not links:
                        if snap.get(self.source, {}).get("empty"):
                            self.state.update(next=None, finished=True)
                            break
                        raise self.error(f"Не найдены ссылки компаний в выборке {self.name}; возможен другой интерфейс кабинета")
                    fingerprint = list(links)
                    if fingerprint in self.state["pages"]:
                        raise self.error(f"{self.name} повторил страницу выдачи; сбор остановлен")
                    self.state["pages"].append(fingerprint)
                    self.state["links"] = list(dict.fromkeys(self.state["links"] + links))
                    control = self.adapter.next_control(snap) if not direct else None
                    if control:
                        href = control.get("href", "")
                        if href.startswith("https://") and href != snap["url"] and not card_url(self.source, href):
                            self.state["next"] = {"url": source_url(self.source, href), "steps": 0}
                        else:
                            self.state["next"] = {"url": cursor["url"], "steps": cursor.get("steps", 0) + 1}
                    else:
                        self.state["next"] = None
                        total = self.adapter.selection_total(snap)
                        self.state["finished"] = bool(direct or (total is not None and len(self.state["links"]) >= total))
                    self.metrics.update(discovered=len(self.state["links"]), pages=len(self.state["pages"]))
                    self.save("page_saved")
                    continue
                for url in pending:
                    if len(completed) >= task["limit"]:
                        break
                    attempted.add(url)
                    self.state["current_url"] = url
                    self.save("card")
                    record = None
                    for attempt in range(3):
                        try:
                            time.sleep(self.delay)
                            record = self.detail(url, task)
                            self.metrics["visited"] += 1
                            break
                        except NeedsAccess:
                            raise
                        except Exception as exc:
                            self.state["failed"][url] = f"{type(exc).__name__}: {exc}"
                            if attempt < 2:
                                self.metrics["retries"] += 1
                                self.save("retry")
                    if not record:
                        continue
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    output.flush()
                    try:
                        os.fsync(output.fileno())
                    except (AttributeError, OSError):
                        pass
                    self.records[record["external_id"]] = record
                    self.count = len(self.records)
                    self.state["failed"].pop(url, None)
                    self.save("card_saved")
                    if record["contacts_status"] == "locked":
                        raise NeedsAccess(f"Основные сведения сохранены, но контакты {self.name} скрыты. Войдите с доступом к контактам и продолжите: эта карточка будет прочитана повторно.")
                    if record["contacts_status"] == "not_listed":
                        raise self.error("Основные сведения сохранены, но раздел контактов не распознан. Доступ к контактам не подтверждён; пустой результат не считается успешным сбором.")
                    completed.add(record["external_id"])
        except NeedsAccess:
            self.save("needs_auth")
            raise
        errors = list(self.state["failed"].values())
        if not self.state["finished"] and len(completed) < task["limit"]:
            errors.append("Полнота выборки не подтверждена: нет доступного перехода или счётчика")
        reason = "record_limit" if len(completed) >= task["limit"] else "selection_exhausted" if self.state["finished"] else "selection_unconfirmed"
        self.state["stop_reason"] = reason
        self.save("finished")
        return {"status": "limited" if len(completed) >= task["limit"] else "partial" if errors else "done" if self.count else "empty",
                "count": self.count, "errors": errors, "stop_reason": reason, "metrics": self.metrics}
