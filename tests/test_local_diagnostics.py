"""Diagnostics pass through real local SMTP and IMAP network services."""
import pytest
from sqlalchemy import select
from app.local_models import MailOperation
from app.local_diagnostics import start_diagnostic, check_receipt, send_diagnostic_reply
from app.models import Inbox, SmtpAccount
from app.settings_manager import settings
from tests.support.local_mail import local_mail_servers


@pytest.mark.asyncio
async def test_diagnostic_receipt_and_labelled_reply_over_real_smtp_imap(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL', 'true')
    monkeypatch.setenv('SMTP_ALLOW_PRIVATE_HOSTS', 'true')
    monkeypatch.setenv('SMTP_ALLOW_PLAIN_SMTP', 'true')
    monkeypatch.setattr(settings, 'test_mode', False)
    with local_mail_servers() as (store, smtp_port, imap_port):
        boxes = []
        for email in ['first@local.test', 'second@local.test']:
            inbox = Inbox(email=email, provider='smtp', max_emails_per_day=5)
            session.add(inbox)
            await session.flush()
            session.add(SmtpAccount(inbox_id=inbox.id, smtp_host='127.0.0.1', smtp_port=smtp_port,
                smtp_username=email, smtp_password='test-only', smtp_use_tls=False, smtp_use_ssl=False,
                imap_host='127.0.0.1', imap_port=imap_port, imap_username=email, imap_password='test-only', imap_use_ssl=False))
            boxes.append(inbox)
        await session.commit()
        run = await start_diagnostic(session, boxes[0].id, boxes[1].id)
        assert run.status == 'accepted'
        assert len(store.messages['second@local.test']) == 1
        run = await check_receipt(session, run)
        assert run.status == 'received'
        assert run.folder == 'INBOX'
        assert run.report['received_confirmed'] is True
        assert run.duration_ms >= 0
        run = await send_diagnostic_reply(session, run)
        assert run.reply_message_id
        run = await check_receipt(session, run, reply=True)
        assert run.status == 'reply_received'
        operations = (await session.execute(select(MailOperation))).scalars().all()
        assert len(operations) == 2
        assert all(item.kind == 'diagnostic' and item.status == 'accepted' for item in operations)
        assert b'DIAGNOSTIC' in store.messages['second@local.test'][0][1]
        assert len(store.messages['first@local.test']) == 1
