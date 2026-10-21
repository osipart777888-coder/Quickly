"""Shared limiter; authentication limits cannot be bypassed with forwarded headers."""
import os
from slowapi import Limiter
from app.client_ip import client_ip_from_request


def request_ip(request):
    if os.getenv('QUICKLY_TRUST_PROXY_HEADERS', '').lower() == 'true':
        return client_ip_from_request(request) or 'unknown'
    return request.client.host if request.client else 'unknown'


limiter = Limiter(key_func=request_ip, default_limits=['200/minute'])
