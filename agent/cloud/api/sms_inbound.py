"""POST /sms/inbound: Twilio "A message comes in" webhook (application/x-www-form-urlencoded).

1. Verify X-Twilio-Signature with the auth token from SSM /fieldwatch/twilio-auth-token. If that
   parameter doesn't exist yet, verification is skipped (logged) so the demo isn't blocked.
2. Dedupe on MessageSid (fw-idempotency, key "sms-in#<sid>", 7-day TTL).
3. Hand off to the reply handler (cloud/api/_reply.py -> fieldwatch.notify.inbound.handle).
4. Answer with empty TwiML; replies go out through the send path, never inline.

The signed URL is the public URL Twilio called. Behind CloudFront or a custom domain, set
FIELDWATCH_TWILIO_WEBHOOK_URL to that exact URL. Handler: cloud.api.sms_inbound.handler.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from . import _aws, _reply
from ._http import Request, Response, Router, log

router = Router("sms")
handler = router.handler

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _twiml(status: int = 200) -> Response:
    return Response(status=status, raw=TWIML_EMPTY, headers={"Content-Type": "text/xml"})


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode("utf-8"), hashlib.sha1).digest()).decode()


def public_url(req: Request) -> str:
    override = os.getenv("FIELDWATCH_TWILIO_WEBHOOK_URL")
    if override:
        return override
    ctx = req.event.get("requestContext") or {}
    host = req.headers.get("host") or ctx.get("domainName", "")
    qs = req.event.get("rawQueryString")
    return f"https://{host}{req.path}" + (f"?{qs}" if qs else "")


def _param_name() -> str:
    return os.getenv("FIELDWATCH_TWILIO_TOKEN_PARAM", "/fieldwatch/twilio-auth-token")


@router.route("POST", "/sms/inbound")
def inbound(req: Request):
    form = req.form()
    token = _aws.ssm_param(_param_name(), required=False)
    if token:
        given = req.headers.get("x-twilio-signature", "")
        want = twilio_signature(token, public_url(req), form)
        if not hmac.compare_digest(given, want):
            log.warning("bad Twilio signature for %s", public_url(req))
            return Response(status=403, raw="forbidden")
    else:
        log.warning("SSM %s not set: Twilio signature NOT verified", _param_name())

    frm, body, sid = form.get("From"), form.get("Body"), form.get("MessageSid") or form.get("SmsSid")
    if not frm or body is None:
        return _twiml()
    if sid:
        tbl = _aws.table("idempotency")
        try:
            tbl.put_item(Item={"key": f"sms-in#{sid}", "at": _aws.now_iso(), "expires_at": _aws.epoch() + 7 * 86400},
                         ConditionExpression="attribute_not_exists(#k)", ExpressionAttributeNames={"#k": "key"})
        except tbl.meta.client.exceptions.ConditionalCheckFailedException:
            return _twiml()
    try:
        _reply.handle(frm, body.strip(), "sms")
    except Exception:  # noqa: BLE001 - Twilio can't do anything useful with a 500
        log.exception("reply hand-off failed for %s", sid)
    return _twiml()
