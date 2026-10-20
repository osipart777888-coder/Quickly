"""Persist provider feedback and stop an account without rotating senders."""
from datetime import timedelta
import base64
import re
from sqlalchemy import func, select
from app.local_models import AccountSafetyEvent
from app.local_send import local_install
from app.models import Inbox, AppSetting
from app.time import utcnow


def gmail_abuse(payload):
    if payload.get('mimeType', '').lower() == 'message/feedback-report':
        try:
            encoded = payload.get('body', {}).get('data', '')
            text = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode(errors='replace')
            return bool(re.search(r'^Feedback-Type:\s*abuse\s*$', text, re.I | re.M))
        except (ValueError, TypeError):
            return False
    return any(gmail_abuse(p) for p in payload.get('parts', []))


async def record_feedback(db, inbox_id, kind, reference):
    if not local_install() or not inbox_id:
        return
    inbox = await db.scalar(select(Inbox).where(Inbox.id == inbox_id).with_for_update())
    if not inbox:
        return
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    insert = pg_insert if db.bind.dialect.name == 'postgresql' else sqlite_insert
    await db.execute(insert(AccountSafetyEvent).values(inbox_id=inbox_id, kind=kind, reference=str(reference)[:512], created_at=utcnow()).on_conflict_do_nothing(index_elements=['inbox_id','kind','reference']))
    count = await db.scalar(select(func.count(AccountSafetyEvent.id)).where(AccountSafetyEvent.inbox_id == inbox_id,
        AccountSafetyEvent.kind == 'bounce', AccountSafetyEvent.created_at >= utcnow() - timedelta(hours=24))) or 0
    if kind == 'complaint' or count >= 3:
        inbox.paused = True
        key = f'local_account_safety_{inbox_id}'
        reason = await db.get(AppSetting, key)
        if not reason:
            reason = AppSetting(key=key); db.add(reason)
        reason.value = 'Получена жалоба в формате ARF' if kind == 'complaint' else 'Не менее 3 подтверждённых возвратов за 24 часа'
        await db.flush()
