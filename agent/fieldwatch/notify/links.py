"""Scoped HMAC links; WS12 can use verify_token without importing a web app."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

SCOPES = {'report', 'upload', 'work_order', 'onboard'}


def _encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def signed_link(base_url, key, farm_id, link, now):
    scope = link['scope']
    ttl = float(link.get('ttl_hours', 24))
    if scope not in SCOPES or not 0 < ttl <= 168 or not key:
        raise ValueError('Invalid link scope, lifetime, or signing key')
    if not base_url.startswith('https://'):
        raise ValueError('Links require an HTTPS base URL')
    claims = dict(farm_id=farm_id, scope=scope, subject_id=str(link['subject_id']),
                  exp=int(now.timestamp() + ttl * 3600))
    payload = _encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())
    signature = _encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    # The front end's pages (web/report.html?inv=..., workorder.html?id=...): fieldwatch.interfaces.LINK_PAGES.
    from fieldwatch.interfaces import LINK_PAGES
    page, param = LINK_PAGES[scope]
    return (f"{base_url.rstrip('/')}/{page}?" +
            urlencode({param: claims['subject_id'], 't': payload + '.' + signature}))


def verify_token(token, key, *, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        payload, signature = token.split('.')
        expected = _encode(hmac.new(key, payload.encode(), hashlib.sha256).digest())
        if not key or not hmac.compare_digest(expected, signature):
            raise ValueError('Invalid link signature')
        claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if (set(claims) != {'farm_id', 'scope', 'subject_id', 'exp'} or
                claims['scope'] not in SCOPES or claims['exp'] <= now.timestamp()):
            raise ValueError('Expired or invalid link')
        return claims
    except (TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Invalid link') from exc
