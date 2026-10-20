"""Real loopback SMTP + IMAP sockets, including a reply with threading headers."""
import imaplib
import smtplib
from email.message import EmailMessage

from tests.support.local_mail import local_mail_servers


def test_real_smtp_and_imap_round_trip():
    with local_mail_servers() as (store, smtp_port, imap_port):
        message = EmailMessage()
        message['From'] = 'first@local.test'
        message['To'] = 'second@local.test'
        message['Subject'] = '[Quickly DIAGNOSTIC] integration test'
        message['Message-ID'] = '<local-diagnostic@quickly.test>'
        message.set_content('Explicitly labelled local diagnostic message.')
        with smtplib.SMTP('127.0.0.1', smtp_port, timeout=3) as smtp:
            smtp.login('first@local.test', 'local-only')
            smtp.send_message(message)
        with imaplib.IMAP4('127.0.0.1', imap_port, timeout=3) as imap:
            imap.login('second@local.test', 'local-only')
            assert imap.select('INBOX')[0] == 'OK'
            _, matches = imap.uid('SEARCH', None, 'HEADER', 'Message-ID', '"<local-diagnostic@quickly.test>"')
            assert matches == [b'1']
            _, messages = imap.uid('FETCH', '1', '(RFC822)')
            assert b'Explicitly labelled local diagnostic' in messages[0][1]
        reply = EmailMessage()
        reply['From'] = 'second@local.test'
        reply['To'] = 'first@local.test'
        reply['Subject'] = 'Re: [Quickly DIAGNOSTIC] integration test'
        reply['Message-ID'] = '<local-reply@quickly.test>'
        reply['In-Reply-To'] = '<local-diagnostic@quickly.test>'
        reply.set_content('Diagnostic receipt confirmation from a test peer.')
        with smtplib.SMTP('127.0.0.1', smtp_port, timeout=3) as smtp:
            smtp.send_message(reply)
        with imaplib.IMAP4('127.0.0.1', imap_port, timeout=3) as imap:
            imap.login('first@local.test', 'local-only')
            imap.select('INBOX')
            _, matches = imap.uid('SEARCH', None, 'HEADER', 'In-Reply-To', '"<local-diagnostic@quickly.test>"')
            assert matches == [b'1']
        assert len(store.messages['first@local.test']) == 1
        assert len(store.messages['second@local.test']) == 1
