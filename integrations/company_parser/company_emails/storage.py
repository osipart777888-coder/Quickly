from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def contact_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if ":" in value and "://" not in value:
        return None
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host or parts.username or parts.password:
            return None
        # A social profile is not the company's own website.
        for domain in ("vk.com", "ok.ru", "t.me", "telegram.me", "wa.me", "youtube.com",
                       "instagram.com", "facebook.com", "max.ru", "dzen.ru", "rutube.ru", "2gis.ru", "yandex.ru", "yandex.com", "spark-interfax.ru", "spark-interfax.com", "rusprofile.ru"):
            if host == domain or host.endswith("." + domain):
                return None
        query = urlencode([(key, val) for key, val in parse_qsl(parts.query)
                           if not key.lower().startswith("utm_") and key.lower() not in ("yclid", "gclid", "fbclid", "roistat")])
        scheme = "https" if parts.scheme == "http" and parts.port is None else parts.scheme
        return parts._replace(scheme=scheme, fragment="", query=query).geturl().rstrip("/")
    except ValueError:
        return None


def normalize_item(item: dict, task: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Карточка не является объектом")
    if item.get("schema") == "company-contact/v1":
        return normalize_contact(item, task)
    identity = str(item.get("id", "")).split("_")[0]
    if not identity:
        raise ValueError("В карточке нет ID")
    contacts = [c for group in item.get("contact_groups") or [] for c in group.get("contacts") or []]
    websites = sorted({url for c in contacts if c.get("type") == "website"
                       if (url := contact_url(c.get("url") or c.get("value", "")))})
    from .websites import extract_emails
    emails = sorted({email for c in contacts if c.get("type") == "email"
                     for email in extract_emails(c.get("value") or "")})
    adm = item.get("adm_div") or []
    city = next((x.get("name", "") for x in adm if x.get("type") == "city"), task["city"])
    region = next((x.get("name", "") for x in adm if x.get("type") == "region"), "")
    return {"id": identity, "source": "2gis", "inn": "", "name": item.get("name") or (item.get("name_ex") or {}).get("primary", ""),
            "city": city, "region": region, "country": task.get('country', 'Россия'), "country_code": task.get('country_code', 'ru'), "address": item.get("address_name", ""),
            "rubrics": [r.get("name", "") for r in item.get("rubrics") or []],
            "phones": sorted({c["value"] for c in contacts if c.get("type") == "phone" and c.get("value")}),
            "websites": websites, "emails": emails,
            "source_url": f'https://2gis.{task.get("domain", "ru")}/{task["city_code"]}/firm/{identity}'}


def normalize_contact(item: dict, task: dict) -> dict:
    from .sources import source_url
    from .websites import extract_emails
    source = task.get("source", item.get("source"))
    if source not in ("yandex", "spark", "rusprofile") or not item.get("external_id") or not item.get("name"):
        raise ValueError("Неполная карточка источника")
    reference = source_url(source, item["source_url"]) if item.get("source_url") else ""
    if not reference and task.get("import_file"):
        reference = f'{task["import_file"]}#row={item.get("origin_row", "")}'
    if not reference:
        raise ValueError("Не указан источник контактов")
    result = {"id": f'{source}:{item["external_id"]}', "source": source, "inn": str(item.get("inn", "")),
            "name": item["name"], "country": task.get('country', 'Россия'), "country_code": task.get('country_code', 'ru'), "city": item.get("city") or (task["city"] if task["city"] != "Россия" else ""), "region": item.get("region", ""),
            "address": item.get("address", ""), "rubrics": item.get("rubrics", []),
            "phones": sorted(set(item.get("phones", []))),
            "websites": sorted({u for v in item.get("websites", []) if (u := contact_url(v))}),
            "emails": sorted({e for v in item.get("emails", []) for e in extract_emails(v) if "*" not in e}),
            "source_url": reference}
    if source in ("spark", "rusprofile"):
        result["contacts_status"] = item.get("contacts_status", "imported" if task.get("import_file") else "unknown")
        result["contacts_note"] = item.get("contacts_note", "")
    return result


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.db = sqlite3.connect(directory / "contacts.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                count INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', updated TEXT);
            CREATE TABLE IF NOT EXISTS companies (id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                company_id TEXT, task_id TEXT, query TEXT, city TEXT,
                PRIMARY KEY (company_id, task_id));
            CREATE TABLE IF NOT EXISTS emails (
                company_id TEXT, email TEXT, source_url TEXT, kind TEXT, found_at TEXT,
                PRIMARY KEY (company_id, email, source_url));
            CREATE TABLE IF NOT EXISTS sites (url TEXT PRIMARY KEY, result TEXT NOT NULL, updated TEXT NOT NULL);
        """)
        if "details" not in {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
        self.db.commit()

    def close(self):
        self.db.close()

    def add_tasks(self, tasks: list[dict]):
        with self.db:
            for task in tasks:
                self.db.execute("INSERT OR IGNORE INTO tasks(id,payload,updated) VALUES(?,?,?)",
                                (task["id"], json.dumps(task, ensure_ascii=False), now()))

    def mark(self, task_id: str, status: str, count: int = 0, message: str = "", details=None):
        with self.db:
            self.db.execute("UPDATE tasks SET status=?,count=?,message=?,updated=?,details=COALESCE(?,details) WHERE id=?",
                            (status, count, message, now(), json.dumps(details, ensure_ascii=False) if details is not None else None, task_id))

    def task_count(self, task_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM observations WHERE task_id=?", (task_id,)).fetchone()[0]

    def import_tail(self, task: dict, path: Path, offset: int = 0) -> tuple[int, int]:
        if not path.exists():
            return offset, 0
        documents = []
        with path.open("rb") as file:
            file.seek(min(offset, path.stat().st_size))
            while True:
                start = file.tell()
                line = file.readline()
                if not line or not line.endswith(b"\n"):
                    offset = start
                    break
                offset = file.tell()
                try:
                    documents.append(json.loads(line))
                except (ValueError, UnicodeError):
                    continue
        return offset, self.import_documents(task, documents)

    def import_items(self, task: dict, path: Path) -> int:
        if not path.exists():
            return 0
        documents = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                documents.append(json.loads(line))
            except ValueError:
                continue
        return self.import_documents(task, documents)

    def import_documents(self, task, documents) -> int:
        count = 0
        with self.db:
            for document in documents:
                try:
                    item = normalize_item(document, task)
                except (ValueError, TypeError, KeyError):
                    continue  # A killed worker may leave an incomplete final line.
                current_emails = item["emails"][:]
                existing = self.db.execute("SELECT payload FROM companies WHERE id=?", (item["id"],)).fetchone()
                if existing:
                    old = json.loads(existing[0])
                    for field in ("websites", "emails", "phones", "rubrics"):
                        item[field] = sorted(set(old.get(field, [])) | set(item[field]))
                    item["websites"] = sorted({url for candidate in item["websites"] if (url := contact_url(candidate))})
                self.db.execute("INSERT OR REPLACE INTO companies VALUES(?,?,?)",
                                (item["id"], json.dumps(item, ensure_ascii=False), now()))
                self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?)",
                                (item["id"], task["id"], task["query"], task["city"]))
                self.add_emails(item["id"], [(email, item["source_url"]) for email in current_emails], item.get("source", "2gis"))
                count += 1
        return count

    def add_emails(self, company_id: str, findings: list[tuple[str, str]], kind: str):
        for email, source in findings:
            self.db.execute("INSERT OR IGNORE INTO emails VALUES(?,?,?,?,?)", (company_id, email, source, kind, now()))

    def prune_invalid_emails(self):
        from .websites import extract_emails
        # Apply improved validation to earlier runs too, preserving raw source observations.
        with self.db:
            for email, in self.db.execute("SELECT DISTINCT email FROM emails").fetchall():
                if email not in extract_emails(email):
                    self.db.execute("DELETE FROM emails WHERE email=?", (email,))

    def prune_invalid_websites(self):
        # Old runs can contain social profiles previously supplied as "website" by a directory.
        with self.db:
            for row in self.db.execute("SELECT id,payload FROM companies").fetchall():
                item = json.loads(row["payload"])
                websites = sorted({url for candidate in item.get("websites", []) if (url := contact_url(candidate))})
                if websites != item.get("websites", []):
                    item["websites"] = websites
                    self.db.execute("UPDATE companies SET payload=? WHERE id=?", (json.dumps(item, ensure_ascii=False), row["id"]))

    def summary(self) -> dict:
        return {"companies": self.db.execute("SELECT COUNT(*) FROM companies").fetchone()[0],
                "unique_emails": self.db.execute("SELECT COUNT(DISTINCT email) FROM emails").fetchone()[0],
                "companies_with_email": self.db.execute("SELECT COUNT(DISTINCT company_id) FROM emails").fetchone()[0],
                "tasks": {r[0]: r[1] for r in self.db.execute("SELECT status,COUNT(*) FROM tasks GROUP BY status")},
                "sources": self.source_summary(),
                "sites": self.site_summary()}

    def source_summary(self) -> dict:
        summary: dict[str, int] = {}
        for row in self.db.execute("SELECT payload FROM companies"):
            source = json.loads(row[0]).get("source", "2gis")
            summary[source] = summary.get(source, 0) + 1
        return summary

    def site_summary(self) -> dict:
        summary: dict[str, int] = {}
        current = {site for row in self.db.execute("SELECT payload FROM companies")
                   for site in json.loads(row[0]).get("websites", [])}
        for row in self.db.execute("SELECT url,result FROM sites"):
            if row["url"] not in current:
                continue  # Keep historical cache entries without counting them as current company sites.
            state = json.loads(row["result"]).get("status", "unknown")
            summary[state] = summary.get(state, 0) + 1
        return summary
