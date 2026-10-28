"""Additive collector schema. No changes to existing mail/account tables."""
import uuid
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from app.database import Base
from app.security import EncryptedText
from app.time import utcnow


class CollectorSource(Base):
    __tablename__ = 'collector_source'
    key = Column(String(24), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    api_key = Column(EncryptedText, nullable=False, default='')
    credentials = Column(EncryptedText, nullable=False, default='')
    config = Column(JSON, nullable=False, default=dict)
    allow_store = Column(Boolean, nullable=False, default=False)
    allow_export = Column(Boolean, nullable=False, default=False)
    terms_url = Column(Text, nullable=False, default='')
    rights_basis = Column(Text, nullable=False, default='')
    paid_authorized = Column(Boolean, nullable=False, default=False)
    daily_request_limit = Column(Integer, nullable=False, default=0)
    budget_day = Column(String(10), nullable=False, default='')
    requests_used = Column(Integer, nullable=False, default=0)
    next_request_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class CollectionJob(Base):
    __tablename__ = 'collection_job'
    id = Column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    name = Column(String(255), nullable=False)
    params = Column(JSON, nullable=False, default=dict)
    status = Column(String(24), nullable=False, default='queued', index=True)
    processed = Column(Integer, nullable=False, default=0)
    duplicates = Column(Integer, nullable=False, default=0)
    sites_checked = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)
    filtered = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=False, default='')
    report_status = Column(String(24), nullable=False, default='disabled')
    created_at = Column(DateTime, nullable=False, default=utcnow)
    finished_at = Column(DateTime, nullable=True)


class CollectionTask(Base):
    __tablename__ = 'collection_task'
    __table_args__ = (UniqueConstraint('job_id', 'task_key', name='uq_collection_task_key'),
                      Index('ix_collection_task_due', 'status', 'due_at'))
    id = Column(Integer, primary_key=True)
    job_id = Column(String(32), ForeignKey('collection_job.id', ondelete='CASCADE'), nullable=False, index=True)
    task_key = Column(String(64), nullable=False)
    kind = Column(String(24), nullable=False)  # import, discover, website
    source = Column(String(24), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(24), nullable=False, default='pending')
    attempts = Column(Integer, nullable=False, default=0)
    due_at = Column(DateTime, nullable=False, default=utcnow)
    lease_until = Column(DateTime, nullable=True)
    claim_token = Column(String(32), nullable=False, default='')
    error = Column(Text, nullable=False, default='')


class Company(Base):
    __tablename__ = 'collected_company'
    id = Column(Integer, primary_key=True)
    name = Column(String(512), nullable=False)
    normalized_name = Column(String(512), nullable=False, index=True)
    inn = Column(String(12), nullable=False, default='', index=True)
    kpp = Column(String(9), nullable=False, default='')
    ogrn = Column(String(15), nullable=False, default='', index=True)
    okved_main = Column(String(12), nullable=False, default='', index=True)
    okved_extra = Column(JSON, nullable=False, default=list)
    okved_as_of = Column(String(32), nullable=False, default='')
    country = Column(String(100), nullable=False, default='')
    region = Column(String(255), nullable=False, default='', index=True)
    city = Column(String(255), nullable=False, default='', index=True)
    address = Column(Text, nullable=False, default='')
    normalized_address = Column(String(1024), nullable=False, default='', index=True)
    website = Column(Text, nullable=False, default='')
    domain = Column(String(255), nullable=False, default='', index=True)
    industry = Column(Text, nullable=False, default='')
    employees = Column(Integer, nullable=True)
    active = Column(Boolean, nullable=True)
    status = Column(String(24), nullable=False, default='new')
    needs_review = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    checked_at = Column(DateTime, nullable=False, default=utcnow)


class CompanyObservation(Base):
    __tablename__ = 'company_observation'
    __table_args__ = (UniqueConstraint('source', 'external_id', name='uq_company_observation_origin'),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey('collected_company.id', ondelete='CASCADE'), nullable=False, index=True)
    source = Column(String(24), nullable=False, index=True)
    external_id = Column(String(255), nullable=False)
    url = Column(Text, nullable=False, default='')
    fields = Column(JSON, nullable=False, default=dict)
    rights = Column(JSON, nullable=False, default=dict)
    checked_at = Column(DateTime, nullable=False, default=utcnow)


class CompanyContact(Base):
    __tablename__ = 'company_contact'
    __table_args__ = (UniqueConstraint('company_id', 'kind', 'value', name='uq_company_contact_value'),)
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey('collected_company.id', ondelete='CASCADE'), nullable=False, index=True)
    kind = Column(String(16), nullable=False)
    value = Column(String(1024), nullable=False, index=True)
    origins = Column(JSON, nullable=False, default=list)
    is_primary = Column(Boolean, nullable=False, default=False)
    mx_status = Column(String(24), nullable=False, default='not_checked')
    checked_at = Column(DateTime, nullable=False, default=utcnow)


class CollectionResult(Base):
    __tablename__ = 'collection_result'
    job_id = Column(String(32), ForeignKey('collection_job.id', ondelete='CASCADE'), primary_key=True)
    company_id = Column(Integer, ForeignKey('collected_company.id', ondelete='CASCADE'), primary_key=True)
    was_existing = Column(Boolean, nullable=False, default=False)


class ContactPermission(Base):
    __tablename__ = 'collector_contact_permission'
    email = Column(String(255), primary_key=True)
    status = Column(String(24), nullable=False, default='unknown')
    advertiser = Column(String(512), nullable=False, default='')
    evidence = Column(Text, nullable=False, default='')
    evidence_url = Column(Text, nullable=False, default='')
    granted_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow)


class ConsentEvent(Base):
    __tablename__ = 'collector_consent_event'
    id = Column(Integer, primary_key=True)
    email = Column(String(255), nullable=False, index=True)
    status = Column(String(24), nullable=False)
    advertiser = Column(String(512), nullable=False, default='')
    evidence = Column(Text, nullable=False, default='')
    evidence_url = Column(Text, nullable=False, default='')
    actor_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class CollectorSetting(Base):
    __tablename__ = 'collector_setting'
    key = Column(String(64), primary_key=True)
    value = Column(JSON, nullable=False, default=dict)


class WebsiteThrottle(Base):
    __tablename__ = 'collector_website_throttle'
    host = Column(String(255), primary_key=True)
    next_request_at = Column(DateTime, nullable=False)
