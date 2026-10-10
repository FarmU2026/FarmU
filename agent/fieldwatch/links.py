"""Signed link tokens (docs/implementation-specs.md "Conventions": Auth: farmer links / dashboard).

A token is `<payload>.<signature>`, both base64url without padding:
    payload   = JSON {"farm_id", "scope", "sub", "exp"}   (exp = Unix seconds, UTC)
    signature = HMAC-SHA256(key, payload bytes)

Scopes: onboard, report, upload, work_order (farmer links) and dashboard (operator bearer token).
The key is the SSM SecureString /fieldwatch/link-signing-key (config.LINK_KEY_PARAM). For local runs and
tests, FIELDWATCH_LINK_KEY in the environment wins over SSM.

Other lanes (notify send, approvals) import sign() to build links; the web API calls verify().

    token = links.sign("farm_1", "report", "inv_123", ttl_hours=72)
    url = links.url("https://dxxxx.cloudfront.net", "report", "inv_123", token)
    claims = links.verify(token, scope="report")      # raises LinkError
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Literal

from fieldwatch import config

Scope = Literal["onboard", "report", "upload", "work_order", "dashboard"]
SCOPES: tuple[str, ...] = ("onboard", "report", "upload", "work_order", "dashboard")

# Page paths on the front end (WS14) for each scope.
PAGES = {"report": "report.html", "work_order": "work-order.html", "upload": "upload.html", "onboard": "index.html"}

_KEY_CACHE: dict[str, bytes] = {}


class LinkError(Exception):
    """Token problem. `code` is one of the specs' error codes: TOKEN_EXPIRED, TOKEN_SCOPE."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LinkClaims:
    farm_id: str
    scope: str
    subject_id: str
    exp: int


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def get_key() -> bytes:
    """The HMAC key: FIELDWATCH_LINK_KEY if set, else the SSM SecureString (cached per container)."""
    env = os.getenv("FIELDWATCH_LINK_KEY")
    if env:
        return env.encode()
    name = os.getenv("FIELDWATCH_LINK_KEY_PARAM", config.LINK_KEY_PARAM)
    if name not in _KEY_CACHE:
        import boto3

        ssm = boto3.client("ssm", region_name=config.REGION)
        value = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        _KEY_CACHE[name] = value.encode()
    return _KEY_CACHE[name]


def clear_key_cache() -> None:
    _KEY_CACHE.clear()


def sign(farm_id: str, scope: str, subject_id: str, ttl_hours: float = 72, *,
         key: bytes | None = None, now: float | None = None) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    exp = int((now if now is not None else time.time()) + ttl_hours * 3600)
    payload = json.dumps({"farm_id": farm_id, "scope": scope, "sub": subject_id, "exp": exp},
                         separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(key or get_key(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify(token: str | None, scope: str | None = None, subject_id: str | None = None, *,
           key: bytes | None = None, now: float | None = None) -> LinkClaims:
    """Check signature, expiry, and (if given) scope and subject. Raises LinkError.

    A bad or missing signature is reported as TOKEN_SCOPE (the link isn't valid for this page);
    an expired one as TOKEN_EXPIRED (the page shows "text LINK for a new one").
    """
    if not token or token.count(".") != 1:
        raise LinkError("TOKEN_SCOPE", "Missing or malformed link token")
    p64, s64 = token.split(".")
    try:
        payload, sig = _b64d(p64), _b64d(s64)
    except (ValueError, TypeError):
        raise LinkError("TOKEN_SCOPE", "Malformed link token") from None
    want = hmac.new(key or get_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(want, sig):
        raise LinkError("TOKEN_SCOPE", "Link token signature does not match")
    try:
        d = json.loads(payload)
        claims = LinkClaims(farm_id=str(d["farm_id"]), scope=str(d["scope"]), subject_id=str(d["sub"]),
                            exp=int(d["exp"]))
    except (ValueError, KeyError, TypeError):
        raise LinkError("TOKEN_SCOPE", "Malformed link token") from None
    if claims.exp < (now if now is not None else time.time()):
        raise LinkError("TOKEN_EXPIRED", "This link has expired. Text LINK for a new one.")
    if scope is not None and claims.scope != scope:
        raise LinkError("TOKEN_SCOPE", f"This link is for {claims.scope}, not {scope}")
    if subject_id is not None and claims.subject_id != subject_id:
        raise LinkError("TOKEN_SCOPE", "This link is for a different item")
    return claims


def url(base_url: str, scope: str, subject_id: str, token: str) -> str:
    """Farmer-facing link on the front end, e.g. https://d.cloudfront.net/report.html?id=inv_1&t=..."""
    page = PAGES.get(scope, "index.html")
    return f"{base_url.rstrip('/')}/{page}?id={subject_id}&t={token}"
