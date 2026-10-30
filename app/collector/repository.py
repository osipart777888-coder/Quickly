"""Conservative identity matching, origin-preserving updates and contact dedup."""
import hashlib
import json
from sqlalchemy import or_, select
from app.collector.models import Company, CompanyContact, CompanyObservation, ContactPermission, CollectionResult
from app.collector.validation import domain_of, normalized_text, normalize_record
from app.time import utcnow

FIELDS = ['name', 'inn', 'kpp', 'ogrn', 'okved_main', 'okved_extra', 'okved_as_of', 'country',
          'region', 'city', 'address', 'website', 'industry', 'employees', 'active']


def identity_conflict(company, record):
    # Even a shared OGRN/domain must not combine distinct known branches/KPP.
    if company.country and record.get('country') and normalized_text(company.country) != normalized_text(record['country']):
        return True
    return any(getattr(company, key) and record.get(key) and getattr(company, key) != record[key]
               for key in ['inn', 'kpp', 'ogrn'])


async def match_company(db, record):
    name = normalized_text(record['name'])
    domain = domain_of(record.get('website', ''))
    address = normalized_text(record.get('address', ''))
    conditions = [Company.normalized_name == name]
    if record.get('inn'): conditions.append(Company.inn == record['inn'])
    if record.get('ogrn'): conditions.append(Company.ogrn == record['ogrn'])
    candidates = list((await db.scalars(select(Company).where(or_(*conditions)))).all())
    candidates = [c for c in candidates if not identity_conflict(c, record)]
    priorities = [
        lambda c: bool(record.get('inn') and record.get('kpp') and c.inn == record['inn'] and c.kpp == record['kpp']),
        lambda c: bool(record.get('ogrn') and c.ogrn == record['ogrn']),
        lambda c: bool(domain and c.domain == domain and c.normalized_name == name and (not address or not c.normalized_address or address == c.normalized_address)),
        lambda c: bool(address and c.normalized_name == name and c.normalized_address == address),
    ]
    for predicate in priorities:
        found = [c for c in candidates if predicate(c)]
        if len(found) == 1:
            return found[0], False
        if len(found) > 1:
            return None, True
    return None, bool(candidates)


async def add_contacts(db, company_id, contacts, *, source, rights):
    from app.local_models import Suppression
    existing = {(c.kind, c.value): c for c in (await db.scalars(select(CompanyContact).where(CompanyContact.company_id == company_id))).all()}
    has_primary = any(c.is_primary for c in existing.values() if c.kind == 'email')
    for item in contacts:
        key = (item['kind'], item['value'])
        if key[0] == 'email' and await db.get(Suppression, 'sha256:' + hashlib.sha256(key[1].lower().encode()).hexdigest()):
            continue
        origin = {'source': source, 'url': item['url'], 'rights': rights}
        contact = existing.get(key)
        if not contact:
            contact = CompanyContact(company_id=company_id, kind=key[0], value=key[1], origins=[origin],
                is_primary=key[0] == 'email' and not has_primary, mx_status=item.get('mx_status', 'not_checked'))
            db.add(contact)
            existing[key] = contact
            if key[0] == 'email': has_primary = True
        else:
            origins = [o for o in contact.origins if (o['source'], o['url']) != (source, item['url'])]
            contact.origins = origins + [origin]
            contact.checked_at = utcnow()
            if item.get('mx_status'): contact.mx_status = item['mx_status']
        if key[0] == 'email' and not await db.get(ContactPermission, key[1]):
            db.add(ContactPermission(email=key[1], status='unknown'))
            await db.flush()


async def has_collectable_email(db, contacts):
    from app.local_models import Suppression
    for contact in contacts:
        if contact['kind'] == 'email' and not await db.get(
            Suppression, 'sha256:' + hashlib.sha256(contact['value'].lower().encode()).hexdigest()
        ):
            return True
    return False


async def save_company(db, raw, *, source, rights, job_id=None, require_email=False, enrichment=None):
    if not rights.get('store'):
        raise ValueError('Источник не разрешает хранение данных')
    record = normalize_record(raw)
    extra_contacts = enrichment['contacts'] if enrichment else []
    if require_email and not await has_collectable_email(db, record['contacts'] + extra_contacts):
        return None, False
    external_id = record['external_id'] or hashlib.sha256(json.dumps({
        k: record.get(k) for k in ['inn', 'kpp', 'ogrn', 'name', 'website', 'address']
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    observation = await db.scalar(select(CompanyObservation).where(
        CompanyObservation.source == source, CompanyObservation.external_id == external_id))
    company = await db.get(Company, observation.company_id) if observation else None
    ambiguous = False
    if company and identity_conflict(company, record):
        raise ValueError('Идентификатор источника противоречит реквизитам ранее найденной организации')
    if not company:
        company, ambiguous = await match_company(db, record)
    existing = company is not None
    if not company:
        company = Company(name=record['name'], normalized_name=normalized_text(record['name']), needs_review=ambiguous)
        db.add(company)
        await db.flush()
    for key in FIELDS:
        value = record.get(key)
        if value not in ('', None, []):
            # All conflicting values remain in observations; stronger legal
            # registry facts take priority over rubric/directory descriptions.
            if not getattr(company, key) or source in {'fns', 'spark'} or key in {'active', 'employees'}:
                setattr(company, key, value)
    company.normalized_name = normalized_text(company.name)
    company.normalized_address = normalized_text(company.address)
    company.domain = domain_of(company.website)
    company.checked_at = utcnow()
    if not observation:
        observation = CompanyObservation(company_id=company.id, source=source, external_id=external_id)
        db.add(observation)
    observation.fields = {k: record[k] for k in FIELDS}
    observation.url = record['source_url']
    observation.rights = dict(rights)
    observation.checked_at = utcnow()
    await add_contacts(db, company.id, record['contacts'], source=source, rights=rights)
    if enrichment:
        # A phone may occur in both the card and its website. Make the card's
        # pending contacts visible before merging website origins.
        await db.flush()
        await add_contacts(db, company.id, extra_contacts, source=enrichment['source'], rights=enrichment['rights'])
    if job_id and not await db.get(CollectionResult, (job_id, company.id)):
        db.add(CollectionResult(job_id=job_id, company_id=company.id, was_existing=existing))
    await db.flush()
    return company, existing
