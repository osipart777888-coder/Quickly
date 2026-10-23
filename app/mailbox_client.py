"""Read-only IMAP mailbox browser used by the local ``Mail`` page.

The campaign-oriented Unibox intentionally mirrors only messages needed for
reply processing.  This module is separate: it opens one explicitly selected
account and folder, uses ``BODY.PEEK`` with a read-only SELECT, and never
changes flags on the provider.
"""

from __future__ import annotations

import base64
import html
import imaplib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import formataddr, getaddresses, parsedate_to_datetime
from typing import Any, Iterable

from app.smtp_utils import _verified_ssl_context, normalise_message_id, smtp_timeout_seconds


log = logging.getLogger("quickly.mailbox")

PAGE_SIZE = 50
MAX_PAGE_SIZE = 50
LIST_PREVIEW_BYTES = 12 * 1024
MAX_FULL_MESSAGE_BYTES = 25 * 1024 * 1024
MAX_SENT_HEADER_SCAN = 20_000
MAX_SEARCH_FALLBACK_SCAN = 1_000


class MailboxError(RuntimeError):
    """Safe, user-facing mailbox error."""


@dataclass(frozen=True)
class ImapFolder:
    id: str
    name: str
    role: str
    flags: tuple[str, ...]
    selectable: bool
    wire_token: bytes


_LIST_RE = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+(?P<delimiter>NIL|\"(?:\\.|[^\"])*\")\s+(?P<name>.+)$"
)
_UID_RE = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)\b", re.IGNORECASE)
_DATE_RE = re.compile(rb'\bINTERNALDATE\s+"([^"]+)"', re.IGNORECASE)


def decode_modified_utf7(value: bytes) -> str:
    """Decode IMAP modified UTF-7 (RFC 3501 section 5.1.3)."""
    text = value.decode("ascii", errors="replace")
    out: list[str] = []
    index = 0
    while index < len(text):
        amp = text.find("&", index)
        if amp < 0:
            out.append(text[index:])
            break
        out.append(text[index:amp])
        end = text.find("-", amp)
        if end < 0:
            out.append(text[amp:])
            break
        token = text[amp + 1 : end]
        if not token:
            out.append("&")
        else:
            encoded = token.replace(",", "/")
            encoded += "=" * ((4 - len(encoded) % 4) % 4)
            try:
                out.append(base64.b64decode(encoded).decode("utf-16-be"))
            except (ValueError, UnicodeError):
                out.append(text[amp : end + 1])
        index = end + 1
    return "".join(out)


def _unquote_wire_token(token: bytes) -> bytes:
    token = token.strip()
    if len(token) >= 2 and token[:1] == b'"' and token[-1:] == b'"':
        token = token[1:-1]
        token = token.replace(b"\\\"", b'"').replace(b"\\\\", b"\\")
    return token


def _folder_id(wire_token: bytes) -> str:
    return base64.urlsafe_b64encode(wire_token).decode("ascii").rstrip("=")


def _folder_role(flags: Iterable[str], name: str) -> str:
    lowered_flags = {flag.casefold() for flag in flags}
    for flag, role in (
        ("\\inbox", "inbox"),
        ("\\sent", "sent"),
        ("\\drafts", "drafts"),
        ("\\junk", "spam"),
        ("\\spam", "spam"),
        ("\\trash", "trash"),
    ):
        if flag in lowered_flags:
            return role

    leaf = re.split(r"[/\\.]", name.casefold())[-1].strip()
    aliases = {
        "inbox": "inbox",
        "входящие": "inbox",
        "sent": "sent",
        "sent items": "sent",
        "sent mail": "sent",
        "отправленные": "sent",
        "исходящие": "sent",
        "drafts": "drafts",
        "черновики": "drafts",
        "spam": "spam",
        "junk": "spam",
        "нежелательная почта": "spam",
        "trash": "trash",
        "deleted": "trash",
        "deleted items": "trash",
        "корзина": "trash",
        "удаленные": "trash",
        "удалённые": "trash",
    }
    return aliases.get(leaf, "other")


def _parse_folder_row(row: bytes) -> ImapFolder | None:
    match = _LIST_RE.match(row.strip())
    if not match:
        return None
    flags = tuple(
        part.decode("ascii", errors="replace")
        for part in match.group("flags").split()
        if part
    )
    wire_token = match.group("name").strip()
    decoded_name = decode_modified_utf7(_unquote_wire_token(wire_token))
    role = _folder_role(flags, decoded_name)
    selectable = not any(flag.casefold() == "\\noselect" for flag in flags)
    return ImapFolder(
        id=_folder_id(wire_token),
        name=decoded_name,
        role=role,
        flags=flags,
        selectable=selectable,
        wire_token=wire_token,
    )


def _imap_login(account, *, timeout: float | None = None):
    timeout = timeout or smtp_timeout_seconds(default=20.0)
    client = None
    try:
        if account.imap_use_ssl:
            client = imaplib.IMAP4_SSL(
                account.imap_host,
                int(account.imap_port or 993),
                ssl_context=_verified_ssl_context(),
                timeout=timeout,
            )
        else:
            client = imaplib.IMAP4(
                account.imap_host,
                int(account.imap_port or 143),
                timeout=timeout,
            )
        client.login(account.imap_username or "", account.imap_password or "")
        return client
    except BaseException:
        if client is not None:
            try:
                client.shutdown()
            except Exception:
                pass
        raise


def _logout(client) -> None:
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        try:
            client.shutdown()
        except Exception:
            pass


def _list_folders(client) -> list[ImapFolder]:
    typ, data = client.list()
    if typ != "OK":
        raise MailboxError("Сервер IMAP не вернул список папок")
    folders: list[ImapFolder] = []
    for row in data or []:
        if not isinstance(row, (bytes, bytearray)):
            continue
        parsed = _parse_folder_row(bytes(row))
        if parsed is not None:
            folders.append(parsed)
    order = {"inbox": 0, "sent": 1, "drafts": 2, "spam": 3, "trash": 4, "other": 5}
    folders.sort(key=lambda item: (order.get(item.role, 9), item.name.casefold()))
    return folders


def list_imap_folders(account) -> list[dict[str, Any]]:
    client = _imap_login(account)
    try:
        return [
            {
                "id": folder.id,
                "name": folder.name,
                "role": folder.role,
                "flags": list(folder.flags),
                "selectable": folder.selectable,
                "source": "imap",
            }
            for folder in _list_folders(client)
        ]
    finally:
        _logout(client)


def _resolve_folder(client, folder_id: str) -> ImapFolder:
    if folder_id == "local-sent":
        return ImapFolder(
            id="local-sent",
            name="Отправленные",
            role="sent",
            flags=(),
            selectable=True,
            wire_token=b"",
        )
    for folder in _list_folders(client):
        if folder.id == folder_id:
            if not folder.selectable:
                raise MailboxError("Эту папку нельзя открыть")
            return folder
    raise MailboxError("Папка больше не доступна на сервере; обновите список папок")


def _select_readonly(client, folder: ImapFolder) -> int:
    typ, data = client.select(folder.wire_token, readonly=True)
    if typ != "OK":
        raise MailboxError(f"Не удалось открыть папку «{folder.name}» в режиме чтения")
    try:
        return int((data or [b"0"])[0] or 0)
    except (TypeError, ValueError):
        return 0


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _format_addresses(values: list[str]) -> str:
    result: list[str] = []
    for name, address in getaddresses(values):
        decoded = _decode_header_value(name).strip()
        result.append(formataddr((decoded, address)) if decoded else address)
    return ", ".join(item for item in result if item)


def _body_text(part) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else content.decode(errors="replace")
    except Exception:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, (bytes, bytearray)):
            return str(payload or "")
        charset = part.get_content_charset() or "utf-8"
        try:
            return bytes(payload).decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            return bytes(payload).decode("utf-8", errors="replace")


def _extract_bodies(message) -> tuple[str, str]:
    plain: list[str] = []
    rich: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or "").casefold() == "attachment":
            continue
        content_type = (part.get_content_type() or "").casefold()
        if content_type == "text/plain":
            plain.append(_body_text(part))
        elif content_type == "text/html":
            rich.append(_body_text(part))
    return "\n".join(plain).strip(), "\n".join(rich).strip()


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value or "")
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html.unescape(value)


def _message_datetime(message, metadata: bytes) -> datetime | None:
    raw = message.get("Date")
    if raw:
        try:
            value = parsedate_to_datetime(str(raw))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    match = _DATE_RE.search(metadata or b"")
    if match:
        try:
            value = datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
            return value.astimezone(timezone.utc)
        except (UnicodeError, ValueError):
            pass
    return None


def _metadata_uid(metadata: bytes) -> int | None:
    match = _UID_RE.search(metadata or b"")
    return int(match.group(1)) if match else None


def _message_from_payload(metadata: bytes, payload: bytes, *, include_body: bool) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(payload)
    plain, rich = _extract_bodies(message)
    preview_source = plain or _strip_html(rich)
    snippet = " ".join(preview_source.split())[:260]
    received_at = _message_datetime(message, metadata)
    flags = metadata.upper()
    uid = _metadata_uid(metadata)
    size_match = _SIZE_RE.search(metadata or b"")
    return {
        "source": "imap",
        "message_key": str(uid or ""),
        "uid": uid,
        "message_id": normalise_message_id(str(message.get("Message-ID", "") or "")),
        "subject": _decode_header_value(str(message.get("Subject", "") or "")),
        "from": _format_addresses([str(message.get("From", "") or "")]),
        "to": _format_addresses([str(message.get("To", "") or "")]),
        "cc": _format_addresses([str(message.get("Cc", "") or "")]),
        "date": received_at.isoformat().replace("+00:00", "Z") if received_at else None,
        "sort_timestamp": received_at.timestamp() if received_at else 0.0,
        "snippet": snippet,
        "body_plain": plain if include_body else "",
        "body_html": rich if include_body else "",
        "is_read": b"\\SEEN" in flags,
        "size": int(size_match.group(1)) if size_match else len(payload),
    }


def _fetch_payloads(client, uids: list[int], *, preview: bool) -> list[dict[str, Any]]:
    if not uids:
        return []
    uid_set = ",".join(str(uid) for uid in uids)
    expression = f"(BODY.PEEK[]<0.{LIST_PREVIEW_BYTES}> FLAGS INTERNALDATE RFC822.SIZE)" if preview else \
        "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM TO CC SUBJECT)] FLAGS INTERNALDATE RFC822.SIZE)"
    typ, data = client.uid("fetch", uid_set, expression)
    if typ != "OK":
        raise MailboxError("Сервер IMAP не вернул список писем")
    messages: list[dict[str, Any]] = []
    for part in data or []:
        if not (isinstance(part, tuple) and len(part) == 2):
            continue
        metadata, payload = part
        if not isinstance(metadata, (bytes, bytearray)) or not isinstance(payload, (bytes, bytearray)):
            continue
        row = _message_from_payload(bytes(metadata), bytes(payload), include_body=False)
        if row["uid"] is not None:
            messages.append(row)
    return messages


def _search_uids(client, query: str) -> tuple[list[int], bool]:
    query = (query or "").strip()
    if not query:
        typ, data = client.uid("search", None, "ALL")
        if typ != "OK":
            raise MailboxError("Сервер IMAP не выполнил поиск писем")
        raw = data[0] if data else b""
        return [int(value) for value in raw.split() if value.isdigit()], False

    if any(char in query for char in ("\r", "\n", "\x00")):
        raise MailboxError("Поисковая строка содержит недопустимые символы")
    escaped = query.replace("\\", "\\\\").replace('"', '\\"').encode("utf-8")
    quoted = b'"' + escaped + b'"'
    try:
        typ, data = client.uid("search", "CHARSET", "UTF-8", "TEXT", quoted)
        if typ == "OK":
            raw = data[0] if data else b""
            return [int(value) for value in raw.split() if value.isdigit()], False
    except (imaplib.IMAP4.error, UnicodeError):
        pass

    # Some providers reject UTF-8 SEARCH even though they store UTF-8 mail.
    # Fall back to a bounded recent scan and filter locally.
    typ, data = client.uid("search", None, "ALL")
    if typ != "OK":
        raise MailboxError("Сервер IMAP не выполнил поиск писем")
    raw = data[0] if data else b""
    all_uids = [int(value) for value in raw.split() if value.isdigit()]
    candidates = sorted(all_uids, reverse=True)[:MAX_SEARCH_FALLBACK_SCAN]
    rows = _fetch_payloads(client, candidates, preview=True)
    needle = query.casefold()
    matched = [
        int(row["uid"])
        for row in rows
        if needle in " ".join(
            [row.get("subject", ""), row.get("from", ""), row.get("to", ""), row.get("snippet", "")]
        ).casefold()
    ]
    return matched, len(all_uids) > len(candidates)


def _filter_local_rows(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    needle = (query or "").strip().casefold()
    if not needle:
        return list(rows)
    return [
        row
        for row in rows
        if needle in " ".join(
            [row.get("subject", ""), row.get("from", ""), row.get("to", ""), row.get("snippet", "")]
        ).casefold()
    ]


def _dedupe_sent(server: list[dict[str, Any]], local: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for row in server + local:
        message_id = normalise_message_id(row.get("message_id"))
        key = message_id.casefold() if message_id else f"{row.get('source')}:{row.get('message_key')}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda row: (float(row.get("sort_timestamp") or 0), str(row.get("message_key") or "")), reverse=True)
    return merged


def list_mailbox_page(
    account,
    *,
    folder_id: str,
    page: int,
    page_size: int = PAGE_SIZE,
    search: str = "",
    local_sent: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    page = max(1, int(page))
    page_size = max(1, min(MAX_PAGE_SIZE, int(page_size)))
    local_sent = local_sent or []

    if folder_id == "local-sent":
        merged = _dedupe_sent([], _filter_local_rows(local_sent, search))
        start = (page - 1) * page_size
        return {
            "folder": {"id": "local-sent", "name": "Отправленные", "role": "sent", "source": "local"},
            "messages": merged[start : start + page_size],
            "page": page,
            "page_size": page_size,
            "total": len(merged),
            "total_pages": max(1, (len(merged) + page_size - 1) // page_size),
            "search_limited": False,
            "sent_scan_limited": False,
        }

    client = _imap_login(account)
    try:
        folder = _resolve_folder(client, folder_id)
        _select_readonly(client, folder)
        uids, search_limited = _search_uids(client, search)
        descending = sorted(set(uids), reverse=True)

        if folder.role != "sent":
            total = len(descending)
            start = (page - 1) * page_size
            selected = descending[start : start + page_size]
            rows = _fetch_payloads(client, selected, preview=True)
            by_uid = {int(row["uid"]): row for row in rows}
            ordered = [by_uid[uid] for uid in selected if uid in by_uid]
            return {
                "folder": {"id": folder.id, "name": folder.name, "role": folder.role, "source": "imap"},
                "messages": ordered,
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "search_limited": search_limited,
                "sent_scan_limited": False,
            }

        scan_limited = len(descending) > MAX_SENT_HEADER_SCAN
        scanned_uids = descending[:MAX_SENT_HEADER_SCAN]
        server_headers: list[dict[str, Any]] = []
        for offset in range(0, len(scanned_uids), 500):
            server_headers.extend(_fetch_payloads(client, scanned_uids[offset : offset + 500], preview=False))
        local_rows = _filter_local_rows(local_sent, search)
        merged = _dedupe_sent(server_headers, local_rows)
        start = (page - 1) * page_size
        selected_rows = merged[start : start + page_size]

        selected_server_uids = [int(row["uid"]) for row in selected_rows if row.get("source") == "imap" and row.get("uid")]
        previews = _fetch_payloads(client, selected_server_uids, preview=True)
        preview_by_uid = {int(row["uid"]): row for row in previews}
        hydrated: list[dict[str, Any]] = []
        for row in selected_rows:
            if row.get("source") == "imap" and row.get("uid") in preview_by_uid:
                hydrated.append(preview_by_uid[int(row["uid"])])
            else:
                hydrated.append(row)

        return {
            "folder": {"id": folder.id, "name": folder.name, "role": folder.role, "source": "imap+local"},
            "messages": hydrated,
            "page": page,
            "page_size": page_size,
            "total": len(merged),
            "total_pages": max(1, (len(merged) + page_size - 1) // page_size),
            "search_limited": search_limited,
            "sent_scan_limited": scan_limited,
        }
    finally:
        _logout(client)


def fetch_mailbox_message(account, *, folder_id: str, uid: int) -> dict[str, Any]:
    if folder_id == "local-sent":
        raise MailboxError("Локальное письмо следует читать из базы Quickly")
    client = _imap_login(account)
    try:
        folder = _resolve_folder(client, folder_id)
        _select_readonly(client, folder)
        typ, metadata_rows = client.uid("fetch", str(int(uid)), "(RFC822.SIZE FLAGS INTERNALDATE)")
        if typ != "OK":
            raise MailboxError("Письмо не найдено на сервере")
        size = 0
        metadata = b""
        for part in metadata_rows or []:
            if isinstance(part, (bytes, bytearray)):
                metadata += bytes(part)
            elif isinstance(part, tuple) and part and isinstance(part[0], (bytes, bytearray)):
                metadata += bytes(part[0])
        size_match = _SIZE_RE.search(metadata)
        if size_match:
            size = int(size_match.group(1))
        if size > MAX_FULL_MESSAGE_BYTES:
            raise MailboxError("Письмо слишком большое для безопасного просмотра (более 25 МБ)")

        typ, data = client.uid("fetch", str(int(uid)), "(BODY.PEEK[] FLAGS INTERNALDATE RFC822.SIZE)")
        if typ != "OK":
            raise MailboxError("Письмо не найдено на сервере")
        for part in data or []:
            if isinstance(part, tuple) and len(part) == 2:
                meta, payload = part
                if isinstance(meta, (bytes, bytearray)) and isinstance(payload, (bytes, bytearray)):
                    row = _message_from_payload(bytes(meta), bytes(payload), include_body=True)
                    if int(row.get("uid") or 0) == int(uid):
                        row["folder_id"] = folder.id
                        row["folder_name"] = folder.name
                        return row
        raise MailboxError("Письмо не найдено на сервере")
    finally:
        _logout(client)


def _imap_search_message_id(client, message_id: str) -> bool:
    safe = normalise_message_id(message_id).replace("\\", "\\\\").replace('"', '\\"')
    typ, data = client.uid("search", None, "HEADER", "Message-ID", f'"{safe}"')
    return typ == "OK" and bool(data and (data[0] or b"").strip())


def append_sent_copy(account, raw_message: bytes, *, message_id: str, sent_at: datetime | None = None) -> bool:
    """Best-effort IMAP APPEND after an accepted SMTP send.

    The caller must ignore failures: SMTP acceptance remains authoritative.
    A Message-ID lookup prevents duplicate copies when the provider already
    stores outgoing SMTP mail automatically.
    """
    if not (getattr(account, "imap_host", "") or "").strip():
        return False
    client = _imap_login(account, timeout=min(10.0, smtp_timeout_seconds(default=10.0)))
    try:
        sent = next((folder for folder in _list_folders(client) if folder.role == "sent" and folder.selectable), None)
        if sent is None:
            return False
        _select_readonly(client, sent)
        if message_id and _imap_search_message_id(client, message_id):
            return True
        date_time = sent_at or datetime.now(timezone.utc)
        if date_time.tzinfo is None:
            date_time = date_time.replace(tzinfo=timezone.utc)
        internal_date = imaplib.Time2Internaldate(date_time)
        typ, _data = client.append(sent.wire_token, "(\\Seen)", internal_date, raw_message)
        return typ == "OK"
    finally:
        _logout(client)


def local_message_to_dict(row) -> dict[str, Any]:
    try:
        recipients = json.loads(row.to_addresses or "[]")
        to_value = ", ".join(recipients) if isinstance(recipients, list) else str(recipients or "")
    except (TypeError, ValueError):
        to_value = str(row.to_addresses or "")
    preview_source = row.body_plain or _strip_html(row.body_html or "")
    when = row.received_at or row.created_at
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return {
        "source": "local",
        "message_key": row.message_id,
        "uid": None,
        "message_id": normalise_message_id(row.rfc_message_id),
        "subject": row.subject or "",
        "from": row.from_address or "",
        "to": to_value,
        "cc": "",
        "date": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if when else None,
        "sort_timestamp": when.timestamp() if when else 0.0,
        "snippet": " ".join(preview_source.split())[:260],
        "body_plain": row.body_plain or "",
        "body_html": row.body_html or "",
        "is_read": True,
        "size": len((row.body_plain or row.body_html or "").encode("utf-8")),
    }
