import pytest

from fastapi.testclient import TestClient
from app.main import app


def test_schedule_api_basic_endpoints(authenticated_api):
    # No manual initialization required; the shared engine fixture
    # ensures the schema is in place and is wired into the FastAPI app.
    """Verify that the schedule-related APIs exist and return the expected
    shape even when the database is empty.
    """
    with TestClient(app) as client:
        # stats endpoint should succeed and return integer counts
        resp = client.get("/api/schedule/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert isinstance(stats.get("total_sent"), int)
        assert isinstance(stats.get("total_scheduled"), int)
        assert isinstance(stats.get("total_campaigns"), int)
        assert "global_recalc_finished_at" in stats
        assert stats["global_recalc_finished_at"] is None or isinstance(
            stats["global_recalc_finished_at"], str
        )

        # sent / scheduled lists should simply be arrays
        resp = client.get("/api/schedule/sent")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        resp = client.get("/api/schedule/scheduled")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        # recalculate-all: default is async; sync=true for full stats in tests
        resp = client.post("/api/schedule/recalculate-all?sync=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert "campaigns_processed" in data

        resp = client.get("/api/schedule/stats")
        assert resp.status_code == 200
        after = resp.json()
        assert isinstance(after.get("global_recalc_finished_at"), str)

        resp = client.post("/api/schedule/validate-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ok") is True
        assert "total_slots_checked" in data


@pytest.mark.asyncio
async def test_schedule_sent_includes_opens_clicks(session, authenticated_api):
    """When logs have associated opens/clicks we should return them without
    triggering lazy-loading errors.

    Previously the route performed a simple join and then accessed
    ``el.opens``/``el.clicks`` in the response comprehension, causing a
    MissingGreenlet exception under AsyncSession.  This regression test
    builds a minimal record set and validates the JSON structure produced
    by the endpoint.
    """
    from app.models import EmailOpen, EmailClick
    from tests.conftest import make_campaign, make_lead, make_email_log

    # create a campaign/lead and log entry
    campaign = await make_campaign(session)
    lead = await make_lead(session)
    log = await make_email_log(session, lead.id, campaign.id)

    # attach an open & click
    op = EmailOpen(email_log_id=log.id, ip_address="1.2.3.4")
    clk = EmailClick(email_log_id=log.id, ip_address="5.6.7.8")
    session.add_all([op, clk])
    await session.flush()
    # Release the write lock before TestClient runs app lifespan/settings writes
    # on the same pooled SQLite database.
    await session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/schedule/sent", params={"include_events": "true"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert data, "expected at least one log"
        entry = data[0]
        assert entry["opens"][0]["ip"] == "1.2.3.4"
        assert entry["clicks"][0]["ip"] == "5.6.7.8"


@pytest.fixture
def authenticated_api():
    from types import SimpleNamespace
    from app.main import app
    from app.auth import get_current_user
    async def admin():
        return SimpleNamespace(id=1, role="admin", is_active=True)
    app.dependency_overrides[get_current_user] = admin
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)
