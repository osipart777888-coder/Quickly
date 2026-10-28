"""SPARK visible-page collector. Authentication stays in the user's dedicated Chrome."""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import unquote, urlsplit

from .adapters import NeedsAccess, card_url, check_access, field_value
from .sources import source_url
from .storage import contact_url
from .websites import extract_emails


class SparkLayoutError(Exception):
    pass


def snapshot(browser):
    from .selection_dom import snapshot as visible_snapshot
    return visible_snapshot(browser, "spark", "СПАРК", SparkLayoutError)


def access_check(snap):
    check_access(snap)
    if snap.get("spark", {}).get("login_visible"):
        raise NeedsAccess("Сессия СПАРК отсутствует или истекла. Войдите через пункт «Вход и выборка», затем продолжите сбор.")
    if snap.get("spark", {}).get("login_offer") and not card_url("spark", snap.get("url", "")) and not candidates(snap):
        raise NeedsAccess("Открыта страница входа СПАРК. Войдите и откройте сохранённую выборку компаний.")


def contact_section(text):
    candidates = []
    for match in re.finditer(r"(?:^|\n)\s*(?:Контакты|Контактная информация|Контактные данные)\s*[:\n]", text, re.I):
        tail = text[match.end():]
        block = re.split(r"\n\s*(?:Информация о компании|Учредители|Виды деятельности|Реквизиты|Полная информация|Связи компании|Описание)\s*(?:\n|$)", tail, 1, re.I)[0][:12000]
        if re.search(r"телефон|почт|e-?mail|сайт|https?://|@", block, re.I):
            candidates.append(block)
    return max(candidates, key=len) if candidates else ""


def named_value(snap, names):
    for row in snap.get("spark", {}).get("labels", []):
        name = re.sub(r"[\s:]+", " ", row.get("name", "")).strip().casefold()
        if name in names:
            return row.get("value", "").strip()
    return ""


def company_name(snap):
    name = named_value(snap, {"краткое наименование", "наименование организации", "наименование компании"}) or field_value(snap, "name") or snap.get("h1", "").strip()
    if name and not re.fullmatch(r"СПАРК|Поиск|Поиск компаний|Карточка компании|Сводка", name, re.I):
        return name
    return next((heading.strip() for heading in snap.get("spark", {}).get("headings", [])
                 if re.search(r"\bООО\b|\bПАО\b|\bАО\b|\bИП\b|адвокат|коллегия", heading, re.I)), "")


def extract(snap, task):
    access_check(snap)
    reference = source_url("spark", snap["url"])
    url = card_url("spark", reference)
    if not url:
        raise SparkLayoutError("Открыта не распознанная карточка СПАРК")
    text = snap.get("text", "")
    name = company_name(snap)
    inn = named_value(snap, {"инн"}) or field_value(snap, "taxID")
    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        match = re.search(r"(?:^|\n)ИНН\s*[:\n ]\s*(\d{12}|\d{10})\b", text, re.I)
        inn = match.group(1) if match else ""
    from_url = re.search(r"-inn-(\d{12}|\d{10})\b", url)
    if from_url and inn and from_url.group(1) != inn:
        raise SparkLayoutError("ИНН открытой карточки не совпадает с её ссылкой")
    inn = inn or (from_url.group(1) if from_url else "")
    if not inn:
        raise SparkLayoutError("В карточке СПАРК не найден ИНН компании; запись не сохраняется")
    if not name or re.search(r"^(?:вход|авторизация|поиск|спарк)\b", name, re.I):
        raise SparkLayoutError("В карточке не найдено название компании")
    address = named_value(snap, {"адрес", "юридический адрес", "местонахождение"}) or snap.get("address") or field_value(snap, "address")
    if not address:
        match = re.search(r"(?:^|\n)(?:Юридический адрес|Адрес)\s*[:\n]\s*([^\n]+(?:\n[^\n]+)?)", text, re.I)
        address = match.group(1).strip() if match else ""
    section = contact_section(text)
    if not section:
        contact_rows = [row for row in snap.get("spark", {}).get("labels", [])
                        if re.fullmatch(r"телефон(?:ы)?|сайт(?:ы)?|e-?mail|эл(?:ектронная|\.)?\s*почта", row.get("name", "").strip(), re.I)]
        section = "\n".join(row["name"] + "\n" + row.get("value", "") for row in contact_rows)
    masked = bool(re.search(r"[░▒▓█]{2,}|\*{2,}|[●•]{3,}", section))
    locked = masked or bool(re.search(r"(?:контакт|почт)[\s\S]{0,160}(?:войдите|авторизац|нет доступа|подписк|тариф|ограничен)|(?:войдите|авториз)[\s\S]{0,100}(?:контакт|почт)", section, re.I))
    emails = {e for e in extract_emails(section) if "*" not in e}
    websites, phones = set(), set()
    for link in snap.get("links", []):
        if link.get("inChrome"):
            continue
        href = link.get("href", "")
        label = link.get("text", "").strip()
        scoped = bool(link.get("inContacts") or (label and label in section))
        if not scoped:
            continue
        if href.startswith("mailto:"):
            emails.update(e for e in extract_emails(href[7:].split("?", 1)[0]) if "*" not in e)
        elif href.startswith("tel:"):
            value = unquote(href[4:])
            if len(re.sub(r"\D", "", value)) >= 10:
                phones.add(value)
        elif not re.search(r"[░▒▓█●•*]", href) and (website := contact_url(href)):
            websites.add(website)
    for raw in re.findall(r"(?:https?://|www\.)[^\s<>\"'░▒▓█]+", section):
        if not re.search(r"[░▒▓█●•*]", raw) and (website := contact_url(raw.rstrip(".,;)"))):
            websites.add(website)
    for number in re.findall(r"(?:\+7|8)[\d\s()–—‒-]{9,25}\d", section):
        if len(re.sub(r"\D", "", number)) in (10, 11):
            phones.add(number.strip())
    emails = {e for e in emails if not e.rsplit("@", 1)[-1].endswith(("interfax.ru", "spark-interfax.ru", "spark-interfax.com"))}
    city = ""
    match = re.search(r"(?:^|,\s*)(?:г\.?\s+|город\s+)([^,\n]+)", address, re.I)
    if match:
        city = match.group(1).strip()
    categories = []
    kind = re.search(r"(?:Основной вид деятельности(?: по ОКВЭД)?|Основной ОКВЭД)\s*[:\n]\s*(\d{2}(?:\.\d{1,2}){0,2})\s*\n([^\n]+)", text, re.I)
    if kind:
        categories = [kind.group(1) + " " + kind.group(2).strip()]
    return {"schema": "company-contact/v1", "source": "spark", "external_id": inn or hashlib.sha256(url.encode()).hexdigest()[:24],
            "inn": inn, "name": name, "address": address, "city": city, "region": named_value(snap, {"регион", "субъект рф"}),
            "rubrics": categories, "emails": sorted(emails), "websites": sorted(websites), "phones": sorted(phones), "source_url": url,
            "contacts_status": "partial" if locked and emails else "locked" if locked else "available" if section else "not_listed",
            "contacts_note": "Часть полей скрыта; сохранены только видимые контакты" if locked and emails else
                "Контакты частично или полностью скрыты; нужен вход или доступ к разделу" if locked else ""}


def candidates(snap):
    return list(dict.fromkeys(url for link in snap.get("links", []) if not link.get("inChrome")
                              if (url := card_url("spark", link.get("href", "")))))


def signature(snap):
    return tuple(candidates(snap))


def next_control(snap):
    for control in snap.get("spark", {}).get("controls", []):
        label = control.get("label", control.get("text", "")).strip()
        if not control.get("disabled") and (control.get("rel") == "next" or re.fullmatch(
                r"показать (?:еще|ещё)|загрузить (?:еще|ещё)|следующая(?: страница)?|далее|next(?: page)?|[›»→]", label, re.I)):
            return control
    return None


def click(browser, control):
    from .selection_dom import click as visible_click
    return visible_click(browser, control)


def advance(browser, snap, timeout=15):
    control = next_control(snap)
    if not control or not click(browser, control):
        raise SparkLayoutError("Не найден рабочий переход к следующей странице СПАРК")
    before, deadline = signature(snap), time.monotonic() + timeout
    while True:
        time.sleep(.5)
        newer = snapshot(browser)
        access_check(newer)
        if signature(newer) and signature(newer) != before:
            return newer
        if time.monotonic() >= deadline:
            raise SparkLayoutError("После перехода СПАРК не обновил список; очередь сохранена")


def probe(browser, url):
    collector = SparkCollector(browser)
    snap = collector.read(url)
    links = [card_url("spark", snap["url"])] if card_url("spark", snap["url"]) else candidates(snap)
    if not links:
        raise SparkLayoutError("В выбранной странице пока нет распознанных компаний")
    record = collector.detail(links[0], {"city": "Россия", "query": "Проверка доступа"})
    return {"selection_ready": True, "companies_visible": len(links), "contacts_status": record["contacts_status"],
            "selection_fingerprint": links[:10], "verified": record["contacts_status"] in ("available", "partial"), "note": record["contacts_note"] or
            ("Контакты доступны в проверенной карточке" if record["contacts_status"] == "available" else "В карточке не найден раздел контактов; доступ не подтверждён")}


SOURCE = "spark"
NAME = "СПАРК"
LayoutError = SparkLayoutError


def selection_total(snap):
    match = re.search(r"(?:найдено результатов|всего|найдено компаний)\s*[:\s]\s*([\d \t ]+)", snap.get("text", ""), re.I)
    return int(re.sub(r"\s", "", match.group(1))) if match else None


from .selection import SelectionCollector


class SparkCollector(SelectionCollector):
    def __init__(self, browser, delay=1, checkpoint=None, recovered=None):
        import sys
        super().__init__(browser, delay, checkpoint, recovered, adapter=sys.modules[__name__])
