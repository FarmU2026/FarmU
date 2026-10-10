"""Inbound reply hand-off (SMS webhook and the web phone panel / mirror reply).

Preferred path: the notifications lane (WS9, Kurt) provides

    fieldwatch.notify.inbound.handle(from_phone: str, body: str, channel: str) -> dict
        channel: "sms" (Twilio webhook, already signature-checked and deduped on MessageSid by the API)
                 or "mirror" (typed into the web phone panel; treat exactly like an SMS).
        Does the reply-handler steps in docs/implementation-specs.md "Reply handler": person lookup by
        phone, intents, STOP/HELP, approval matching via approvals.record_decision, ReplyReceived /
        ApprovalRecorded / MessageReceived. Returns a small dict, e.g. {"status": "...", "intent": "..."}.
        It may send texts (unknown number, HELP) through the send path; the API never does.

Until that module exists, `fallback()` does a minimal version so the demo loop works.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from fieldwatch import contracts as C

from . import _approvals, _aws, store
from ._http import ApiError, log

YES, NO = {"YES", "Y"}, {"NO", "N"}
KEYWORDS = {"MORE": "more", "PHOTO": "photo", "CALL": "call", "HELP": "help",
            "STOP": "stop", "UNSUBSCRIBE": "stop", "QUIT": "stop", "CANCEL": "stop",
            "START": "start", "UNSTOP": "start"}
OPEN_STATUSES = {"open", "running", "investigating", "waiting", "waiting_for_farmer", "started"}


def _inbound_module():
    try:
        return importlib.import_module("fieldwatch.notify.inbound")
    except ModuleNotFoundError as e:
        if e.name in ("fieldwatch.notify.inbound", "fieldwatch.notify"):
            return None
        raise


def handle(from_phone: str, body: str, channel: str) -> dict:
    mod = _inbound_module()
    if mod is not None and hasattr(mod, "handle"):
        return mod.handle(from_phone, body, channel) or {}
    return fallback(from_phone, body, channel)


def classify(text: str) -> str:
    t = text.strip().upper().rstrip(".!")
    if t in YES:
        return "yes"
    if t in NO:
        return "no"
    if t == "ASK" or t.startswith("ASK "):
        return "ask"
    return KEYWORDS.get(t, "free_text")


def _pending_approval(person_id: str) -> dict | None:
    items = _aws.query_all(_aws.table("approvals"), IndexName="by-person-status",
                           KeyConditionExpression=Key("sent_to_person_id").eq(person_id) & Key("status").eq("pending"))
    now = _aws.now_iso()
    live = [a for a in items if str(a.get("expires_at", "9999")) > now]
    live.sort(key=lambda a: str(a.get("created_at") or a.get("expires_at") or ""), reverse=True)
    return live[0] if live else None


def _open_investigation(farm_id: str) -> str | None:
    """Most recent open investigation on any of the farm's fields, within 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    best: dict | None = None
    for f in store.list_fields(farm_id):
        page = _aws.table("investigations").query(
            IndexName="by-field", KeyConditionExpression=Key("field_id").eq(f["field_id"]) & Key("created_at").gt(cutoff),
            ScanIndexForward=False, Limit=5)
        for inv in (_aws.from_ddb(i) for i in page.get("Items", [])):
            if str(inv.get("status", "open")) in OPEN_STATUSES and (not best or inv["created_at"] > best["created_at"]):
                best = inv
    return best["investigation_id"] if best else None


def fallback(from_phone: str, body: str, channel: str) -> dict:
    person = store.person_by_phone(from_phone)
    if not person:
        return {"status": "unknown_number"}
    farm_id, person_id = person["farm_id"], person["person_id"]
    text = body.strip()
    intent = classify(text)
    _aws.put_event("MessageReceived", C.MessageReceived(
        occurred_at=_aws.now_iso(), message_id=store.new_id("msg_"), farm_id=farm_id, person_id=person_id, body=text))

    if intent in ("stop", "start"):
        stop = intent == "stop"
        _aws.table("farms").update_item(Key={"farm_id": farm_id, "item": person["item"]},
                                        UpdateExpression="SET opted_out = :o", ExpressionAttributeValues={":o": stop})
        if person_id == store.get_farm_item(farm_id).get("owner_person_id"):
            _aws.table("farms").update_item(Key={"farm_id": farm_id, "item": "FARM"},
                                            UpdateExpression="SET muted = :o", ExpressionAttributeValues={":o": stop})
        return {"status": "opted_out" if stop else "opted_in", "intent": intent}
    if intent == "help":
        return {"status": "help", "intent": intent}

    approval_id = investigation_id = None
    if intent in ("yes", "no"):
        appr = _pending_approval(person_id)
        if appr:
            approval_id, investigation_id = appr["approval_id"], appr.get("investigation_id")
            if _approvals.available():
                try:
                    _approvals.record_decision(approval_id, "approved" if intent == "yes" else "declined",
                                               person_id, "sms", from_phone=from_phone)
                except ApiError as e:
                    # Someone else answered first, it expired, or the number didn't match: not a server error.
                    if e.status >= 500:
                        raise
                    log.warning("approval %s not recorded: %s %s", approval_id, e.code, e.message)
                    return {"status": "approval_not_recorded", "intent": intent, "approval_id": approval_id,
                            "code": e.code, "message": e.message}
                return {"status": "approval_recorded", "intent": intent, "approval_id": approval_id}
            log.warning("approvals module missing; passing %s on as ReplyReceived", approval_id)
    if investigation_id is None:
        investigation_id = _open_investigation(farm_id)
    _aws.put_event("ReplyReceived", C.ReplyReceived(
        occurred_at=_aws.now_iso(), reply_id=store.new_id("rep_"), farm_id=farm_id, person_id=person_id,
        intent=intent if intent != "call" else "free_text", text=text, investigation_id=investigation_id,
        approval_id=approval_id))
    return {"status": "reply_received", "intent": intent, "investigation_id": investigation_id}
