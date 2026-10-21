"""Persistent local-install extensions; existing Quickly tables remain authoritative."""
import uuid
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from app.database import Base
from app.time import utcnow


class SendControl(Base):
    __tablename__ = 'local_send_control'
    id = Column(Integer, primary_key=True, default=1)
    emergency_stopped = Column(Boolean, nullable=False, default=False)
    campaigns_enabled = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=False, default='Кампании ожидают явного разрешения пользователя')
    updated_at = Column(DateTime, nullable=False, default=utcnow)
    mail_concurrency = Column(Integer, nullable=False, default=4)


class SenderDayQuota(Base):
    __tablename__ = 'local_sender_day_quota'
    inbox_id = Column(Integer, ForeignKey('inbox.id'), primary_key=True)
    day = Column(String(10), primary_key=True)
    used = Column(Integer, nullable=False, default=0)


class SenderPacing(Base):
    __tablename__ = 'local_sender_pacing'
    inbox_id = Column(Integer, ForeignKey('inbox.id'), primary_key=True)
    last_reserved_at = Column(DateTime, nullable=False)


class MailboxReadState(Base):
    __tablename__ = 'local_mailbox_read_state'
    inbox_id = Column(Integer, ForeignKey('inbox.id'), primary_key=True)
    retry_after = Column(DateTime, nullable=True)


class MailOperation(Base):
    __tablename__ = 'local_mail_operation'
    id = Column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    operation_key = Column(String(255), nullable=False, unique=True, index=True)
    inbox_id = Column(Integer, ForeignKey('inbox.id'), nullable=False, index=True)
    campaign_id = Column(Integer, nullable=True, index=True)
    recipient = Column(String(320), nullable=False)
    kind = Column(String(24), nullable=False)
    status = Column(String(24), nullable=False, default='reserved')
    message_id = Column(String(512), nullable=False, default='')
    error = Column(Text, nullable=False, default='')
    retry_safe = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    finished_at = Column(DateTime, nullable=True)


class Suppression(Base):
    __tablename__ = 'local_suppression'
    email = Column(String(320), primary_key=True)
    reason = Column(Text, nullable=False, default='Не отправлять')
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DiagnosticPlan(Base):
    __tablename__ = 'local_diagnostic_plan'
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    inbox_ids = Column(JSON, nullable=False, default=list)
    mode = Column(String(16), nullable=False, default='pair')
    per_account_daily_limit = Column(Integer, nullable=False, default=1)
    auto_reply = Column(Boolean, nullable=False, default=False)
    rotation_offset = Column(Integer, nullable=False, default=0)
    last_batch_id = Column(String(32), nullable=True)
    last_dispatched_at = Column(DateTime, nullable=True)
    observation_hours = Column(Integer, nullable=False, default=48)
    poll_interval_seconds = Column(Integer, nullable=False, default=300)
    sender_id = Column(Integer, ForeignKey('inbox.id'), nullable=False)
    recipient_id = Column(Integer, ForeignKey('inbox.id'), nullable=False)
    enabled = Column(Boolean, nullable=False, default=False)
    interval_minutes = Column(Integer, nullable=False, default=1440)
    sending_days = Column(JSON, nullable=False, default=lambda: [0, 1, 2, 3, 4])
    hours_start = Column(String(5), nullable=False, default='09:00')
    hours_end = Column(String(5), nullable=False, default='18:00')
    max_messages_per_day = Column(Integer, nullable=False, default=1)
    next_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DiagnosticRun(Base):
    __tablename__ = 'local_diagnostic_run'
    id = Column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    plan_id = Column(Integer, ForeignKey('local_diagnostic_plan.id'), nullable=True)
    batch_id = Column(String(32), nullable=True, index=True)
    started_at = Column(DateTime, nullable=True)
    last_checked_at = Column(DateTime, nullable=True)
    reply_started_at = Column(DateTime, nullable=True)
    sender_email = Column(String(320), nullable=False, default='')
    recipient_email = Column(String(320), nullable=False, default='')
    accepted_at = Column(DateTime, nullable=True)
    reply_accepted_at = Column(DateTime, nullable=True)
    observation_deadline = Column(DateTime, nullable=True)
    reply_observation_deadline = Column(DateTime, nullable=True)
    next_check_at = Column(DateTime, nullable=True)
    check_attempts = Column(Integer, nullable=False, default=0)
    observation_status = Column(String(32), nullable=False, default='pending')
    check_error = Column(Text, nullable=False, default='')
    folder_kind = Column(String(16), nullable=False, default='unknown')
    sender_id = Column(Integer, ForeignKey('inbox.id'), nullable=False)
    recipient_id = Column(Integer, ForeignKey('inbox.id'), nullable=False)
    status = Column(String(32), nullable=False, default='created')
    operation_id = Column(String(32), ForeignKey('local_mail_operation.id'), nullable=True)
    message_id = Column(String(512), nullable=False, default='')
    reply_message_id = Column(String(512), nullable=False, default='')
    folder = Column(String(512), nullable=False, default='')
    authentication_headers = Column(JSON, nullable=False, default=dict)
    duration_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=False, default='')
    report = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)
    received_at = Column(DateTime, nullable=True)


class DiagnosticDayQuota(Base):
    __tablename__ = 'local_diagnostic_day_quota'
    plan_id = Column(Integer, ForeignKey('local_diagnostic_plan.id'), primary_key=True)
    day = Column(String(10), primary_key=True)
    used = Column(Integer, nullable=False, default=0)


class DiagnosticSenderDayQuota(Base):
    __tablename__ = 'local_diagnostic_sender_day_quota'
    plan_id = Column(Integer, ForeignKey('local_diagnostic_plan.id'), primary_key=True)
    inbox_id = Column(Integer, ForeignKey('inbox.id'), primary_key=True)
    day = Column(String(10), primary_key=True)
    used = Column(Integer, nullable=False, default=0)


class AccountSafetyEvent(Base):
    __tablename__ = 'local_account_safety_event'
    __table_args__ = (UniqueConstraint('inbox_id', 'kind', 'reference'),)
    id = Column(Integer, primary_key=True)
    inbox_id = Column(Integer, ForeignKey('inbox.id'), nullable=False)
    kind = Column(String(24), nullable=False)
    reference = Column(String(512), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
