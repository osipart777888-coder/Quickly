"""Validate every encrypted value and migrate only legacy plaintext."""
from sqlalchemy import select,cast,Text,update
from app.models import AppSetting,GmailAccount,Inbox,Office365Account,SmtpAccount,User,Webhook
from app.security import decrypt,is_encrypted

async def encrypt_existing_local_secrets(db):
    changes=[]
    for model,fields in [
        (AppSetting,['value']),(GmailAccount,['access_token','refresh_token']),
        (Office365Account,['access_token','refresh_token']),
        (SmtpAccount,['smtp_password','imap_password']),
        (User,['notif_access_token','notif_refresh_token']),
        (Webhook,['secret']),(Inbox,['beacon_setup_token','beacon_webhook_secret']),
    ]:
        primary=list(model.__table__.primary_key.columns)
        # cast bypasses transparent decryption so existing ciphertext is not
        # needlessly re-encrypted (and updated_at changed) on every startup.
        rows=(await db.execute(select(*primary,*(cast(getattr(model,f),Text).label(f) for f in fields)))).mappings().all()
        for row in rows:
            values={}
            for field in fields:
                raw=row[field]
                if not raw:continue
                if is_encrypted(raw):decrypt(raw)  # validate before any writes
                else:values[field]=raw
            if values:changes.append((model,[(col,row[col.name]) for col in primary],values))
    for model,keys,values in changes:
        await db.execute(update(model).where(*(col==value for col,value in keys)).values(**values).execution_options(synchronize_session=False))
    await db.commit()
