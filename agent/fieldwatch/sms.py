"""Real text messages. One send() and one inbound path, several providers behind them.

Provider is picked from env (FIELDWATCH_SMS) or auto-detected from which keys are present:

  twilio    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM (+1...). Free trial works: it texts
            numbers you verify in the Twilio console. Replies come in two ways: Twilio POSTs to
            /sms/inbound if you give it a public URL (ngrok), otherwise poll() fetches them.
  sns       AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (IAM keys, not the Bedrock API key).
            One-way only; US numbers need an origination number registered in the console.
  textbelt  TEXTBELT_KEY (the key "textbelt" sends one free text per day). One-way.
  none      nothing configured; the web phone panel is the only mirror.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from . import config  # noqa: F401  (loads agent/.env before the env reads below)

log = logging.getLogger("fieldwatch.sms")

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
TEXTBELT_KEY = os.getenv("TEXTBELT_KEY", "")


def provider() -> str:
    p = os.getenv("FIELDWATCH_SMS", "").lower()
    if p:
        return p
    if TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM:
        return "twilio"
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
        return "sns"
    if TEXTBELT_KEY:
        return "textbelt"
    return "none"


def e164(phone: str) -> str:
    """'402 555 0100' -> '+14025550100'. Anything already starting with + is kept."""
    digits = re.sub(r"\D", "", phone)
    if phone.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def farm_id_for(phone: str) -> str:
    """Same rule the web app uses to name a farm: last 10 digits of the phone."""
    return re.sub(r"\D", "", phone)[-10:]


# ---------- outbound ----------


def send(to: str, text: str) -> dict:
    """Send one text. Never raises; returns {"provider", "ok", "detail"} so the thread can show it."""
    p = provider()
    try:
        if p == "twilio":
            return _twilio_send(to, text)
        if p == "sns":
            return _sns_send(to, text)
        if p == "textbelt":
            return _textbelt_send(to, text)
        return {"provider": "none", "ok": False, "detail": "no SMS provider configured"}
    except Exception as e:  # a failed text must never break the agent loop
        log.warning("sms send failed via %s: %s", p, e)
        return {"provider": p, "ok": False, "detail": str(e)[:200]}


def _twilio_send(to: str, text: str) -> dict:
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
        auth=(TWILIO_SID, TWILIO_TOKEN),
        data={"From": TWILIO_FROM, "To": e164(to), "Body": text},
        timeout=20,
    )
    body = r.json()
    ok = r.status_code < 300
    return {"provider": "twilio", "ok": ok, "detail": body.get("sid") if ok else body.get("message", r.text[:200])}


def _sns_send(to: str, text: str) -> dict:
    import boto3

    sns = boto3.client("sns", region_name=os.getenv("AWS_REGION", "us-east-1"))
    r = sns.publish(
        PhoneNumber=e164(to),
        Message=text,
        MessageAttributes={"AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}},
    )
    return {"provider": "sns", "ok": True, "detail": r["MessageId"]}


def _textbelt_send(to: str, text: str) -> dict:
    r = requests.post(
        "https://textbelt.com/text",
        data={"phone": e164(to), "message": text, "key": TEXTBELT_KEY or "textbelt"},
        timeout=20,
    )
    body = r.json()
    return {
        "provider": "textbelt",
        "ok": bool(body.get("success")),
        "detail": body.get("textId") if body.get("success") else body.get("error", r.text[:200]),
        "quota_remaining": body.get("quotaRemaining"),
    }


# ---------- inbound (Twilio only; the others are one-way) ----------


def parse_twilio_webhook(form: dict) -> tuple[str, str] | None:
    """Twilio POSTs application/x-www-form-urlencoded with From and Body."""
    frm, body = form.get("From"), form.get("Body")
    if not frm or body is None:
        return None
    return frm, body.strip()


class TwilioPoller:
    """Fetch replies without a public URL: list inbound messages to our number, newest first,
    and hand over the ones we have not seen. Good enough for a demo at a 5s cadence."""

    def __init__(self):
        self.seen: set[str] = set()
        self.since = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.primed = False

    def poll(self) -> list[tuple[str, str]]:
        if provider() != "twilio":
            return []
        r = requests.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_TOKEN),
            params={"To": TWILIO_FROM, "PageSize": 20, "DateSent>": self.since.strftime("%Y-%m-%d")},
            timeout=20,
        )
        r.raise_for_status()
        out = []
        for m in r.json().get("messages", []):
            if m.get("direction") != "inbound" or m["sid"] in self.seen:
                continue
            self.seen.add(m["sid"])
            if self.primed:  # the first poll only records what already exists
                out.append((m["from"], (m.get("body") or "").strip()))
        self.primed = True
        return list(reversed(out))  # oldest first
