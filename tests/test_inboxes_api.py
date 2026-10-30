from datetime import datetime, timedelta

import pytest

from app.models import Inbox
from app.routers import inbox as inbox_router
from tests.conftest import make_email_log, make_campaign, make_lead


@pytest.mark.asyncio
async def test_list_inboxes_sent_today(session):
    """The inbox list API should include a `sent_today` field that reflects
    how many emails have been logged for that inbox on the current UTC day.
    """
    # create a fresh inbox in the database
    inbox = Inbox(email="foo@example.com")
    session.add(inbox)
    await session.flush()
    assert inbox.id is not None

    # before any logs the field should be zero; call the router directly
    listed = await inbox_router.list_inboxes(db=session)
    assert isinstance(listed, list)
    assert listed[0].sent_today == 0

    campaign = await make_campaign(session)
    lead1 = await make_lead(session,email="first@local.test")
    lead2 = await make_lead(session,email="second@local.test")

    # insert a log timestamped today and verify the count increments
    await make_email_log(session, lead_id=lead1.id, campaign_id=campaign.id, inbox_id=inbox.id)
    await session.flush()
    listed = await inbox_router.list_inboxes(db=session)
    assert listed[0].sent_today == 1

    # a log from yesterday should not be counted
    yesterday = datetime.utcnow() - timedelta(days=1)
    await make_email_log(
        session,
        lead_id=lead2.id,
        campaign_id=campaign.id,
        inbox_id=inbox.id,
        sent_at=yesterday,
    )
    await session.flush()
    listed = await inbox_router.list_inboxes(db=session)
    assert listed[0].sent_today == 1  # still only the single today-based entry


@pytest.mark.asyncio
async def test_get_single_inbox_returns_sent_today(session):
    inbox = Inbox(email="bar@example.com")
    session.add(inbox)
    await session.flush()
    campaign = await make_campaign(session)
    lead = await make_lead(session)
    await make_email_log(session, lead_id=lead.id, campaign_id=campaign.id, inbox_id=inbox.id)
    await session.flush()

    # invoke the router function directly instead of an HTTP call
    obj = await inbox_router.get_inbox(inbox.id, db=session)
    assert obj.sent_today == 1


@pytest.mark.asyncio
async def test_daily_limit_patch_changes_only_selected_mailbox_limit(session, monkeypatch):
    from fastapi import BackgroundTasks
    from app.schemas import InboxUpdate
    from app.routers import schedule

    recalculations = []
    monkeypatch.setattr(schedule, 'enqueue_global_recalculate', lambda tasks: recalculations.append(tasks))
    inbox = Inbox(email='selected@example.com', display_name='Отправитель', provider='smtp',
                  max_emails_per_day=5, wait_minutes_between=13, max_jitter_seconds=77, paused=True)
    other = Inbox(email='other@example.com', max_emails_per_day=9)
    session.add_all([inbox, other])
    await session.commit()

    tasks = BackgroundTasks()
    await inbox_router.update_inbox(inbox.id, InboxUpdate(max_emails_per_day=20), tasks, db=session)
    await session.refresh(inbox)
    await session.refresh(other)

    assert inbox.max_emails_per_day == 20
    assert inbox.display_name == 'Отправитель' and inbox.provider == 'smtp'
    assert inbox.wait_minutes_between == 13 and inbox.max_jitter_seconds == 77 and inbox.paused
    assert other.max_emails_per_day == 9
    assert recalculations == [tasks]
