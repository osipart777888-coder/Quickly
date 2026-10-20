"""Optional official Google Postmaster adapter. No inferred reputation scores."""
import json
import re
import httpx
from app.models import AppSetting


def mail_domain(value):
    value = value.strip().lower().encode('idna').decode('ascii')
    if len(value) > 253 or '.' not in value or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')):
        raise ValueError('Нужен корректный почтовый домен')
    return value


async def fetch_postmaster(domain, token, client=None):
    domain = mail_domain(domain)
    if not token:
        return {'configured': False, 'available': False, 'reason': 'Нужен отдельный OAuth-токен Postmaster и подтверждённый домен'}
    async def request(http):
        response = await http.get(f'https://gmailpostmastertools.googleapis.com/v1/domains/{domain}/trafficStats',
                                  headers={'Authorization': f'Bearer {token}'}, params={'pageSize': 31})
        if response.status_code in (401,403):
            return {'configured': True, 'available': False, 'reason': 'Токен истёк, нет postmaster.readonly или доступа к подтверждённому домену'}
        response.raise_for_status()
        data = response.json()
        stats = data.get('trafficStats', [])
        return {'configured': True, 'available': bool(stats), 'domain': domain, 'source': 'Google Postmaster Tools API',
                'traffic_stats': stats, 'reason': '' if stats else 'Провайдер пока не предоставляет статистику для этого домена',
                'note': 'Это агрегированные данные провайдера. Они не доказывают доставку конкретного письма.'}
    if client is not None:
        return await request(client)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
        return await request(http)
