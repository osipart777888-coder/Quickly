"""Idempotent at-rest migration for existing local-install secrets."""
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from app.models import AppSetting, GmailAccount, Inbox, Office365Account, SmtpAccount, User, Webhook


async def encrypt_existing_local_secrets(db):
    # Reads validate existing ciphertext against the external key before writes.
    for model, fields in [
        (AppSetting, ['value']), (GmailAccount, ['access_token', 'refresh_token']),
        (Office365Account, ['access_token', 'refresh_token']),
        (SmtpAccount, ['smtp_password', 'imap_password']),
        (User, ['notif_access_token', 'notif_refresh_token']),
        (Webhook, ['secret']), (Inbox, ['beacon_setup_token', 'beacon_webhook_secret']),
    ]:
        rows = (await db.execute(select(model))).scalars().all()
        for row in rows:
            for field in fields:
                if getattr(row, field):
                    flag_modified(row, field)
    await db.commit()
