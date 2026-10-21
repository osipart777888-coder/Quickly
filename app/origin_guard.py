"""Reject cross-origin cookie-authenticated API writes, including simple forms."""
import os
from urllib.parse import urlsplit
from starlette.responses import JSONResponse
from app.settings_manager import settings

class OriginGuardMiddleware:
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']=='http' and scope['method'] not in {'GET','HEAD','OPTIONS'} and scope.get('path','').startswith('/api/'):
            headers={k.lower():v.decode('latin1') for k,v in scope.get('headers',[])}
            origin=headers.get(b'origin')
            if origin:
                base=(settings.base_url or '').rstrip('/')
                allowed={base,*[s.strip().rstrip('/') for s in os.getenv('CORS_ORIGINS','http://localhost:5173').split(',') if s.strip() and s.strip()!='*']}
                parts=urlsplit(base)
                if parts.hostname in {'localhost','127.0.0.1','::1'}:
                    port=':'+str(parts.port) if parts.port else ''
                    allowed.update(parts.scheme+'://'+host+port for host in ['localhost','127.0.0.1','[::1]'])
                # Explicit API credentials have no ambient-cookie authority.
                explicit=headers.get(b'x-api-key') or headers.get(b'authorization','').lower().startswith('bearer ')
                if origin.rstrip('/') not in allowed and not explicit:
                    return await JSONResponse({'detail':'Недоверенный Origin для изменения данных'},status_code=403)(scope,receive,send)
        await self.app(scope,receive,send)
