"""Rusprofile visible cards and saved searches; no undocumented/private endpoints."""
from __future__ import annotations

import re
import sys
import time
from urllib.parse import unquote, urlsplit

from .adapters import NeedsAccess, card_url, check_access, field_value
from .selection import SelectionCollector
from .selection_dom import click, snapshot as visible_snapshot
from .sources import source_url
from .storage import contact_url
from .websites import extract_emails

SOURCE, NAME = "rusprofile", "Rusprofile"


class RusprofileLayoutError(Exception):
    pass


LayoutError = RusprofileLayoutError


def snapshot(browser):
    # Check server denial before interpreting a document as a company card.
    result = browser.snapshot()
    check_access(result)
    return visible_snapshot(browser, SOURCE, NAME, LayoutError, initial=result)


def access_check(snap):
    check_access(snap)
    if snap.get(SOURCE, {}).get("login_visible"):
        raise NeedsAccess("Открыта форма входа Rusprofile. Войдите через «Вход и выборка», затем продолжите сбор.")
    if re.search(r"/(?:login|signin|auth)(?:/|$)", urlsplit(snap.get("url", "")).path, re.I):
        raise NeedsAccess("Сессия Rusprofile отсутствует или истекла. Войдите в отдельном окне браузера.")


def named_value(snap, names):
    for row in snap.get(SOURCE, {}).get("labels", []):
        if re.sub(r"[\s:]+", " ", row.get("name", "")).strip().casefold() in names:
            return row.get("value", "").strip()
    return ""


def company_name(snap):
    value = named_value(snap, {"краткое наименование", "наименование организации"}) or field_value(snap, "name") or snap.get("h1", "").strip()
    if re.match(r"^(?:rusprofile|вход|авторизация|подбор компаний|поиск|403|ошибка)\b", value, re.I):
        return ""
    return value


def contact_section(text):
    blocks = []
    for match in re.finditer(r"(?:^|\n)\s*(?:Контакты|Контактная информация|Контактные данные)\s*[:\n]", text, re.I):
        block = re.split(r"\n\s*(?:Реквизиты|Основные сведения|Юридический адрес|Над[её]жность|Судебные дела|Арбитраж|Финансы|Бухгалтерская отч[её]тность|Виды деятельности|Связи|Связанные организации|Похожие компании|Краткая справка|Учредители|Руководитель|Госзакупки|Налоги и сборы|История изменений)\s*(?:\n|$)", text[match.end():], 1, re.I)[0][:10000]
        if re.search(r"телефон|почт|e-?mail|сайт|https?://|@|нет (?:сведений|данных)|контакт.*отсутств", block, re.I):
            blocks.append(block)
    return max(blocks, key=len) if blocks else ""


def extract(snap, task):
    access_check(snap)
    url = card_url(SOURCE, source_url(SOURCE, snap["url"]))
    if not url or not (name := company_name(snap)):
        raise LayoutError("Не распознана карточка компании Rusprofile")
    text = snap.get("text", "")
    inn = named_value(snap, {"инн"}) or field_value(snap, "taxID")
    if not re.fullmatch(r"\d{10}|\d{12}", inn):
        match = re.search(r"\bИНН\s*[:\n ]\s*(\d{12}|\d{10})\b", text, re.I)
        inn = match.group(1) if match else ""
    if not inn:
        raise LayoutError("В карточке Rusprofile не найден ИНН; запись не сохраняется")
    address = named_value(snap, {"юридический адрес", "адрес"}) or snap.get("address") or field_value(snap, "address")
    if not address:
        match = re.search(r"(?:^|\n)(?:Юридический адрес|Адрес)\s*[:\n]\s*([^\n]+)", text, re.I)
        address = match.group(1).strip() if match else ""
    section = contact_section(text)
    if not section:
        section = "\n".join(row["name"] + "\n" + row.get("value", "")
                            for row in snap.get(SOURCE, {}).get("labels", [])
                            if re.fullmatch(r"телефон(?:ы)?|сайт(?:ы)?|e-?mail|эл(?:ектронная|\.)?\s*почта", row.get("name", "").strip(), re.I))
    mask = r"[░▒▓█]{2,}|\*{2,}|[●•]{3,}"
    locked = bool(re.search(mask, section) or re.search(
        r"войдите|авторизуйтесь|подключ(?:ить|ите)|(?:получить|купить|нужен|требуется) доступ|подписк|тариф|контакты скрыты|показать контакты", section, re.I))
    emails = {email for email in extract_emails(section) if "*" not in email}
    websites, phones = set(), set()
    for link in snap.get("links", []):
        href, label = link.get("href", ""), link.get("text", "").strip()
        if link.get("inChrome") or re.search(mask, label) or not (link.get("inContacts") or (label and label in section)):
            continue
        if href.startswith("mailto:"):
            emails.update(email for email in extract_emails(unquote(href[7:].split("?", 1)[0])) if "*" not in email)
        elif href.startswith("tel:"):
            number = unquote(href[4:])
            if len(re.sub(r"\D", "", number)) in (10, 11):
                phones.add(number)
        elif not re.search(r"[░▒▓█●•*]", href) and (website := contact_url(href)):
            websites.add(website)
    for raw in re.findall(r"(?:https?://|www\.)[^\s<>\"'░▒▓█]+", section):
        if not re.search(r"[░▒▓█●•*]", raw) and (website := contact_url(raw.rstrip(".,;)"))):
            websites.add(website)
    for number in re.findall(r"(?:\+7|8)[\d\s()–—‒-]{9,25}\d", section):
        if len(re.sub(r"\D", "", number)) in (10, 11):
            phones.add(number.strip())
    emails = {email for email in emails if not (email.rsplit("@", 1)[-1] == "rusprofile.ru" or email.endswith(".rusprofile.ru"))}
    city_match = re.search(r"(?:^|,\s*)(?:г\.?\s+|город\s+)([^,\n]+)", address, re.I)
    kind = named_value(snap, {"основной вид деятельности", "основной вид деятельности по оквэд", "основной оквэд"})
    if not kind:
        match = re.search(r"(?:Основной вид деятельности(?: по ОКВЭД)?|Основной ОКВЭД)\s*[:\n]\s*([^\n]+)(?:\n(\d{2}(?:\.\d{1,2}){0,2})\b)?", text, re.I)
        kind = " ".join(filter(None, match.groups())) if match else ""
    # An IP's OGRN and a company's internal numeric ID are different namespaces.
    parts = urlsplit(url).path.strip("/").split("/")
    identity = parts[1] if parts[0] == "id" else "ip-" + parts[1]
    visible_contacts = bool(emails or websites or phones)
    status = "partial" if locked and visible_contacts else "locked" if locked else "available" if section else "not_listed"
    return {"schema": "company-contact/v1", "source": SOURCE, "external_id": identity, "inn": inn,
            "name": name, "address": address, "city": city_match.group(1).strip() if city_match else "",
            "region": named_value(snap, {"регион", "субъект рф"}), "rubrics": [kind] if kind else [],
            "emails": sorted(emails), "websites": sorted(websites), "phones": sorted(phones), "source_url": url,
            "contacts_status": status, "contacts_note": "Часть контактов скрыта; сохранены только видимые сведения" if status == "partial" else
            "Контакты скрыты; нужен вход или соответствующий доступ Rusprofile" if locked else ""}


def candidates(snap):
    return list(dict.fromkeys(url for link in snap.get("links", []) if not link.get("inChrome")
                              if (url := card_url(SOURCE, link.get("href", "")))))


def next_control(snap):
    return next((c for c in snap.get(SOURCE, {}).get("controls", []) if not c.get("disabled") and
                 (c.get("rel") == "next" or re.fullmatch(r"показать (?:ещ[её]|следующие)(?: \d+)?|загрузить ещ[её]|следующая(?: страница)?|далее|next(?: page)?|[›»→]", c.get("label", c.get("text", "")).strip(), re.I))), None)


def advance(browser, snap, timeout=15):
    control = next_control(snap)
    if not control or not click(browser, control):
        raise LayoutError("Не найден переход к следующей странице Rusprofile")
    before, deadline = candidates(snap), time.monotonic() + timeout
    while True:
        time.sleep(.5)
        newer = snapshot(browser)
        access_check(newer)
        if candidates(newer) and candidates(newer) != before:
            return newer
        if time.monotonic() >= deadline:
            raise LayoutError("Rusprofile не обновил выдачу после перехода; очередь сохранена")


def selection_total(snap):
    match = re.search(r"(?:найдено\s*(?:результатов|компаний|организаций)?\s*:?\s*|всего\s*:?\s*|показаны организации[^\n]+?\sиз\s+)([\d \t ]+)", snap.get("text", ""), re.I)
    return int(re.sub(r"\s", "", match.group(1))) if match and match.group(1).strip() else None


class RusprofileCollector(SelectionCollector):
    def __init__(self, browser, delay=1, checkpoint=None, recovered=None):
        super().__init__(browser, delay, checkpoint, recovered, adapter=sys.modules[__name__])


def probe(browser, url):
    collector = RusprofileCollector(browser)
    snap = collector.read(url)
    links = [card_url(SOURCE, snap["url"])] if card_url(SOURCE, snap["url"]) else candidates(snap)
    if not links:
        raise LayoutError("В выбранной странице Rusprofile нет распознанных компаний")
    record = collector.detail(links[0], {"city": "Россия", "query": "Проверка доступа"})
    return {"selection_ready": True, "companies_visible": len(links), "selection_fingerprint": links[:10],
            "contacts_status": record["contacts_status"], "verified": record["contacts_status"] in ("available", "partial"),
            "note": record["contacts_note"] or ("Раздел контактов прочитан в проверенной карточке; email может отсутствовать" if record["contacts_status"] == "available" else
                                               "Раздел контактов не распознан; доступ не подтверждён")}
