"""Exercise real HTTP dependencies and real MCP tool functions, using only synthetic identities."""
import json
from datetime import timedelta
from types import SimpleNamespace
import httpx
import pytest
from starlette.requests import Request
from app.auth import hash_api_key,create_access_token
from app.database import get_db
from app.models import User,APIKey,Lead
from app.time import utcnow

async def identity(db,scopes,role='admin',**kwargs):
    user=User(username='revision2-admin',email='revision2@local.test',role=role,is_active=True)
    db.add(user);await db.flush()
    db.add(APIKey(user_id=user.id,name='Synthetic key',key_hash=hash_api_key('qk_revision2_synthetic'),prefix='qk_revision2',scopes=scopes,**kwargs))
    await db.commit()
    return user,{'X-API-Key':'qk_revision2_synthetic'}

@pytest.mark.asyncio
@pytest.mark.parametrize('bearer',[False,True])
async def test_write_key_cannot_mint_unrestricted_credentials(session,bearer):
    from app.main import app
    _,headers=await identity(session,['write'])
    if bearer:headers={'Authorization':'Bearer qk_revision2_synthetic'}
    async def db():yield session
    app.dependency_overrides[get_db]=db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
            result=await client.post('/api/auth/api-keys',headers=headers,json={'name':'privilege escalation','scopes':['*']})
            assert result.status_code==403
    finally:app.dependency_overrides.pop(get_db,None)

@pytest.mark.asyncio
@pytest.mark.parametrize('bearer',[False,True])
@pytest.mark.parametrize('scope',['read','leads:read','leads:write'])
async def test_scopes_at_http_and_mcp_tools(session,monkeypatch,bearer,scope):
    from app.main import app
    from app import mcp_leads
    _,headers=await identity(session,[scope])
    if bearer:headers={'Authorization':'Bearer qk_revision2_synthetic'}
    lead=Lead(email='contact@local.test',name='Original');session.add(lead);await session.commit()
    async def db():yield session
    app.dependency_overrides[get_db]=db
    # MCP really calls its registered tool functions and forwards credentials into
    # the real ASGI HTTP app. Only network transport is replaced, not auth or tools.
    original=httpx.AsyncClient
    def loopback(*args,**kwargs):
        kwargs['transport']=httpx.ASGITransport(app)
        return original(*args,**kwargs)
    monkeypatch.setattr(mcp_leads.httpx,'AsyncClient',loopback)
    request=Request({'type':'http','method':'POST','path':'/api/mcp/','headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()]})
    ctx=SimpleNamespace(request_context=SimpleNamespace(request=request))
    try:
        async with loopback(base_url='http://test') as client:
            assert (await client.put('/api/local/control',headers=headers,json={'emergency_stopped':True})).status_code==403
            assert (await client.post('/api/local/diagnostics/run',headers=headers,json={})).status_code==403
            assert (await client.post('/api/settings/backup/restore/execute',headers=headers,json={})).status_code==403
            assert (await client.post('/api/auth/api-keys',headers=headers,json={'name':'bad','scopes':['*']})).status_code==403
        listed=json.loads(await mcp_leads.list_leads(ctx))
        if scope=='leads:write':assert listed['status_code']==403
        else:assert not (isinstance(listed,dict) and listed.get('error'))
        if scope=='leads:write':
            bypass=json.loads(await mcp_leads.update_lead(ctx,lead.id,enrollment_status='active'))
            assert bypass['status_code']==403
            async with loopback(base_url='http://test') as client:
                assert (await client.post('/api/leads/bulk-status',headers=headers,json={'lead_ids':[lead.id],'enrollment_status':'active'})).status_code==403
                assert (await client.post(f'/api/leads/{lead.id}/recover',headers=headers,json={'email':'changed@local.test'})).status_code==403
        changed=json.loads(await mcp_leads.update_lead(ctx,lead.id,name='Changed'))
        if scope=='leads:write':
            assert not changed.get('error'),changed
            deleted=json.loads(await mcp_leads.delete_lead(ctx,lead.id));assert not deleted.get('error'),deleted
        else:
            assert changed['status_code']==403
            assert json.loads(await mcp_leads.delete_lead(ctx,lead.id))['status_code']==403
            assert json.loads(await mcp_leads.add_campaign_leads(ctx,999,[{'email':'new@local.test'}]))['status_code']==403
    finally:app.dependency_overrides.pop(get_db,None)

@pytest.mark.asyncio
@pytest.mark.parametrize('bearer',[False,True])
@pytest.mark.parametrize('condition',['revoked','expired','member'])
async def test_credentials_expiry_revocation_and_role(session,bearer,condition):
    from app.main import app
    extras={'revoked':True} if condition=='revoked' else {'expires_at':utcnow()-timedelta(seconds=1)} if condition=='expired' else {}
    _,headers=await identity(session,['*'],role='member' if condition=='member' else 'admin',**extras)
    if bearer:headers={'Authorization':'Bearer qk_revision2_synthetic'}
    async def db():yield session
    app.dependency_overrides[get_db]=db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
            response=await client.put('/api/local/control',headers=headers,json={'emergency_stopped':True})
            assert response.status_code==(403 if condition=='member' else 401)
            await session.rollback()  # real get_db also rolls back an HTTP 403
            for path in ['/api/settings/backup/restore/execute','/api/local/diagnostics/run']:
                assert (await client.post(path,headers=headers,json={})).status_code==(403 if condition=='member' else 401)
    finally:app.dependency_overrides.pop(get_db,None)

@pytest.mark.asyncio
async def test_cookie_csrf_cors_and_csv_whitelist(session):
    from app.main import app
    from app.local_models import DiagnosticRun
    from app.models import Inbox
    user,_=await identity(session,['*'])
    box=Inbox(email='csv@local.test');session.add(box);await session.flush()
    from app.log_redaction import register_secret
    register_secret('synthetic-provider-echo-secret')
    session.add(DiagnosticRun(error='Server echoed synthetic-provider-echo-secret',sender_id=box.id,recipient_id=box.id,status='expired',sender_email='=FORMULA@local.test',recipient_email='recipient@local.test',report={'secret':'synthetic-secret-must-not-export'},observation_status='expired'))
    await session.commit()
    async def db():yield session
    app.dependency_overrides[get_db]=db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://localhost:8000',cookies={'access_token':create_access_token(user.id,'admin')}) as client:
            from unittest.mock import patch
            from app.origin_guard import OriginGuardMiddleware
            async def old_behavior(self,scope,receive,send):await self.app(scope,receive,send)
            # Model the former middleware stack. A same-site form POST from
            # another localhost port reaches the authenticated handler.
            with patch.object(OriginGuardMiddleware,'__call__',old_behavior):
                before=await client.post('/api/local/diagnostics/runs/1/reply',headers={'Origin':'http://localhost:9999'})
                assert before.status_code==404
            attack=await client.post('/api/local/diagnostics/runs/1/reply',headers={'Origin':'http://localhost:9999'})
            assert attack.status_code==403
            assert 'access-control-allow-origin' not in attack.headers
            assert (await client.options('/api/local/control',headers={'Origin':'https://evil.invalid','Access-Control-Request-Method':'PUT'})).status_code==400
            response=await client.get('/api/local/diagnostics/export.csv')
            assert response.status_code==200
            assert "'=FORMULA@local.test" in response.text
            assert 'synthetic-secret-must-not-export' not in response.text
            assert 'synthetic-provider-echo-secret' not in response.text
            assert 'Срок наблюдения истёк' in response.text
    finally:app.dependency_overrides.pop(get_db,None)

def test_logs_redact_decrypted_credentials_and_exception_text():
    import io,logging
    from app.security import EncryptedText
    from app.log_redaction import install_log_redaction
    secret='revision2-password-kept-out-of-logs'
    EncryptedText().process_bind_param(secret,None)
    install_log_redaction()
    stream=io.StringIO();handler=logging.StreamHandler(stream)
    logger=logging.getLogger('revision2-security-log');logger.addHandler(handler)
    try:
        try:raise RuntimeError(f'upstream echoed {secret} refresh_token="unregistered-token-value" Authorization: Bearer other-synthetic-token')
        except RuntimeError:logger.exception('Provider failed: %s',secret)
    finally:logger.removeHandler(handler)
    output=stream.getvalue()
    assert secret not in output and 'unregistered-token-value' not in output and 'other-synthetic-token' not in output
    assert 'RuntimeError' in output and 'REDACTED' in output

def test_file_transport_logs_and_oauth_urls_redact_secrets(tmp_path,monkeypatch):
    from app import sender
    from app.log_redaction import register_secret,redact_credentials
    path=tmp_path/'smtp.log';monkeypatch.setattr(sender,'_SMTP_LOG_PATH',str(path))
    secret='revision2-secret-echoed-by-server';register_secret(secret)
    assert secret not in sender.SendFailure('auth_failed',secret).message
    sender._log_smtp_call('target@local.test','source@local.test','Test',error=secret)
    assert secret not in path.read_text()
    assert redact_credentials('/oauth/connect/synthetic-connect-token')=='/oauth/connect/REDACTED'
    assert 'synthetic-code' not in redact_credentials('/oauth/callback?code=synthetic-code&state=synthetic-state')

@pytest.mark.asyncio
async def test_secret_migration_is_idempotent_and_preserves_existing_ciphertext(session,monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy import text
    from app import security
    from app.local_secrets import encrypt_existing_local_secrets
    monkeypatch.setenv('QUICKLY_REQUIRE_EXTERNAL_KEYS','true')
    monkeypatch.setattr(security,'_fernet',Fernet(Fernet.generate_key()))
    await session.execute(text("INSERT INTO app_setting(key,value) VALUES ('revision2_legacy','synthetic-plaintext-secret')"));await session.commit()
    await encrypt_existing_local_secrets(session)
    first=(await session.execute(text("SELECT value,updated_at FROM app_setting WHERE key='revision2_legacy'"))).one()
    assert first.value.startswith('gAAAAA') and security.decrypt(first.value)=='synthetic-plaintext-secret'
    await encrypt_existing_local_secrets(session)
    second=(await session.execute(text("SELECT value,updated_at FROM app_setting WHERE key='revision2_legacy'"))).one()
    assert first==second

def test_log_redaction_preserves_percent_argument_formatting():
    import logging,io
    from app.log_redaction import install_log_redaction
    install_log_redaction();stream=io.StringIO();handler=logging.StreamHandler(stream);logger=logging.getLogger('revision2-format');logger.addHandler(handler)
    try:logger.warning('Unsubscribed lead_id=%s token=%s',7,'synthetic-query-secret')
    finally:logger.removeHandler(handler)
    assert 'lead_id=7' in stream.getvalue() and 'synthetic-query-secret' not in stream.getvalue()

@pytest.mark.asyncio
@pytest.mark.parametrize('bearer',[False,True])
async def test_restore_endpoint_does_not_hold_duplicate_auth_transaction(session,bearer):
    import asyncio
    from app.main import app
    _,headers=await identity(session,['*'])
    if bearer:headers={'Authorization':'Bearer qk_revision2_synthetic'}
    async def db():yield session
    app.dependency_overrides[get_db]=db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
            result=await asyncio.wait_for(client.post('/api/settings/backup/restore/execute',headers=headers,json={'restore_token':'synthetic-missing-restore-token'}),5)
            assert result.status_code==400
    finally:app.dependency_overrides.pop(get_db,None)

@pytest.mark.asyncio
async def test_mcp_mount_rejects_unauthenticated_request_without_middleware_crash(session):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        response=await client.post('/api/mcp/',headers={'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
        assert response.status_code==401
