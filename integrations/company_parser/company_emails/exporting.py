from __future__ import annotations

import csv
import json
from pathlib import Path

from .storage import Store, now
from .sources import SOURCES
from .websites import extract_emails

COMPANY_HEADERS = ["ID", "Источник", "ИНН", "Компания", "Город", "Регион", "Адрес", "Рубрики", "Поисковые сферы", "Телефоны", "Сайты", "Email", "Источники email", "Карточка источника"]
EMAIL_HEADERS = ["Email", "Компании", "Города", "Сферы", "Источники"]
TASK_HEADERS = ["Город", "Поисковый запрос", "Статус", "Карточек", "Лимит", "Причина остановки", "Найдено ссылок", "Восстановлено карточек", "Ошибки", "Поисковая ссылка", "Обновлено"]
SITE_HEADERS = ["Сайт", "Компании", "Статус", "Уникальных email", "Проверенные страницы", "Ошибки", "Обновлено"]
STATUSES = {"done": "Выдача пройдена", "limited": "Достигнут лимит", "empty": "Пустая выдача",
            "partial": "Неполный результат", "error": "Ошибка", "timeout": "Таймаут",
            "needs_auth": "Нужна проверка доступа", "interrupted": "Остановлено", "pending": "В очереди",
            "running": "Выполняется", "ok": "Проверен", "not_checked": "Ещё не проверен"}
STOP_REASONS = {"record_limit": "Достигнут заданный лимит", "visible_list_exhausted": "Доступный список закончился",
                "pagination_exhausted": "Доступные страницы выдачи закончились",
                "selection_exhausted": "Доступная выборка пройдена", "selection_unconfirmed": "Полнота выборки не подтверждена",
                "empty_results": "Нет результатов", "discovery_timeout": "Истекло время поиска ссылок",
                "scroll_container_missing": "Не найден список для прокрутки"}


def cell(value) -> str:
    text = str(value or "")
    # Files can be opened in Excel; external text must not become a formula.
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def write_csv(path: Path, headers: list, rows: list):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(headers)
        writer.writerows([cell(v) for v in row] for row in rows)
    temp.replace(path)


def export_results(store: Store, output: Path, xlsx: bool = True) -> dict:
    store.prune_invalid_emails()
    store.prune_invalid_websites()
    output.mkdir(parents=True, exist_ok=True)
    companies = []
    company_sites: dict[str, set[str]] = {}
    directory_access = {"spark": [], "rusprofile": []}
    unique: dict[str, dict[str, set]] = {}
    for record in store.db.execute("SELECT * FROM companies ORDER BY id"):
        item = json.loads(record["payload"])
        if item.get("source") in directory_access:
            status = {"locked": "Скрыты — нужен вход/тариф", "available": "Доступны в проверенной карточке",
                      "partial": "Частично доступны — часть полей скрыта",
                      "not_listed": "Раздел не найден", "imported": "Импортированы из выгрузки", "unknown": "Не проверены"}.get(item.get("contacts_status"), "Не проверены")
            directory_access[item["source"]].append([item["id"], item.get("inn", ""), item["name"], status, item.get("contacts_note", ""), item["source_url"]])
        queries = sorted({r[0] for r in store.db.execute("SELECT query FROM observations WHERE company_id=?", (item["id"],))})
        findings = list(store.db.execute("SELECT email,source_url FROM emails WHERE company_id=? ORDER BY email,source_url", (item["id"],)))
        emails = sorted({r[0] for r in findings})
        sources = sorted({r[1] for r in findings})
        for website in item["websites"]:
            company_sites.setdefault(website, set()).add(item["name"])
        companies.append([item["id"], SOURCES[item.get("source", "2gis")]["name"], item.get("inn", ""), item["name"], item["city"], item["region"], item["address"],
                          " | ".join(item["rubrics"]), " | ".join(queries), " | ".join(item["phones"]),
                          " | ".join(item["websites"]), " | ".join(emails), " | ".join(sources), item["source_url"]])
        for email, source in findings:
            aggregate = unique.setdefault(email, {k: set() for k in ("companies", "cities", "queries", "sources")})
            aggregate["companies"].add(item["name"])
            aggregate["cities"].add(item["city"])
            aggregate["queries"].update(queries)
            aggregate["sources"].add(source)
    email_rows = [[email] + [" | ".join(sorted(info[k])) for k in ("companies", "cities", "queries", "sources")]
                  for email, info in sorted(unique.items())]
    task_rows = []
    for row in store.db.execute("SELECT * FROM tasks ORDER BY rowid"):
        task, details = json.loads(row["payload"]), json.loads(row["details"])
        metrics = details.get("metrics", {})
        reason = details.get("stop_reason", "")
        task_rows.append([task["city"], task["query"], STATUSES.get(row["status"], row["status"]), row["count"],
                          task["limit"], STOP_REASONS.get(reason, reason), metrics.get("discovered", ""),
                          metrics.get("recovered", ""), row["message"], task["url"], row["updated"]])
    saved_sites = {r["url"]: r for r in store.db.execute("SELECT * FROM sites")}
    site_rows = []
    for url, names in sorted(company_sites.items()):
        row = saved_sites.get(url)
        result = json.loads(row["result"]) if row else {}
        site_rows.append([url, " | ".join(sorted(names)), STATUSES.get(result.get("status", "not_checked"), result.get("status")),
                          len({email for f in result.get("findings", []) for email in extract_emails(f[0])}), " | ".join(result.get("pages", [])),
                          " | ".join(result.get("errors", [])), row["updated"] if row else ""])
    write_csv(output / "companies.csv", COMPANY_HEADERS, companies)
    write_csv(output / "emails.csv", EMAIL_HEADERS, email_rows)
    write_csv(output / "tasks.csv", TASK_HEADERS, task_rows)
    write_csv(output / "sites.csv", SITE_HEADERS, site_rows)
    access_headers = ["ID", "ИНН", "Компания", "Доступ к контактам", "Пояснение", "Источник"]
    for source, rows in directory_access.items():
        if rows:
            write_csv(output / f"{source}_access.csv", access_headers, rows)
    if xlsx:
        import xlsxwriter
        path = output / "contacts.xlsx"
        temp = path.with_suffix(".tmp.xlsx")
        with xlsxwriter.Workbook(temp, {"strings_to_formulas": False, "strings_to_urls": False}) as book:
            header = book.add_format({"bold": True, "bg_color": "#1f3a4d", "font_color": "white"})
            tables = [("Компании", COMPANY_HEADERS, companies), ("Уникальные email", EMAIL_HEADERS, email_rows),
                      ("Задания", TASK_HEADERS, task_rows), ("Проверка сайтов", SITE_HEADERS, site_rows)]
            for source, rows in directory_access.items():
                if rows:
                    tables.append((SOURCES[source]["name"] + " — доступ", access_headers, rows))
            for name, headers, rows in tables:
                sheet = book.add_worksheet(name)
                sheet.write_row(0, 0, headers, header)
                for number, row in enumerate(rows, 1):
                    sheet.write_row(number, 0, row)
                sheet.freeze_panes(1, 0)
                sheet.autofilter(0, 0, len(rows), len(headers) - 1)
                sheet.set_column(0, len(headers) - 1, 27)
                sheet.set_column(3 if name == "Компании" else 1, 3 if name == "Компании" else 1, 40)
        temp.replace(path)
    report = {"exported_at": now(), **store.summary(),
              "coverage_note": "Доступные карточки выбранных источников, не полный реестр компаний России. limited — достигнут лимит; empty — пустой ответ; needs_auth — нужен вход/доступ; partial/error/timeout — неполный сбор."}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
