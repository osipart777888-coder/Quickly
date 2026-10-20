import base64
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app.local_send import public_links_error
from app.local_safety import gmail_abuse, record_feedback
from app.local_models import AccountSafetyEvent, Suppression
from app.models import Inbox, Lead, Campaign, CampaignLead, LeadUnsubscribeToken
from app.tracking_events import process_unsubscribe
from app.routers.local import campaign_preflight, cancel_campaign


def test_public_links_require_external_https():
    box = SimpleNamespace(tracking_domain=None, beacon_connected=False)
    assert public_links_error(box)
    box.tracking_domain = 'localhost'
    assert public_links_error(box)
    box.tracking_domain = '192.168.1.2'
    assert public_links_error(box)
    box.tracking_domain = 'links.example.com'
    assert public_links_error(box) == ''


def test_gmail_structured_complaint_only():
    encoded = base64.urlsafe_b64encode(b'Feedback-Type: abuse\r\nUser-Agent: test').decode()
    report = {'parts':[{'mimeType':'message/feedback-report','body':{'data':encoded}}]}
    assert gmail_abuse(report)
    assert not gmail_abuse({'mimeType':'text/plain','body':{'data':encoded}})


@pytest.mark.asyncio
async def test_delayed_bounce_stop_deduplicates_and_persists_reason(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    box = Inbox(email='sender@example.com', provider='smtp')
    session.add(box); await session.commit()
    for reference in ['one','one','two']:
        await record_feedback(session,box.id,'bounce',reference)
    assert not box.paused
    await record_feedback(session,box.id,'bounce','three')
    assert box.paused
    assert len((await session.execute(select(AccountSafetyEvent))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_complaint_stops_immediately(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    box = Inbox(email='sender@example.com', provider='smtp')
    session.add(box); await session.commit()
    await record_feedback(session,box.id,'complaint','arf-one')
    assert box.paused


@pytest.mark.asyncio
async def test_unsubscribe_applies_to_all_campaigns(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    lead = Lead(email='recipient@example.com'); a=Campaign(name='One'); b=Campaign(name='Two')
    session.add_all([lead,a,b]); await session.flush()
    session.add_all([CampaignLead(lead_id=lead.id,campaign_id=a.id),CampaignLead(lead_id=lead.id,campaign_id=b.id),LeadUnsubscribeToken(lead_id=lead.id,campaign_id=a.id,token='local-unsub-test')]); await session.commit()
    html,code=await process_unsubscribe(session,'local-unsub-test')
    assert code==200 and 'отписались' in html
    await session.refresh(lead)
    assert lead.status=='unsubscribed' and await session.get(Suppression,lead.email)
    for enrollment in (await session.execute(select(CampaignLead))).scalars().all():
        assert enrollment.sending_paused and enrollment.enrollment_status=='unsubscribed'


@pytest.mark.asyncio
async def test_preflight_and_cancel_preserve_history(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    campaign=Campaign(name='Draft'); session.add(campaign); await session.commit()
    report=await campaign_preflight(campaign.id,db=session)
    assert not report['ready'] and len(report['errors'])==3
    result=await cancel_campaign(campaign.id,db=session,_admin=None)
    assert result['cancelled'] and await session.get(Campaign,campaign.id)
    assert campaign.paused
    assert any('отменена' in error for error in (await campaign_preflight(campaign.id,db=session))['errors'])


def test_vpn_trust_never_allows_private_network(monkeypatch):
    from app import smtp_utils
    from app.settings_manager import settings
    import socket
    monkeypatch.setenv('QUICKLY_LOCAL_INSTALL','true')
    monkeypatch.delenv('SMTP_ALLOW_PRIVATE_HOSTS',raising=False)
    monkeypatch.setattr(settings,'test_mode',False)
    monkeypatch.setattr(settings,'smtp_trusted_proxy_hosts',{'smtp.own.example'})
    def resolved(ip): return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,0))]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:resolved('240.0.1.93'))
    assert smtp_utils._assert_host_not_private('smtp.own.example') is None
    assert smtp_utils._assert_host_not_private('smtp.other.example')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:resolved('192.168.1.1'))
    assert smtp_utils._assert_host_not_private('smtp.own.example')

@pytest.mark.asyncio
async def test_postmaster_reports_only_official_data():
    import httpx
    from app.local_reputation import fetch_postmaster
    def response(request):
        assert request.url.host == 'gmailpostmastertools.googleapis.com'
        assert request.headers['Authorization'] == 'Bearer local-test-token'
        return httpx.Response(200,json={'trafficStats':[{'name':'domains/example.com/trafficStats/20261020','domainReputation':'HIGH'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        report=await fetch_postmaster('example.com','local-test-token',client)
    assert report['available'] and report['traffic_stats'][0]['domainReputation']=='HIGH'
    assert not (await fetch_postmaster('example.com',''))['available']

@pytest.mark.asyncio
async def test_new_replies_filter_for_all_mail_providers(session):
    from app.models import GmailThread,Office365Thread,SmtpThread
    from app.unibox import list_unibox_conversations
    for index,(provider,model,key) in enumerate([('gmail',GmailThread,'thread_id'),('office365',Office365Thread,'conversation_id'),('smtp',SmtpThread,'thread_key')]):
        box=Inbox(email=f'filter-{index}@example.com',provider=provider)
        session.add(box);await session.flush()
        for unread in (True,False):
            session.add(model(inbox_id=box.id,**{key:f'thread-{index}-{unread}'},is_lead_thread=True,unread_lead_reply=unread))
    await session.commit()
    all_threads=await list_unibox_conversations(session,page=1,page_size=50,leads_only=True)
    unread_threads=await list_unibox_conversations(session,page=1,page_size=50,leads_only=True,lead_status='unread')
    assert all_threads['total']==6
    assert unread_threads['total']==3
    assert {x['provider'] for x in unread_threads['items']}=={'gmail','office365','smtp'}
    assert all(x['unread_lead_reply'] for x in unread_threads['items'])
