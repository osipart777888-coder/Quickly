"""Address-specific advertising evidence and permanent opt-out registry."""
from sqlalchemy import delete, func, select
from app.collector.models import ContactPermission, ConsentEvent, CompanyContact
from app.local_models import Suppression
from app.models import Lead, CampaignLead, QueueSlot, Campaign
from app.local_imports import email_address
from app.time import utcnow


async def marketing_error(db, email):
    permission = await db.get(ContactPermission, email.lower())
    if not permission:
        return ''  # Existing unrelated Quickly contacts retain their old rules.
    if permission.status != 'confirmed' or not permission.evidence or not permission.advertiser or not permission.granted_at or permission.granted_at > utcnow():
        return 'Нет подтверждённого согласия этого адресата на рекламу от указанной компании'
    return ''


async def set_permission(db, email, *, status, advertiser='', evidence='', evidence_url='', granted_at=None, actor_id=None):
    email = email_address(email)
    if status == 'confirmed' and (not advertiser.strip() or not evidence.strip() or not evidence_url.strip() or not granted_at or granted_at > utcnow()):
        raise ValueError('Для подтверждения нужны рекламодатель, проверяемое основание, ссылка/номер документа и дата согласия не в будущем')
    if status == 'confirmed' and await db.get(Suppression, email):
        raise ValueError('Адрес в реестре отказов; автоматическое повторное разрешение запрещено')
    permission = await db.get(ContactPermission, email)
    if not permission:
        permission = ContactPermission(email=email)
        db.add(permission)
    permission.status, permission.advertiser, permission.evidence = status, advertiser, evidence
    permission.evidence_url, permission.granted_at, permission.updated_at = evidence_url, granted_at, utcnow()
    db.add(ConsentEvent(email=email, status=status, advertiser=advertiser, evidence=evidence, evidence_url=evidence_url, actor_id=actor_id))
    if status == 'refused':
        if not await db.get(Suppression, email):
            db.add(Suppression(email=email, reason=evidence or 'Отказ от рекламы'))
        leads = list((await db.scalars(select(Lead).where(func.lower(Lead.email) == email))).all())
        for lead in leads:
            lead.status = 'unsubscribed'
            enrollments = list((await db.scalars(select(CampaignLead).where(CampaignLead.lead_id == lead.id))).all())
            for enrollment in enrollments:
                enrollment.enrollment_status = 'unsubscribed'
                await db.execute(delete(QueueSlot).where(QueueSlot.campaign_lead_id == enrollment.id))
    await db.flush()
    return permission


async def transfer_to_quickly(db, company_ids, *, campaign_id=None):
    if campaign_id and not await db.get(Campaign, campaign_id):
        raise ValueError('Кампания не найдена')
    from app.collector.reports import exportable_company
    from app.collector.models import Company
    report = {'created': 0, 'existing': 0, 'blocked': 0, 'enrolled': 0}
    seen = set()
    for company in (await db.scalars(select(Company).where(Company.id.in_(company_ids)))).all():
        if company.status == 'excluded' or company.needs_review:
            report['blocked'] += 1
            continue
        allowed = await exportable_company(db, company)
        emails = set(allowed.get('emails', [])) if allowed else set()
        for email in emails:
            if email in seen: continue
            seen.add(email)
            permission = await db.get(ContactPermission, email)
            suppressed = await db.get(Suppression, email)
            leads = list((await db.scalars(select(Lead).where(func.lower(Lead.email) == email))).all())
            if suppressed or await marketing_error(db, email) or not permission or any(l.status in {'unsubscribed', 'invalid', 'bounced', 'blacklisted'} for l in leads):
                report['blocked'] += 1
                continue
            lead = leads[0] if leads else None
            if lead:
                report['existing'] += 1
            else:
                lead = Lead(email=email, name=allowed['name'][:255], status='active', custom_data={
                    'company': allowed['name'], 'city': allowed.get('city', ''), 'okved': allowed.get('okved_main', ''),
                    'collector_company_id': company.id, 'source': 'collector', 'consent_basis': permission.evidence,
                    'consent_date': permission.granted_at.date().isoformat(), 'advertiser': permission.advertiser,
                    'consent_evidence': permission.evidence_url})
                db.add(lead)
                await db.flush()
                report['created'] += 1
            if campaign_id and not await db.scalar(select(CampaignLead.id).where(CampaignLead.lead_id == lead.id, CampaignLead.campaign_id == campaign_id)):
                db.add(CampaignLead(lead_id=lead.id, campaign_id=campaign_id, sending_paused=True))
                report['enrolled'] += 1
    await db.flush()
    return report
