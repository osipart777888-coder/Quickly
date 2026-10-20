"""Persistent send budgets and uncertainty handling against real SQLAlchemy."""
import pytest
from sqlalchemy import func, select
from app.local_models import MailOperation, SenderDayQuota, SendControl, Suppression
from app.local_send import guarded_send, reserve_operation
from app.models import Inbox, Lead, Campaign
from app.sender import SendResult


@pytest.fixture(autouse=True)
def local_mode(monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL', 'true')


async def account(db, limit=5):
    inbox = Inbox(email='sender@local.test', provider='smtp', max_emails_per_day=limit)
    db.add(inbox)
    await db.commit()
    return inbox


@pytest.mark.asyncio
async def test_five_attempt_limit_includes_diagnostics_and_manual_tests(session):
    inbox = await account(session)
    sent = []
    def deliver(**kwargs):
        sent.append(kwargs['to_email'])
        return SendResult(f'<{len(sent)}@local.test>')
    for number in range(8):
        await guarded_send(session, inbox, operation_key=f'test:{number}', kind='diagnostic',
                           send=deliver, to_email='receiver@local.test')
    assert len(sent) == 5
    assert await session.scalar(select(func.count(MailOperation.id))) == 5
    assert await session.scalar(select(SenderDayQuota.used)) == 5


@pytest.mark.asyncio
async def test_committed_intent_not_replayed_after_lost_result(session):
    inbox = await account(session)
    operation, error = await reserve_operation(session, inbox, operation_key='campaign-step',
                                               recipient='receiver@local.test', kind='diagnostic')
    assert not error
    # Simulate a process dying after intent commit and before recording the result.
    session.expunge_all()
    inbox = await session.get(Inbox, inbox.id)
    sent = []
    result = await guarded_send(session, inbox, operation_key='campaign-step', kind='diagnostic',
                                send=lambda **kwargs: sent.append(1), to_email='receiver@local.test')
    assert not result
    assert sent == []
    assert await session.scalar(select(SenderDayQuota.used)) == 1


@pytest.mark.asyncio
async def test_campaign_default_locked_and_requires_contact_permission(session):
    inbox = await account(session)
    campaign = Campaign(name='Permission check')
    session.add(campaign)
    inbox.tracking_domain = 'mail.example.com'
    lead = Lead(email='receiver@local.test', custom_data={})
    session.add(lead)
    await session.commit()
    sender = lambda **kwargs: SendResult('<test@local.test>')
    result = await guarded_send(session, inbox, kind='campaign', campaign_id=campaign.id, send=sender,
                                to_email=lead.email, lead=lead)
    assert not result
    session.add(SendControl(id=1, campaigns_enabled=True))
    await session.commit()
    result = await guarded_send(session, inbox, kind='campaign', campaign_id=campaign.id, send=sender,
                                to_email=lead.email, lead=lead)
    assert not result
    lead.custom_data = {'consent_date': '2026-10-20', 'consent_basis': 'Запрос клиента на переписку'}
    await session.commit()
    assert await guarded_send(session, inbox, kind='campaign', campaign_id=campaign.id, send=sender, to_email=lead.email, lead=lead)


@pytest.mark.asyncio
async def test_emergency_stop_blocks_all_new_messages(session):
    inbox = await account(session)
    session.add(SendControl(id=1, emergency_stopped=True))
    await session.commit()
    sent = []
    for kind in ['campaign', 'diagnostic', 'test', 'reply', 'notification']:
        assert not await guarded_send(session, inbox, kind=kind, send=lambda **kwargs: sent.append(1), to_email='receiver@local.test')
    assert sent == []
    assert await session.scalar(select(func.count(MailOperation.id))) == 0


@pytest.mark.asyncio
async def test_suppression_blocks_reply_and_manual_send(session):
    inbox = await account(session)
    session.add(Suppression(email='receiver@local.test', reason='Просьба не писать'))
    await session.commit()
    assert not await guarded_send(session, inbox, kind='reply', send=lambda **kwargs: SendResult('<x>'), to_email='receiver@local.test')


@pytest.mark.asyncio
async def test_network_failure_is_uncertain_and_not_retried(session):
    inbox = await account(session)
    def broken(**kwargs):
        raise ConnectionError('test connection lost after DATA')
    with pytest.raises(ConnectionError):
        await guarded_send(session, inbox, operation_key='uncertain-one', kind='test', send=broken, to_email='receiver@local.test')
    operation = await session.scalar(select(MailOperation))
    assert operation.status == 'uncertain'
    assert not await guarded_send(session, inbox, operation_key='uncertain-one', kind='test',
                                   send=lambda **kwargs: SendResult('<duplicate>'), to_email='receiver@local.test')

@pytest.mark.asyncio
async def test_current_refusal_counts_toward_three_failure_pause(session):
    from app.sender import SendFailure
    inbox=await account(session)
    calls=[]
    def refused(**kwargs):
        calls.append(1)
        return SendFailure('smtp_rejected','Local test refusal')
    for number in range(3):
        assert not await guarded_send(session,inbox,kind='test',operation_key=f'refusal:{number}',send=refused,to_email='receiver@local.test')
        assert inbox.paused is (number==2)
    assert not await guarded_send(session,inbox,kind='test',send=refused,to_email='receiver@local.test')
    assert len(calls)==3
