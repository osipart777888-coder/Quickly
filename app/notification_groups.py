"""One persistent IMAP incident summary per user; original events remain archived."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import select

from app import time as time_provider
from app.models import Inbox, Notification, SmtpSyncState, User

GROUP = "imap_sync"
IMAP_ERRORS = {"imap_sync_failed", "imap_auth_failed"}


def visible_notification():
    return Notification.data_json["superseded_by"].as_integer().is_(None)


def is_imap_failure(event_type: str, data: dict) -> bool:
    return event_type == "token_expired" and data.get("error_type") in IMAP_ERRORS


def _active(mailbox: dict) -> bool:
    return not mailbox.get("recovered_at") and not mailbox.get("removed")


def _add_failure(payload: dict, data: dict, at: datetime) -> None:
    stamp = at.isoformat(timespec="microseconds")
    email = str(data.get("inbox_email") or "Неизвестный ящик")
    key = str(data.get("inbox_id") or email.lower())
    mailboxes = payload.setdefault("mailboxes", {})
    entry = mailboxes.setdefault(key, {
        "inbox_id": data.get("inbox_id"), "email": email,
        "first_failed_at": stamp, "occurrences": 0,
    })
    entry["occurrences"] += 1
    if stamp >= entry.get("last_failed_at", ""):
        entry.update(last_failed_at=stamp, error_type=data["error_type"])
        entry.pop("recovered_at", None)
        entry.pop("removed", None)
    payload["occurrences"] = payload.get("occurrences", 0) + 1
    payload["last_failed_at"] = max(stamp, payload.get("last_failed_at", ""))


def _render(row: Notification, payload: dict) -> None:
    mailboxes = list(payload.get("mailboxes", {}).values())
    active = sum(_active(m) for m in mailboxes)
    payload["active_count"] = active
    row.title = "Проблемы синхронизации почты" if active else "Синхронизация почты восстановлена"
    row.message = (
        f"Сейчас требуют внимания: {active}. Ящиков в истории: {len(mailboxes)}. "
        f"Сбоев зафиксировано: {payload.get('occurrences', 0)}. "
        "Повторные ошибки объединяются в эту сводку."
    )
    row.data_json = payload  # assign a fresh JSON object so SQLAlchemy persists changes
    row.inbox_id = None
    if not active and row.read_at is None:
        row.read_at = time_provider.utcnow()


async def _locked_summary(db, user_id: int) -> Notification | None:
    # Lock the owner even before the first summary exists. Different mailbox
    # syncs/processes cannot both insert a summary or lose another mailbox update.
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    return await db.scalar(select(Notification).where(
        Notification.user_id == user_id,
        Notification.data_json["notification_group"].as_string() == GROUP,
    ).order_by(Notification.id.desc()).limit(1).with_for_update())


async def record_imap_failure(db, user_id: int, data: dict) -> tuple[Notification, bool]:
    """Update the summary; return whether this incident warrants an email alert."""
    now = time_provider.utcnow()
    row = await _locked_summary(db, user_id)
    created = row is None
    if created:
        row = Notification(user_id=user_id, event_type="token_expired", title="", message="")
        db.add(row)
    payload = deepcopy(row.data_json or {"notification_group": GROUP})
    was_recovered = not any(_active(m) for m in payload.get("mailboxes", {}).values())
    last_alert = payload.get("last_alert_at")
    alert = created or (was_recovered and (
        not last_alert or now - datetime.fromisoformat(last_alert) >= timedelta(days=1)
    ))
    _add_failure(payload, data, now)
    if alert:
        payload["last_alert_at"] = now.isoformat()
        row.read_at = None
    _render(row, payload)
    await db.flush()
    return row, alert


async def resolve_imap_failure(db, inbox_id: int) -> None:
    """A successful read resolves an existing issue without creating a new alert."""
    user_ids = (await db.scalars(select(Notification.user_id).where(
        Notification.data_json["notification_group"].as_string() == GROUP,
    ).distinct().order_by(Notification.user_id))).all()
    for user_id in user_ids:
        row = await _locked_summary(db, user_id)
        if row is None:
            continue
        payload = deepcopy(row.data_json or {})
        entry = payload.get("mailboxes", {}).get(str(inbox_id))
        if entry and _active(entry):
            entry["recovered_at"] = time_provider.utcnow().isoformat()
            _render(row, payload)
    await db.flush()


async def consolidate_legacy_imap_notifications(db) -> int:
    """Idempotently group old IMAP alerts; keep every original payload and read state."""
    user_ids = (await db.scalars(select(Notification.user_id).where(
        Notification.event_type == "token_expired", visible_notification(),
        Notification.data_json["error_type"].as_string().in_(IMAP_ERRORS),
    ).distinct().order_by(Notification.user_id))).all()
    archived = 0
    inboxes = (await db.execute(select(Inbox.id, Inbox.email, SmtpSyncState.last_sync_at)
        .outerjoin(SmtpSyncState, SmtpSyncState.inbox_id == Inbox.id))).all()
    by_id = {i.id: i for i in inboxes}
    by_email = {i.email.lower(): i for i in inboxes}
    for user_id in user_ids:
        summary = await _locked_summary(db, user_id)
        old = (await db.scalars(select(Notification).where(
            Notification.user_id == user_id, visible_notification(),
            Notification.event_type == "token_expired",
            Notification.data_json["error_type"].as_string().in_(IMAP_ERRORS),
        ).order_by(Notification.created_at, Notification.id))).all()
        if not old:
            continue
        created = summary is None
        if created:
            summary = Notification(user_id=user_id, event_type="token_expired", title="", message="")
            db.add(summary)
        payload = deepcopy(summary.data_json or {"notification_group": GROUP})
        for row in old:
            data = dict(row.data_json or {})
            inbox = by_id.get(row.inbox_id) or by_email.get(str(data.get("inbox_email") or "").lower())
            data["inbox_id"] = inbox.id if inbox else row.inbox_id
            data["inbox_email"] = data.get("inbox_email") or (inbox.email if inbox else "Неизвестный ящик")
            _add_failure(payload, data, row.created_at)
        for mailbox in payload["mailboxes"].values():
            inbox = by_id.get(mailbox.get("inbox_id"))
            if inbox is None:
                mailbox["removed"] = True
            elif inbox.last_sync_at and inbox.last_sync_at > datetime.fromisoformat(mailbox["last_failed_at"]):
                mailbox["recovered_at"] = inbox.last_sync_at.isoformat()
        if created:
            summary.created_at = old[-1].created_at
            summary.read_at = None if any(n.read_at is None for n in old) else time_provider.utcnow()
            payload["last_alert_at"] = old[-1].created_at.isoformat()
        _render(summary, payload)
        await db.flush()
        for row in old:
            row.data_json = {**row.data_json, "superseded_by": summary.id}
            archived += 1
        await db.flush()
    return archived


def mailbox_sync_summary(row: Notification) -> dict | None:
    payload = row.data_json or {}
    if payload.get("notification_group") != GROUP:
        return None
    mailboxes = []
    for entry in payload.get("mailboxes", {}).values():
        status = ("removed" if entry.get("removed") else "recovered" if entry.get("recovered_at")
                  else "auth_error" if entry.get("error_type") == "imap_auth_failed" else "connection_error")
        mailboxes.append({key: entry.get(key) for key in (
            "email", "inbox_id", "last_failed_at", "recovered_at", "occurrences"
        )} | {"status": status})
    return {
        "active_count": payload.get("active_count", 0),
        "occurrences": payload.get("occurrences", 0),
        "mailboxes": sorted(mailboxes, key=lambda m: (m["status"] in {"recovered", "removed"}, m["email"])),
    }
