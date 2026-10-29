"""Mailbox retry storms stay visible as one incident, with preserved history."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models import EmailNotificationConfig, Inbox, Notification, SmtpSyncState, User
from app.notification_groups import (
    consolidate_legacy_imap_notifications, mailbox_sync_summary,
    resolve_imap_failure,
)
from app.notifications import dispatch_notification, get_unread_notification_count
from app.routers.notifications import list_notifications, mark_all_read


async def owner(db, name="owner"):
    user = User(username=name, email=f"{name}@example.com", password_hash="x", is_active=True)
    db.add(user)
    await db.flush()
    return user


def failure(inbox, kind="imap_sync_failed"):
    return {"inbox_id": inbox.id, "inbox_email": inbox.email, "provider": "smtp", "error_type": kind}


@pytest.mark.asyncio
async def test_500_retries_across_50_accounts_create_one_notice_and_one_email(session, monkeypatch):
    user = await owner(session)
    session.add(EmailNotificationConfig(user_id=user.id, enabled=True, events=[], rate_limit_per_hour=100))
    inboxes = [Inbox(email=f"box-{i}@example.com", provider="smtp") for i in range(50)]
    session.add_all(inboxes)
    await session.flush()
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr("app.notifications._send_notification_for_user", deliver)
    for _ in range(10):
        for inbox in inboxes:
            await dispatch_notification(session, "token_expired", failure(inbox))
    assert await session.scalar(select(func.count(Notification.id))) == 1
    assert await get_unread_notification_count(session, user.id) == 1
    result = await list_notifications(user=user, db=session, unread_only=False, limit=50, offset=0)
    assert result.total == result.unread == 1
    summary = result.items[0].sync_summary
    assert summary.active_count == 50
    assert len(summary.mailboxes) == 50
    assert summary.occurrences == 500
    assert {m.occurrences for m in summary.mailboxes} == {10}
    assert deliver.await_count == 1


@pytest.mark.asyncio
async def test_read_retry_recovery_restart_and_new_incident(session, monkeypatch):
    user = await owner(session)
    inbox = Inbox(email="box@example.com", provider="smtp")
    session.add(inbox)
    await session.flush()
    now = datetime(2026, 1, 1, 12)
    monkeypatch.setattr("app.time.utcnow", lambda: now)
    await dispatch_notification(session, "token_expired", failure(inbox))
    await mark_all_read(user=user, db=session)
    await dispatch_notification(session, "token_expired", failure(inbox, "imap_auth_failed"))
    assert await get_unread_notification_count(session, user.id) == 0
    inbox_id = inbox.id
    await session.commit()
    session.expire_all()
    row = await session.scalar(select(Notification))
    assert row.data_json["occurrences"] == 2
    assert row.data_json["active_count"] == 1
    await resolve_imap_failure(session, inbox_id)
    assert mailbox_sync_summary(row)["active_count"] == 0
    assert row.read_at is not None
    # A quick transient relapse updates the same read card without nagging.
    inbox = await session.scalar(select(Inbox))
    await dispatch_notification(session, "token_expired", failure(inbox))
    assert await get_unread_notification_count(session, user.id) == 0
    await resolve_imap_failure(session, inbox.id)
    now += timedelta(days=2)
    await dispatch_notification(session, "token_expired", failure(inbox))
    assert await get_unread_notification_count(session, user.id) == 1
    assert await session.scalar(select(func.count(Notification.id))) == 1


@pytest.mark.asyncio
async def test_legacy_compaction_preserves_events_and_respects_successful_sync(session):
    user = await owner(session)
    good = Inbox(email="recovered@example.com", provider="smtp")
    bad = Inbox(email="offline@example.com", provider="smtp")
    session.add_all([good, bad])
    await session.flush()
    at = datetime(2026, 1, 1)
    session.add(SmtpSyncState(inbox_id=good.id, last_sync_at=at + timedelta(hours=1)))
    old = []
    for i, inbox in enumerate([good, bad, good]):
        row = Notification(user_id=user.id, event_type="token_expired", title="old", message="old",
                           inbox_id=inbox.id, data_json=failure(inbox), created_at=at + timedelta(minutes=i))
        old.append(row)
        session.add(row)
    await dispatch_notification(session, "lead.replied", {"lead_email": "lead@example.com"})
    await dispatch_notification(session, "token_expired", {"provider": "smtp", "error_type": "smtp_auth_failed"})
    await session.flush()
    original_payloads = [dict(n.data_json) for n in old]
    assert await consolidate_legacy_imap_notifications(session) == 3
    assert await consolidate_legacy_imap_notifications(session) == 0
    assert await session.scalar(select(func.count(Notification.id))) == 6  # no deletion
    for row, original in zip(old, original_payloads):
        assert {k: v for k, v in row.data_json.items() if k != "superseded_by"} == original
        assert row.read_at is None  # historical read state preserved
    result = await list_notifications(user=user, db=session, unread_only=False, limit=50, offset=0)
    assert result.total == result.unread == 3
    summary = next(n.sync_summary for n in result.items if n.sync_summary)
    assert summary.occurrences == 3
    assert summary.active_count == 1
    assert {m.status for m in summary.mailboxes} == {"recovered", "connection_error"}
    await mark_all_read(user=user, db=session)
    assert await get_unread_notification_count(session, user.id) == 0
    unread = await list_notifications(user=user, db=session, unread_only=True, limit=50, offset=0)
    assert unread.items == []
    assert all(n.read_at is None for n in old)


@pytest.mark.asyncio
async def test_recovered_legacy_storm_has_no_unread_badge_and_is_user_scoped(session):
    first = await owner(session)
    second = await owner(session, "second")
    inbox = Inbox(email="restored@example.com", provider="smtp")
    session.add(inbox)
    await session.flush()
    at = datetime(2026, 1, 1)
    session.add(SmtpSyncState(inbox_id=inbox.id, last_sync_at=at + timedelta(hours=1)))
    for user in (first, second):
        session.add(Notification(user_id=user.id, event_type="token_expired", title="old", message="old",
                                 inbox_id=inbox.id, data_json=failure(inbox), created_at=at))
    await session.flush()
    await consolidate_legacy_imap_notifications(session)
    assert await get_unread_notification_count(session, first.id) == 0
    assert await get_unread_notification_count(session, second.id) == 0
    for user in (first, second):
        response = await list_notifications(user=user, db=session, unread_only=False, limit=50, offset=0)
        assert response.total == 1
        assert response.items[0].sync_summary.active_count == 0
        assert response.items[0].title == "Синхронизация почты восстановлена"
