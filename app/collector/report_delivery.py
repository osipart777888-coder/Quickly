"""Explicitly enabled administrator reports, through Quickly's send guard."""
import json
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.collector.models import CollectorSetting, CollectionJob, CollectionResult, Company
from app.collector.reports import export_bytes, statistics
from app.models import Inbox, SmtpAccount
from app.local_send import guarded_send
from app.sender import SendResult


async def run_pending_reports():
    async with AsyncSessionLocal() as db:
        settings = await db.get(CollectorSetting, 'report')
        config = settings.value if settings else {}
        if not config.get('enabled'): return
        job = await db.scalar(select(CollectionJob).where(CollectionJob.report_status == 'pending').order_by(CollectionJob.created_at).limit(1).with_for_update(skip_locked=True))
        if not job: return
        inbox = await db.get(Inbox, config.get('inbox_id', 0))
        account = await db.scalar(select(SmtpAccount).where(SmtpAccount.inbox_id == config.get('inbox_id', 0)))
        if not inbox or not account:
            job.report_status = 'configuration_error'
            await db.commit()
            return
        ids = list((await db.scalars(select(CollectionResult.company_id).where(CollectionResult.job_id == job.id))).all())
        stats = await statistics(db, ids)
        report_params = {k: job.params.get(k) for k in ['sources', 'country', 'cities', 'regions', 'okved', 'query']}
        body = 'Сбор компаний завершён.\n' + json.dumps({'параметры': report_params, 'статистика': stats, 'дубли': job.duplicates, 'ошибки': job.errors}, ensure_ascii=False, indent=2)
        message = EmailMessage()
        message['From'] = formataddr((inbox.display_name or '', inbox.email))
        message['To'] = config['recipient']
        message['Subject'] = 'Quickly: отчёт о сборе компаний'
        message_id = make_msgid()
        message['Message-ID'] = message_id
        message.set_content(body)
        if config.get('attach_csv'):
            companies = list((await db.scalars(select(Company).where(Company.id.in_(ids)))).all())
            raw, _ = await export_bytes(db, companies, 'csv')
            message.add_attachment(raw, maintype='text', subtype='csv', filename='companies.csv')
        job.report_status = 'sending'
        await db.commit()  # A crash after this is never replayed automatically.
        def send(**kwargs):
            from app.smtp_utils import _smtp_connect
            client = _smtp_connect(account, timeout=20)
            try:
                client.sendmail(inbox.email, [config['recipient']], message.as_bytes())
            finally:
                try: client.quit()
                except Exception: client.close()
            return SendResult(message_id=message_id, thread_id=message_id)
        try:
            result = await guarded_send(db, inbox, kind='collector_report', operation_key='collector-report:'+job.id,
                send=send, to_email=config['recipient'], from_email=inbox.email, subject=str(message['Subject']), body=body, is_html=False)
            job.report_status = 'sent' if isinstance(result, SendResult) else 'blocked'
        except Exception:
            job.report_status = 'uncertain'
        await db.commit()
