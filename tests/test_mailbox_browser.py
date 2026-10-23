from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest

from app import mailbox_client
from app.mailbox_client import (
    append_sent_copy,
    decode_modified_utf7,
    fetch_mailbox_message,
    list_mailbox_page,
)
from app.models import Inbox, SmtpAccount, SmtpMessage, SmtpThread
from app.routers import mailbox as mailbox_router


def _encode_modified_utf7(value: str) -> bytes:
    out = []
    non_ascii = []

    def flush():
        if not non_ascii:
            return
        raw = "".join(non_ascii).encode("utf-16-be")
        out.append("&" + base64.b64encode(raw).decode().rstrip("=").replace("/", ",") + "-")
        non_ascii.clear()

    for char in value:
        if " " <= char <= "~":
            flush()
            out.append("&-" if char == "&" else char)
        else:
            non_ascii.append(char)
    flush()
    return "".join(out).encode("ascii")


def _raw_message(uid: int, *, message_id: str | None = None, subject: str | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = f"Sender {uid} <sender{uid}@example.com>"
    message["To"] = "owner@example.com"
    message["Subject"] = subject or f"Subject {uid}"
    message["Message-ID"] = message_id or f"<message-{uid}@example.com>"
    message["Date"] = f"Thu, 22 Oct 2026 10:{uid:02d}:00 +0000"
    message.set_content(f"Body preview for message {uid}")
    return message.as_bytes()


class FakeImap:
    def __init__(self, *, duplicate=False):
        self.readonly = []
        self.appended = []
        self.duplicate = duplicate
        self.closed = False

    def list(self):
        return "OK", [
            b'(\\HasNoChildren \\Inbox) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "Sent"',
            b'(\\HasNoChildren \\Drafts) "/" "Drafts"',
        ]

    def select(self, mailbox, readonly=False):
        self.readonly.append((mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        command = command.casefold()
        if command == "search":
            if "HEADER" in args:
                return "OK", [b"2" if self.duplicate else b""]
            return "OK", [b"1 2"]
        if command == "fetch":
            uids = [int(value) for value in str(args[0]).split(",")]
            data = []
            for uid in uids:
                payload = _raw_message(uid)
                metadata = (
                    f'{uid} (UID {uid} FLAGS (\\Seen) INTERNALDATE '
                    f'"22-Oct-2026 10:{uid:02d}:00 +0000" RFC822.SIZE {len(payload)})'
                ).encode()
                data.append((metadata, payload))
            return "OK", data
        raise AssertionError((command, args))

    def append(self, mailbox, flags, internal_date, raw_message):
        self.appended.append((mailbox, flags, internal_date, raw_message))
        return "OK", [b"APPENDUID 1 3"]

    def logout(self):
        self.closed = True
        return "BYE", []


def _account() -> SmtpAccount:
    return SmtpAccount(
        inbox_id=1,
        smtp_host="smtp.example.com",
        smtp_username="owner@example.com",
        smtp_password="secret",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="owner@example.com",
        imap_password="secret",
        imap_use_ssl=True,
    )


def test_modified_utf7_and_russian_folder_role():
    encoded = _encode_modified_utf7("Отправленные")
    assert decode_modified_utf7(encoded) == "Отправленные"
    folder = mailbox_client._parse_folder_row(
        b'(\\HasNoChildren) "/" "' + encoded + b'"'
    )
    assert folder is not None
    assert folder.name == "Отправленные"
    assert folder.role == "sent"


def test_folder_page_is_readonly_and_paginated(monkeypatch):
    fake = FakeImap()
    monkeypatch.setattr(mailbox_client, "_imap_login", lambda account, **kwargs: fake)
    folders = mailbox_client.list_imap_folders(_account())
    inbox = next(folder for folder in folders if folder["role"] == "inbox")
    result = list_mailbox_page(_account(), folder_id=inbox["id"], page=1, page_size=1)
    assert result["total"] == 2
    assert result["total_pages"] == 2
    assert result["messages"][0]["message_key"] == "2"
    assert "Body preview" in result["messages"][0]["snippet"]
    assert fake.readonly[-1][1] is True


def test_sent_merges_local_and_server_without_message_id_duplicates(monkeypatch):
    fake = FakeImap()
    monkeypatch.setattr(mailbox_client, "_imap_login", lambda account, **kwargs: fake)
    folders = mailbox_client.list_imap_folders(_account())
    sent = next(folder for folder in folders if folder["role"] == "sent")
    local = [
        {
            "source": "local",
            "message_key": "local-duplicate",
            "message_id": "<message-2@example.com>",
            "subject": "duplicate",
            "from": "owner@example.com",
            "to": "recipient@example.com",
            "snippet": "local duplicate",
            "sort_timestamp": 1,
            "is_read": True,
        },
        {
            "source": "local",
            "message_key": "local-only",
            "message_id": "<local-only@example.com>",
            "subject": "local only",
            "from": "owner@example.com",
            "to": "recipient@example.com",
            "snippet": "local",
            "sort_timestamp": 2,
            "is_read": True,
        },
    ]
    result = list_mailbox_page(
        _account(), folder_id=sent["id"], page=1, page_size=50, local_sent=local
    )
    ids = [row["message_id"] for row in result["messages"]]
    assert result["total"] == 3
    assert ids.count("<message-2@example.com>") == 1
    assert "<local-only@example.com>" in ids


def test_full_message_uses_peek_and_readonly(monkeypatch):
    fake = FakeImap()
    monkeypatch.setattr(mailbox_client, "_imap_login", lambda account, **kwargs: fake)
    folders = mailbox_client.list_imap_folders(_account())
    inbox = next(folder for folder in folders if folder["role"] == "inbox")
    result = fetch_mailbox_message(_account(), folder_id=inbox["id"], uid=1)
    assert result["body_plain"].startswith("Body preview")
    assert result["message_id"] == "<message-1@example.com>"
    assert fake.readonly[-1][1] is True


def test_append_sent_copy_checks_duplicate_before_append(monkeypatch):
    new_fake = FakeImap(duplicate=False)
    monkeypatch.setattr(mailbox_client, "_imap_login", lambda account, **kwargs: new_fake)
    assert append_sent_copy(
        _account(), _raw_message(7), message_id="<message-7@example.com>",
        sent_at=datetime(2026, 10, 22, 10, 7, tzinfo=timezone.utc),
    ) is True
    assert len(new_fake.appended) == 1

    duplicate_fake = FakeImap(duplicate=True)
    monkeypatch.setattr(mailbox_client, "_imap_login", lambda account, **kwargs: duplicate_fake)
    assert append_sent_copy(
        _account(), _raw_message(7), message_id="<message-7@example.com>",
        sent_at=datetime(2026, 10, 22, 10, 7, tzinfo=timezone.utc),
    ) is True
    assert duplicate_fake.appended == []


@pytest.mark.asyncio
async def test_mail_api_lists_accounts_without_credentials(session):
    inbox = Inbox(email="owner@example.com", provider="smtp")
    session.add(inbox)
    await session.flush()
    session.add(
        SmtpAccount(
            inbox_id=inbox.id,
            smtp_host="smtp.example.com",
            smtp_username=inbox.email,
            smtp_password="secret",
            imap_host="imap.example.com",
            imap_username=inbox.email,
            imap_password="secret",
        )
    )
    await session.flush()
    payload = await mailbox_router.list_mail_accounts(db=session)
    assert payload["accounts"][0]["email"] == inbox.email
    assert "smtp_password" not in payload["accounts"][0]
    assert "imap_password" not in payload["accounts"][0]


@pytest.mark.asyncio
async def test_mail_api_adds_local_sent_folder_when_server_has_none(session, monkeypatch):
    inbox = Inbox(email="owner@example.com", provider="smtp")
    session.add(inbox)
    await session.flush()
    account = SmtpAccount(
        inbox_id=inbox.id,
        smtp_host="smtp.example.com",
        smtp_username=inbox.email,
        smtp_password="secret",
        imap_host="imap.example.com",
        imap_username=inbox.email,
        imap_password="secret",
    )
    session.add(account)
    thread = SmtpThread(inbox_id=inbox.id, thread_key="<root@example.com>")
    session.add(thread)
    session.add(
        SmtpMessage(
            inbox_id=inbox.id,
            message_id="local-one",
            thread_key=thread.thread_key,
            rfc_message_id="<one@example.com>",
            direction="sent",
            subject="Saved locally",
        )
    )
    await session.flush()

    async def fake_mail_thread(db, function, *args, **kwargs):
        return [{
            "id": "inbox-id", "name": "INBOX", "role": "inbox",
            "flags": [], "selectable": True, "source": "imap",
        }]

    monkeypatch.setattr(mailbox_router, "mail_thread", fake_mail_thread)
    result = await mailbox_router.list_folders(inbox.id, db=session)
    assert any(folder["id"] == "local-sent" for folder in result["folders"])
    assert result["default_folder_id"] == "inbox-id"
