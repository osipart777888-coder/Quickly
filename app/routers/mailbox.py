"""Authenticated read-only APIs for browsing one IMAP mailbox at a time."""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.mail_io import mail_thread
from app.mailbox_client import (
    MailboxError,
    fetch_mailbox_message,
    list_imap_folders,
    list_mailbox_page,
    local_message_to_dict,
)
from app.models import Inbox, SmtpAccount, SmtpMessage
from app.smtp_utils import sanitize_connection_error
from app.time import utcnow


log = logging.getLogger("quickly.mailbox.router")
router = APIRouter(prefix="/api/mail", tags=["mail"])

_mailbox_status: dict[int, dict] = {}


class RefreshRequest(BaseModel):
    folder_id: str | None = Field(default=None, max_length=2048)


def _iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_mail_error(exc: Exception) -> str:
    if isinstance(exc, MailboxError):
        return str(exc)
    raw = str(exc or "")
    low = raw.casefold()
    if any(token in low for token in ("auth", "login", "credential", "invalid user", "invalid password")):
        return "Ошибка авторизации IMAP — проверьте логин и пароль"
    category = sanitize_connection_error(raw)
    translations = {
        "Connection timed out": "Истекло время ожидания подключения к IMAP",
        "Connection refused": "IMAP-сервер отклонил подключение",
        "TLS/certificate error": "Ошибка TLS-сертификата IMAP-сервера",
        "DNS resolution failed — check the hostname": "Не удалось найти IMAP-сервер",
    }
    return translations.get(category, "Не удалось прочитать почтовый ящик")


def _mark_ok(inbox_id: int) -> dict:
    value = {
        "status": "ok",
        "error": "",
        "last_refreshed_at": _iso(utcnow()),
    }
    _mailbox_status[inbox_id] = value
    return value


def _mark_error(inbox_id: int, exc: Exception) -> str:
    safe = _safe_mail_error(exc)
    _mailbox_status[inbox_id] = {
        "status": "error",
        "error": safe,
        "last_refreshed_at": _iso(utcnow()),
    }
    log.warning("Mailbox read failed inbox_id=%s category=%s", inbox_id, safe)
    return safe


async def _smtp_context(db: AsyncSession, inbox_id: int) -> tuple[Inbox, SmtpAccount]:
    row = (
        await db.execute(
            select(Inbox, SmtpAccount)
            .join(SmtpAccount, SmtpAccount.inbox_id == Inbox.id)
            .where(Inbox.id == inbox_id, Inbox.provider == "smtp")
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Почтовый ящик не найден")
    inbox, account = row
    if not (account.imap_host or "").strip():
        raise HTTPException(400, "Для этого аккаунта не настроен IMAP")
    return inbox, account


async def _local_sent(db: AsyncSession, inbox_id: int) -> list[dict]:
    rows = (
        await db.execute(
            select(SmtpMessage)
            .where(SmtpMessage.inbox_id == inbox_id, SmtpMessage.direction == "sent")
            .order_by(SmtpMessage.received_at.desc(), SmtpMessage.created_at.desc())
        )
    ).scalars().all()
    return [local_message_to_dict(row) for row in rows]


@router.get("/accounts")
async def list_mail_accounts(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(Inbox, SmtpAccount)
            .join(SmtpAccount, SmtpAccount.inbox_id == Inbox.id)
            .where(Inbox.provider == "smtp")
            .order_by(func.lower(Inbox.email))
        )
    ).all()
    return {
        "accounts": [
            {
                "inbox_id": inbox.id,
                "email": inbox.email,
                "display_name": inbox.display_name or "",
                "imap_configured": bool((account.imap_host or "").strip()),
                "connection_test_ok": bool(account.last_test_ok),
                "connection_tested_at": _iso(account.last_tested_at),
                "sync": _mailbox_status.get(
                    inbox.id,
                    {"status": "idle", "error": "", "last_refreshed_at": None},
                ),
            }
            for inbox, account in rows
        ]
    }


async def _folders_response(db: AsyncSession, inbox: Inbox, account: SmtpAccount) -> dict:
    try:
        folders = await mail_thread(
            db,
            list_imap_folders,
            account,
            mail_inbox_id=inbox.id,
        )
        local_sent_count = await db.scalar(
            select(func.count())
            .select_from(SmtpMessage)
            .where(SmtpMessage.inbox_id == inbox.id, SmtpMessage.direction == "sent")
        )
        if local_sent_count and not any(folder.get("role") == "sent" for folder in folders):
            folders.append(
                {
                    "id": "local-sent",
                    "name": "Отправленные",
                    "role": "sent",
                    "flags": [],
                    "selectable": True,
                    "source": "local",
                }
            )
        status = _mark_ok(inbox.id)
        default = next(
            (folder["id"] for folder in folders if folder.get("role") == "inbox" and folder.get("selectable")),
            next((folder["id"] for folder in folders if folder.get("selectable")), None),
        )
        return {
            "inbox_id": inbox.id,
            "email": inbox.email,
            "folders": folders,
            "default_folder_id": default,
            "sync": status,
        }
    except Exception as exc:
        raise HTTPException(502, _mark_error(inbox.id, exc)) from exc


@router.get("/inboxes/{inbox_id}/folders")
async def list_folders(inbox_id: int, db: AsyncSession = Depends(get_db)):
    inbox, account = await _smtp_context(db, inbox_id)
    return await _folders_response(db, inbox, account)


@router.get("/inboxes/{inbox_id}/messages")
async def list_messages(
    inbox_id: int,
    folder_id: str = Query(..., min_length=1, max_length=2048),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=50),
    search: str = Query(default="", max_length=300),
    db: AsyncSession = Depends(get_db),
):
    inbox, account = await _smtp_context(db, inbox_id)
    local_sent = await _local_sent(db, inbox.id)
    try:
        result = await mail_thread(
            db,
            list_mailbox_page,
            account,
            folder_id=folder_id,
            page=page,
            page_size=page_size,
            search=search,
            local_sent=local_sent,
            mail_inbox_id=inbox.id,
        )
        result.update({"inbox_id": inbox.id, "email": inbox.email, "sync": _mark_ok(inbox.id)})
        return result
    except Exception as exc:
        raise HTTPException(502, _mark_error(inbox.id, exc)) from exc


@router.get("/inboxes/{inbox_id}/message")
async def get_message(
    inbox_id: int,
    folder_id: str = Query(..., min_length=1, max_length=2048),
    source: Literal["imap", "local"] = Query(...),
    message_key: str = Query(..., min_length=1, max_length=1024),
    db: AsyncSession = Depends(get_db),
):
    inbox, account = await _smtp_context(db, inbox_id)
    if source == "local":
        row = await db.scalar(
            select(SmtpMessage).where(
                SmtpMessage.inbox_id == inbox.id,
                SmtpMessage.message_id == message_key,
                SmtpMessage.direction == "sent",
            )
        )
        if row is None:
            raise HTTPException(404, "Локальная копия письма не найдена")
        result = local_message_to_dict(row)
        result.update({"folder_id": folder_id, "folder_name": "Отправленные"})
        return result

    try:
        uid = int(message_key)
    except ValueError as exc:
        raise HTTPException(400, "Некорректный идентификатор письма") from exc
    try:
        result = await mail_thread(
            db,
            fetch_mailbox_message,
            account,
            folder_id=folder_id,
            uid=uid,
            mail_inbox_id=inbox.id,
        )
        _mark_ok(inbox.id)
        return result
    except Exception as exc:
        raise HTTPException(502, _mark_error(inbox.id, exc)) from exc


@router.post("/inboxes/{inbox_id}/refresh")
async def refresh_mailbox(
    inbox_id: int,
    _body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
):
    inbox, account = await _smtp_context(db, inbox_id)
    # The browser is live rather than cache-backed.  Refreshing re-runs IMAP
    # LIST now; the UI then reloads only its selected folder and page.
    return await _folders_response(db, inbox, account)
