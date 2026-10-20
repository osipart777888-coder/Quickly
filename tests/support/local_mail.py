"""Isolated SMTP/IMAP peers for integration tests, bound only to loopback.

Messages never leave this process. Plain transport is intentional in this
fixture; the application's production SMTP/IMAP policy remains unchanged.
"""
from collections import defaultdict
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import re
import socketserver
import threading

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult


class MailStore:
    def __init__(self):
        self.messages = defaultdict(list)
        self.lock = threading.Lock()

    def deliver(self, address, raw):
        with self.lock:
            mailbox = self.messages[address.lower()]
            mailbox.append((len(mailbox) + 1, raw))


class SMTPHandler:
    def __init__(self, store):
        self.store = store

    async def handle_DATA(self, server, session, envelope):
        for address in envelope.rcpt_tos:
            self.store.deliver(address, bytes(envelope.content))
        return '250 Accepted by local test server'


class IMAPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.user = ''
        self.write('* OK Local Quickly integration test IMAP')
        while raw := self.rfile.readline():
            try:
                tag, command, *rest = raw.decode().strip().split(' ', 2)
                args = rest[0] if rest else ''
                command = command.upper()
                if command == 'CAPABILITY':
                    self.write('* CAPABILITY IMAP4rev1')
                elif command == 'LOGIN':
                    self.user = args.split()[0].strip('"').lower()
                elif command == 'LIST':
                    self.write('* LIST (\\HasNoChildren) "/" "INBOX"')
                    self.write('* LIST (\\HasNoChildren) "/" "Diagnostics"')
                elif command in {'SELECT', 'EXAMINE'}:
                    self.write(f'* {len(self.mailbox())} EXISTS')
                    self.write('* FLAGS (\\Seen)')
                    self.write('* OK [UIDVALIDITY 4242] UIDs valid')
                    self.write(f'* OK [UIDNEXT {len(self.mailbox()) + 1}] Next UID')
                elif command == 'STATUS':
                    self.write(f'* STATUS INBOX (UIDVALIDITY 4242 UIDNEXT {len(self.mailbox()) + 1} MESSAGES {len(self.mailbox())})')
                elif command in {'UID', 'SEARCH', 'FETCH'}:
                    sub, _, criteria = args.partition(' ') if command == 'UID' else (command, '', args)
                    if sub.upper() == 'SEARCH':
                        messages = self.mailbox()
                        match = re.search(r'UID (\d+):', criteria, re.I)
                        if match:
                            messages = [(uid, body) for uid, body in messages if uid >= int(match.group(1))]
                        header = re.search(r'HEADER\s+([\w-]+)\s+"?([^"\s]+)', criteria, re.I)
                        if header:
                            key, value = header.groups()
                            messages = [(uid, body) for uid, body in messages if value.lower() in str(BytesParser(policy=policy.default).parsebytes(body).get(key, '')).lower()]
                        self.write('* SEARCH ' + ' '.join(str(uid) for uid, _ in messages))
                    elif sub.upper() == 'FETCH':
                        wanted = criteria.split()[0].strip('"')
                        for uid, body in self.mailbox():
                            if wanted in {str(uid), '1:*', '*'}:
                                self.write(f'* {uid} FETCH (UID {uid} RFC822 {{{len(body)}}}')
                                self.wfile.write(body + b')\r\n')
                                self.wfile.flush()
                elif command == 'LOGOUT':
                    self.write('* BYE Local test session closed')
                    self.write(f'{tag} OK LOGOUT completed')
                    break
                elif command not in {'NOOP', 'CLOSE', 'ID', 'ENABLE'}:
                    self.write(f'{tag} BAD Unsupported test command')
                    continue
                self.write(f'{tag} OK {command} completed')
            except (OSError, ValueError):
                break

    def mailbox(self):
        with self.server.store.lock:
            return list(self.server.store.messages[self.user])

    def write(self, text):
        self.wfile.write(text.encode() + b'\r\n')
        self.wfile.flush()


class IMAPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def local_mail_servers():
    import socket
    store = MailStore()
    imap = IMAPServer(('127.0.0.1', 0), IMAPHandler)
    imap.store = store
    thread = threading.Thread(target=imap.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        smtp_port = probe.getsockname()[1]
    smtp = Controller(SMTPHandler(store), hostname='127.0.0.1', port=smtp_port,
                      authenticator=lambda *args: AuthResult(success=True), auth_require_tls=False)
    smtp.start()
    try:
        yield store, smtp_port, imap.server_address[1]
    finally:
        smtp.stop()
        imap.shutdown()
        imap.server_close()
        thread.join(timeout=2)
