"""Local deployment security invariants; no real provider connections."""
import pytest
from sqlalchemy import select

from app.models import AppSetting
from app.settings_manager import _ensure_secrets, save_setting_to_db, settings
from app.routers.auth import _secure_auth_cookie


@pytest.mark.asyncio
async def test_external_keys_not_copied_to_database(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_SECRET_KEY', 'test-signing-external')
    monkeypatch.setenv('QUICKLY_ENCRYPTION_KEY', 'test-encryption-external')
    await save_setting_to_db(session, 'quickly_secret_key', 'old-signing')
    await save_setting_to_db(session, 'quickly_encryption_key', 'old-encryption')
    await _ensure_secrets(session)
    keys = (await session.execute(select(AppSetting.key))).scalars().all()
    assert 'quickly_secret_key' not in keys
    assert 'quickly_encryption_key' not in keys


@pytest.mark.asyncio
async def test_external_keys_required_when_enabled(session, monkeypatch):
    monkeypatch.setenv('QUICKLY_REQUIRE_EXTERNAL_KEYS', 'true')
    monkeypatch.delenv('QUICKLY_ENCRYPTION_KEY', raising=False)
    with pytest.raises(RuntimeError, match='External'):
        await _ensure_secrets(session)


@pytest.mark.parametrize('origin,enabled,secure', [
    ('http://localhost:8765', 'true', False),
    ('http://127.0.0.1:8765', 'true', False),
    ('http://public.example', 'true', True),
    ('https://localhost:8765', 'true', True),
    ('http://localhost:8765', 'false', True),
])
def test_http_cookie_only_for_explicit_loopback(origin, enabled, secure, monkeypatch):
    monkeypatch.setattr(settings, 'base_url', origin)
    monkeypatch.setenv('QUICKLY_LOCAL_HTTP', enabled)
    assert _secure_auth_cookie() is secure


@pytest.mark.asyncio
async def test_local_admin_registration_login_refresh_and_closed_registration(engine, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import auth, security
    monkeypatch.setattr(auth, 'SECRET_KEY', auth.SECRET_KEY)
    monkeypatch.setattr(auth, '_API_KEY_HASH_SECRET', auth._API_KEY_HASH_SECRET)
    monkeypatch.setattr(security, '_fernet', security._fernet)
    monkeypatch.setenv('QUICKLY_LOCAL_HTTP', 'true')
    monkeypatch.setenv('BASE_URL', 'http://localhost:8765')
    monkeypatch.setenv('QUICKLY_SECRET_KEY', 'local-admin-test-signing-key')
    monkeypatch.setenv('QUICKLY_ENCRYPTION_KEY', 'local-admin-test-encryption-key')
    monkeypatch.setattr(settings, 'base_url', 'http://localhost:8765')
    with TestClient(app, base_url='http://localhost:8765') as client:
        assert client.get('/api/auth/setup-status').json()['setup_complete'] is False
        result = client.post('/api/auth/register', json={
            'username': 'local_admin', 'email': 'admin@local.test', 'password': 'LocalTestPass13!',
        })
        assert result.status_code == 201
        assert result.json()['role'] == 'admin'
        result = client.post('/api/auth/login', json={'username': 'local_admin', 'password': 'LocalTestPass13!'})
        assert result.status_code == 200
        assert 'Secure' not in result.headers['set-cookie']
        assert client.post('/api/auth/refresh').status_code == 200
        assert client.get('/api/auth/me').json()['username'] == 'local_admin'
        assert client.post('/api/auth/register', json={
            'username': 'second', 'email': 'second@local.test', 'password': 'LocalTestPass13!',
        }).status_code == 403
