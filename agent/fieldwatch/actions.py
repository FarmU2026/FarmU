"""Write actions behind an approval: work order, scouting pin, insurance notice draft.

The same checks run locally and behind the Gateway (implementation-specs "Gateway tools", approval check):
the approval must exist, be `approved`, belong to this investigation, match the action type and be unexpired.
Each approval produces at most one action: the action id is derived from the approval id and written with a
conditional put, so a retry (an interrupt that re-runs its tool, a Lambda retry) returns the same action.

A work order or scouting pin is *delivered*: a text to the assignee. Delivery goes through the notify lane
(`interfaces.notify()`, WS9's send Lambda) when one is configured, else the legacy `fieldwatch.sms.send`, behind
`default_sender`. `ActionCreated` is emitted when an action is first recorded; `ActionCompleted` only when
`mark_done` flips the action to done.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import approvals
from . import contracts as C
from . import interfaces as I
from .approvals import iso
from .interfaces import FieldwatchError

log = logging.getLogger("fieldwatch.actions")

# A sender takes keyword arguments and returns {"ok": bool, "detail": str, ...}. Never raises.
# keywords: person_id, phone, body, farm_id, investigation_id, idempotency_key, link
Sender = Callable[..., dict]

# Item keys that are not part of the Action contract (the full row is interfaces.ActionRecord).
EXTRA_KEYS = set(I.ActionRecord.model_fields) - set(C.Action.model_fields)


def action_id_for(approval_id: str) -> str:
    return "act-" + approval_id.removeprefix("apr-")


def navigate_url(pin: tuple[float, float] | list[float]) -> str:
    lon, lat = pin
    return f"https://maps.google.com/?q={lat},{lon}"


# ---------- delivery ----------


def _local_phone(person_id: str) -> str | None:
    """Harness only: a phone number written next to the person in the farm file."""
    try:
        import json

        from . import config

        for p in json.loads(config.FARM_FILE.read_text()).get("people", []):
            if person_id.lower() in (str(p.get("id", "")).lower(), str(p.get("name", "")).lower()) and p.get("phone"):
                return p["phone"]
    except Exception:
        pass
    return None


def default_sender(*, person_id: str, phone: str | None, body: str, farm_id: str | None,
                   investigation_id: str, idempotency_key: str, link: dict | None = None) -> dict:
    """The notify lane (interfaces.NotifyAPI.send with a SendMessageRequest) when configured, else sms.send."""
    try:
        service = I.notify()
        if service is not None:
            req = C.SendMessageRequest(farm_id=farm_id or "", person_id=person_id, investigation_id=investigation_id,
                                       body=body, link=link, idempotency_key=idempotency_key)
            return _norm(service.send(req))
        from . import sms

        phone = phone or _local_phone(person_id)
        if not phone:
            return {"ok": False, "detail": f"no phone number for {person_id}"}
        return _norm(sms.send(phone, body))
    except Exception as e:  # a failed text must not undo a recorded action
        log.warning("delivery to %s failed: %s", person_id, e)
        return {"ok": False, "detail": str(e)[:200]}


def _norm(res: Any) -> dict:
    get = (lambda k: res.get(k)) if isinstance(res, dict) else (lambda k: getattr(res, k, None))
    status = get("status")
    ok = get("ok")
    if ok is None:
        ok = status in ("sent", "mirror_only", "deferred_quiet_hours")
    return {"ok": bool(ok), "detail": str(get("detail") or status or get("message_id") or "")}


# ---------- approval check ----------


def _require_approval(approval_id: str, investigation_id: str, action_type: str) -> C.Approval:
    """Raises POLICY_DENIED unless the approval is approved, unexpired and matches this investigation and type."""
    a = approvals.get(approval_id) if approval_id else None
    if a is None:
        raise FieldwatchError("POLICY_DENIED", "no such approval")
    if a.investigation_id != investigation_id:
        raise FieldwatchError("POLICY_DENIED", "approval belongs to a different investigation")
    if a.action_type != action_type:
        raise FieldwatchError("POLICY_DENIED", f"approval is for a {a.action_type}, not a {action_type}")
    if a.status != "approved":
        raise FieldwatchError("POLICY_DENIED", f"approval is {a.status}, not approved")
    if approvals.clock() >= approvals.parse_time(a.expires_at):
        raise FieldwatchError("POLICY_DENIED", "approval has expired")
    return a


def _to_action(item: dict) -> C.Action:
    return I.ActionRecord.model_validate(item).to_contract()


# ---------- creating actions ----------


def _create(action_type: str, *, approval_id: str, investigation_id: str, assignee_person_id: str | None,
            instructions: str, pin, field_name: str | None, farm_id: str | None, phone: str | None,
            message: Callable[[str], str] | None, sender: Sender | None) -> C.Action:
    if not instructions or not instructions.strip():
        raise FieldwatchError("INVALID_INPUT", "instructions are required")
    s = approvals.store()
    action_id = action_id_for(approval_id)

    item = s.get_action(action_id)
    if item is None:  # first call: check the approval, then claim the id
        _require_approval(approval_id, investigation_id, action_type)
        item = {
            "action_id": action_id, "approval_id": approval_id, "investigation_id": investigation_id,
            "type": action_type, "assignee_person_id": assignee_person_id,
            "pin": list(pin) if pin else None, "instructions": instructions.strip(), "status": "sent",
            "field_name": field_name, "farm_id": farm_id, "delivered": message is None,
            "created_at": iso(approvals.clock()),
        }
        item = {k: v for k, v in item.items() if v is not None}
        if s.put_action_if_absent(item):
            _emit_created(item)
        else:  # lost a race: the other call's action is the one
            item = s.get_action(action_id)
    elif item["investigation_id"] != investigation_id or item["type"] != action_type:
        raise FieldwatchError("POLICY_DENIED", "approval was already used for a different action")

    if message is not None and not item.get("delivered"):  # new, or an earlier delivery failed: (re)try
        res = (sender or default_sender)(
            person_id=item.get("assignee_person_id") or "", phone=phone, body=message(action_id),
            farm_id=farm_id or item.get("farm_id"), investigation_id=investigation_id,
            idempotency_key=f"{action_id}#text",
            link={"scope": "work_order", "subject_id": action_id, "ttl_hours": 72} if action_type == "work_order" else None)
        delivered = {"delivered": bool(res.get("ok")), "delivery_detail": str(res.get("detail", ""))[:200]}
        s.update_action(action_id, delivered, only_if={"delivered": False})
        item = {**item, **delivered}
    return _to_action(item)


def _emit_created(item: dict) -> None:
    approvals.publish_event("ActionCreated", C.ActionCreated(
        occurred_at=item["created_at"], action_id=item["action_id"], approval_id=item["approval_id"],
        investigation_id=item["investigation_id"], type=item["type"], summary=_brief(item["instructions"], 200),
        farm_id=item.get("farm_id"), assignee_person_id=item.get("assignee_person_id"),
        field_name=item.get("field_name"), delivered=bool(item.get("delivered"))))


def _brief(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def create_work_order(*, approval_id: str, investigation_id: str, assignee_person_id: str, field_id: str,
                      instructions: str, pin=None, field_name: str | None = None, farm_id: str | None = None,
                      assignee_phone: str | None = None, sender: Sender | None = None) -> C.Action:
    """Record the work order and text the assignee. Idempotent on approval_id."""
    if not assignee_person_id:
        raise FieldwatchError("INVALID_INPUT", "assignee_person_id is required")
    name = field_name or field_id

    def text(_action_id: str) -> str:
        link = f" Map: {navigate_url(pin)}" if pin else ""
        return f"Repair job from the farm: {_brief(instructions, 200)} Field: {name}.{link}"

    return _create("work_order", approval_id=approval_id, investigation_id=investigation_id,
                   assignee_person_id=assignee_person_id, instructions=instructions, pin=pin, field_name=name,
                   farm_id=farm_id, phone=assignee_phone, message=text, sender=sender)


def create_scout_pin(*, approval_id: str, investigation_id: str, person_id: str, pin, what_to_check: str,
                     field_name: str | None = None, farm_id: str | None = None, phone: str | None = None,
                     sender: Sender | None = None) -> C.Action:
    """Record a scouting pin and text it to the person who will walk it. Idempotent on approval_id."""
    if not pin:
        raise FieldwatchError("INVALID_INPUT", "pin [lon, lat] is required")

    def text(_action_id: str) -> str:
        where = f" in the {field_name}" if field_name else ""
        return f"Scouting spot{where}: {_brief(what_to_check, 200)} Map: {navigate_url(pin)}"

    return _create("scout_pin", approval_id=approval_id, investigation_id=investigation_id,
                   assignee_person_id=person_id, instructions=what_to_check, pin=pin, field_name=field_name,
                   farm_id=farm_id, phone=phone, message=text, sender=sender)


def draft_insurance_notice(*, approval_id: str, investigation_id: str, person_id: str,
                           details: str | None = None) -> C.Action:
    """Write a draft notice of loss for the farmer to review and send themselves. Nothing is sent to anyone."""
    a = approvals.get(approval_id)  # the summary is what the farmer approved; it anchors the draft
    summary = details or (a.summary if a else "")
    draft = ("DRAFT notice of possible crop loss. Review before sending to your insurance agent.\n"
             f"Date: {approvals.clock():%Y-%m-%d}\nWhat happened: {summary}\n"
             "Please add: policy number, field location and the date you first noticed it.")
    return _create("insurance_notice", approval_id=approval_id, investigation_id=investigation_id,
                   assignee_person_id=person_id, instructions=draft, pin=None, field_name=None, farm_id=None,
                   phone=None, message=None, sender=None)


# ---------- reading and finishing ----------


def get_action(action_id: str) -> C.Action | None:
    item = approvals.store().get_action(action_id)
    return _to_action(item) if item else None


def delivery(action_id: str) -> dict:
    """Whether the assignee's text went out: {"delivered": bool, "detail": str}."""
    item = approvals.store().get_action(action_id) or {}
    return {"delivered": bool(item.get("delivered")), "detail": item.get("delivery_detail", "")}


def work_order_view(action_id: str) -> C.WorkOrderView:
    """The repair person's page (GET /v1/work-orders/{action_id})."""
    item = approvals.store().get_action(action_id)
    if not item or item["type"] not in ("work_order", "scout_pin"):
        raise FieldwatchError("NOT_FOUND", f"no work order {action_id}")
    if not item.get("pin"):
        raise FieldwatchError("INVALID_INPUT", "this work order has no map pin")
    return C.WorkOrderView(action_id=action_id, field_name=item.get("field_name") or "the field", pin=tuple(item["pin"]),
                           instructions=item["instructions"], status=item["status"], navigate_url=navigate_url(item["pin"]))


def mark_done(action_id: str, by: str, photo_upload_id: str | None = None) -> C.Action:
    """The assignee finished. Emits ActionCompleted once; marking a done action again changes nothing."""
    s = approvals.store()
    item = s.get_action(action_id)
    if not item:
        raise FieldwatchError("NOT_FOUND", f"no action {action_id}")
    if item["status"] != "done":
        fields = {"status": "done", "done_at": iso(approvals.clock()), "completed_by": by}
        if photo_upload_id:
            fields["photo_upload_id"] = photo_upload_id
        if s.update_action(action_id, fields, only_if={"status": "sent"}):
            item = {**item, **fields}
            approvals.publish_event("ActionCompleted", C.ActionCompleted(
                occurred_at=fields["done_at"], action_id=action_id, investigation_id=item["investigation_id"],
                completed_by=by, farm_id=item.get("farm_id"), photo_upload_id=photo_upload_id))
        else:  # someone else finished it first
            item = s.get_action(action_id) or item
    return _to_action(item)
