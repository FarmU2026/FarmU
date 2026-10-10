"""Approval records: the gate between "the agent wants to act" and "something is sent" (docs/decisions.md D6).

Rules enforced here, so they hold the same way locally and in the cloud:
- a record is written by `create_pending` (when a write tool asks) and changed only by `record_decision`
  (the reply handler or the report page), never by the agent;
- a decision is accepted only from the person asked and, for texts, only from the number the question went to;
- only while unexpired, and only once: the write is conditional on `status == "pending"`;
- a second answer gets ALREADY_DECIDED with who decided and when.

Storage sits behind a tiny interface with two backends, picked by FIELDWATCH_APPROVALS_BACKEND:
  local   (default) a JSON file under agent/data/ (FIELDWATCH_APPROVALS_DIR overrides): the FastAPI harness and tests.
  dynamo  the `fw-approvals` / `fw-actions` tables (config.TABLE_PREFIX) and the EventBridge bus.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from . import config
from . import contracts as C

log = logging.getLogger("fieldwatch.approvals")

DEFAULT_TTL_HOURS = 24
KEEP_AFTER_EXPIRY = timedelta(days=7)  # DynamoDB TTL deletes lazily; keep the record so pages can say "expired"
ACTION_TYPES = ("work_order", "scout_pin", "photo_request", "insurance_notice")


# ---------- errors (codes from implementation-specs "Gateway tools" / "Web API") ----------


class FieldwatchError(Exception):
    """Carries a spec error code: NOT_FOUND, POLICY_DENIED, ALREADY_DECIDED, EXPIRED, INVALID_INPUT, ..."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, **details: Any):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.retryable, self.details = code, message, retryable, details

    def body(self) -> dict:
        """The spec's ErrorBody shape."""
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable, **self.details}}


# ---------- time ----------


def _now() -> datetime:
    return datetime.now(timezone.utc)


clock: Callable[[], datetime] = _now  # tests replace this


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _expired(item: dict) -> bool:
    return clock() >= parse_time(item["expires_at"])


# ---------- storage interface ----------

# Internal items are the Approval fields plus: created_at, sent_to_phone (optional), expires_at_epoch (TTL).
INTERNAL_KEYS = {"created_at", "sent_to_phone", "expires_at_epoch"}


class Store(Protocol):
    def put_approval_if_absent(self, item: dict) -> bool: ...
    def get_approval(self, approval_id: str) -> dict | None: ...
    def decide(self, approval_id: str, new_status: str, fields: dict) -> bool:
        """Set status and fields only if the current status is 'pending'. True if it applied."""
    def pending_for_person(self, person_id: str) -> list[dict]: ...
    def put_action_if_absent(self, item: dict) -> bool: ...
    def get_action(self, action_id: str) -> dict | None: ...
    def update_action(self, action_id: str, fields: dict, only_if: dict | None = None) -> bool:
        """Merge fields; if only_if is given, apply only when every key/value matches. True if it applied."""


class LocalStore:
    """JSON file with an in-process lock. Enough for the single-process harness and tests."""

    _lock = threading.RLock()

    def __init__(self, directory: Path):
        self.path = Path(directory) / "approvals.json"

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"approvals": {}, "actions": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.path)

    def put_approval_if_absent(self, item):
        with self._lock:
            data = self._load()
            if item["approval_id"] in data["approvals"]:
                return False
            data["approvals"][item["approval_id"]] = item
            self._save(data)
            return True

    def get_approval(self, approval_id):
        with self._lock:
            return self._load()["approvals"].get(approval_id)

    def decide(self, approval_id, new_status, fields):
        with self._lock:
            data = self._load()
            cur = data["approvals"].get(approval_id)
            if not cur or cur["status"] != "pending":
                return False
            cur.update(fields, status=new_status)
            self._save(data)
            return True

    def pending_for_person(self, person_id):
        with self._lock:
            return [a for a in self._load()["approvals"].values()
                    if a["sent_to_person_id"] == person_id and a["status"] == "pending"]

    def put_action_if_absent(self, item):
        with self._lock:
            data = self._load()
            if item["action_id"] in data["actions"]:
                return False
            data["actions"][item["action_id"]] = item
            self._save(data)
            return True

    def get_action(self, action_id):
        with self._lock:
            return self._load()["actions"].get(action_id)

    def update_action(self, action_id, fields, only_if=None):
        with self._lock:
            data = self._load()
            cur = data["actions"].get(action_id)
            if not cur or any(cur.get(k) != v for k, v in (only_if or {}).items()):
                return False
            cur.update(fields)
            self._save(data)
            return True


class DynamoStore:
    """fw-approvals (pk approval_id; GSI by-person-status) and fw-actions (pk action_id)."""

    def __init__(self, resource=None, prefix: str | None = None):
        import boto3

        self.ddb = resource or boto3.resource("dynamodb", region_name=config.REGION)
        prefix = config.TABLE_PREFIX if prefix is None else prefix
        self.approvals = self.ddb.Table(f"{prefix}approvals")
        self.actions = self.ddb.Table(f"{prefix}actions")

    @staticmethod
    def _clean(value):
        """boto3 rejects float and NULL-typed GSI keys: drop Nones, turn floats into Decimal."""
        from decimal import Decimal

        if isinstance(value, dict):
            return {k: DynamoStore._clean(v) for k, v in value.items() if v is not None}
        if isinstance(value, (list, tuple)):
            return [DynamoStore._clean(v) for v in value]
        return Decimal(str(value)) if isinstance(value, float) else value

    @staticmethod
    def _plain(value):
        """boto3 returns Decimal for numbers; make them ints/floats so JSON and pydantic are happy."""
        if value is None:
            return None
        if isinstance(value, dict):
            return {k: DynamoStore._plain(v) for k, v in value.items()}
        if isinstance(value, list):
            return [DynamoStore._plain(v) for v in value]
        if hasattr(value, "as_tuple"):
            return int(value) if value == int(value) else float(value)
        return value

    def _put_if_absent(self, table, key: str, item: dict) -> bool:
        from botocore.exceptions import ClientError

        try:
            table.put_item(Item=self._clean(item), ConditionExpression=f"attribute_not_exists({key})")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def put_approval_if_absent(self, item):
        item = {**item, "expires_at_epoch": int((parse_time(item["expires_at"]) + KEEP_AFTER_EXPIRY).timestamp())}
        return self._put_if_absent(self.approvals, "approval_id", item)

    def get_approval(self, approval_id):
        return self._plain(self.approvals.get_item(Key={"approval_id": approval_id}, ConsistentRead=True).get("Item"))

    def decide(self, approval_id, new_status, fields):
        from botocore.exceptions import ClientError

        names = {"#s": "status"}
        values = {":pending": "pending", ":new": new_status}
        sets = ["#s = :new"]
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"], values[f":f{i}"] = k, self._clean(v)
            sets.append(f"#f{i} = :f{i}")
        try:
            self.approvals.update_item(
                Key={"approval_id": approval_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="#s = :pending",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def pending_for_person(self, person_id):
        from boto3.dynamodb.conditions import Key

        r = self.approvals.query(
            IndexName="by-person-status",
            KeyConditionExpression=Key("sent_to_person_id").eq(person_id) & Key("status").eq("pending"),
        )
        # GSIs are eventually consistent; re-read each hit so a just-decided one doesn't come back.
        items = (self.get_approval(i["approval_id"]) for i in r.get("Items", []))
        return [a for a in items if a and a["status"] == "pending"]

    def put_action_if_absent(self, item):
        return self._put_if_absent(self.actions, "action_id", item)

    def get_action(self, action_id):
        return self._plain(self.actions.get_item(Key={"action_id": action_id}, ConsistentRead=True).get("Item"))

    def update_action(self, action_id, fields, only_if=None):
        from botocore.exceptions import ClientError

        names, values, sets, conds = {}, {}, [], ["attribute_exists(action_id)"]
        for i, (k, v) in enumerate(fields.items()):
            names[f"#f{i}"], values[f":f{i}"] = k, self._clean(v)
            sets.append(f"#f{i} = :f{i}")
        for j, (k, v) in enumerate((only_if or {}).items()):
            names[f"#c{j}"], values[f":c{j}"] = k, v
            conds.append(f"#c{j} = :c{j}")
        try:
            self.actions.update_item(
                Key={"action_id": action_id},
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=" AND ".join(conds),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


_store_override: Store | None = None
_store_cache: dict[tuple, Store] = {}


def use_store(store: Store | None) -> None:
    """Tests (and a Lambda that already holds a boto3 resource) inject a store."""
    global _store_override
    _store_override = store


def backend() -> str:
    return os.getenv("FIELDWATCH_APPROVALS_BACKEND", "local").lower()


def store() -> Store:
    if _store_override is not None:
        return _store_override
    b = backend()
    if b == "dynamo":
        key = ("dynamo", config.TABLE_PREFIX, config.REGION)
        if key not in _store_cache:
            _store_cache[key] = DynamoStore()
        return _store_cache[key]
    if b != "local":
        raise FieldwatchError("INVALID_INPUT", f"unknown FIELDWATCH_APPROVALS_BACKEND {b!r} (use local or dynamo)")
    directory = Path(os.getenv("FIELDWATCH_APPROVALS_DIR", str(config.DATA)))
    key = ("local", str(directory))
    if key not in _store_cache:
        _store_cache[key] = LocalStore(directory)
    return _store_cache[key]


# ---------- events ----------

_event_sink: Callable[[str, dict], None] | None = None


def set_event_sink(sink: Callable[[str, dict], None] | None) -> None:
    """Redirect events (detail_type, detail) to a callable instead of the bus. Local harness and tests use this."""
    global _event_sink
    _event_sink = sink


def publish_event(detail_type: str, model: C.Contract) -> None:
    """Put one event on the `fieldwatch` bus (source fieldwatch.approvals). Never raises: the decision is
    already recorded, and a re-emit is available through `emit_approval_recorded`."""
    detail = model.model_dump(mode="json")
    if _event_sink is not None:
        _event_sink(detail_type, detail)
        return
    if backend() != "dynamo":
        return
    try:
        import boto3

        boto3.client("events", region_name=config.REGION).put_events(Entries=[{
            "EventBusName": config.EVENT_BUS,
            "Source": "fieldwatch.approvals",
            "DetailType": detail_type,
            "Detail": json.dumps(detail),
        }])
    except Exception:
        log.exception("could not publish %s", detail_type)


def emit_approval_recorded(a: C.Approval) -> None:
    publish_event("ApprovalRecorded", C.ApprovalRecorded(
        occurred_at=a.decided_at or iso(clock()), approval_id=a.approval_id, investigation_id=a.investigation_id,
        decision=a.status, decided_by=a.decided_by, channel=a.channel))  # type: ignore[arg-type]


# ---------- public functions ----------


def _to_model(item: dict) -> C.Approval:
    """Apply expiry on read: a pending record past its time reads as 'expired'."""
    data = {k: v for k, v in item.items() if k not in INTERNAL_KEYS}
    if data["status"] == "pending" and _expired(data):
        data["status"] = "expired"
    return C.Approval.model_validate(data)


def approval_id_for(investigation_id: str, action_type: str, summary: str) -> str:
    """Deterministic id, so a write tool that re-runs when its interrupt resumes finds the same record."""
    h = hashlib.sha1(f"{investigation_id}|{action_type}|{summary}".encode()).hexdigest()[:12]
    return f"apr-{h}"


def e164(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return "+" + (digits if len(digits) != 10 else "1" + digits)


def create_pending(investigation_id: str, action_type: str, summary: str, sent_to_person_id: str,
                   ttl_hours: float = DEFAULT_TTL_HOURS, *, approval_id: str | None = None,
                   sent_to_phone: str | None = None) -> C.Approval:
    """Write a pending approval. With an explicit `approval_id` this is idempotent: a second call returns
    the existing record untouched (whatever its status)."""
    if action_type not in ACTION_TYPES:
        raise FieldwatchError("INVALID_INPUT", f"unknown action type {action_type!r}")
    if not (investigation_id and summary and sent_to_person_id):
        raise FieldwatchError("INVALID_INPUT", "investigation_id, summary and sent_to_person_id are required")
    now = clock()
    item = {
        "approval_id": approval_id or f"apr-{uuid.uuid4().hex[:12]}",
        "investigation_id": investigation_id,
        "action_type": action_type,
        "summary": summary,
        "sent_to_person_id": sent_to_person_id,
        "status": "pending",
        "expires_at": iso(now + timedelta(hours=ttl_hours)),
        "created_at": iso(now),
        "sent_to_phone": e164(sent_to_phone) if sent_to_phone else None,
    }
    item = {k: v for k, v in item.items() if v is not None}
    s = store()
    if not s.put_approval_if_absent(item):
        return _to_model(s.get_approval(item["approval_id"]))
    return _to_model(item)


def get(approval_id: str) -> C.Approval | None:
    item = store().get_approval(approval_id)
    return _to_model(item) if item else None


def latest_pending_for(person_id: str) -> C.Approval | None:
    """The person's newest pending, unexpired approval (the reply handler's fallback when a YES doesn't say what it answers)."""
    live = [a for a in store().pending_for_person(person_id) if not _expired(a)]
    if not live:
        return None
    return _to_model(max(live, key=lambda a: a.get("created_at", "")))


_DECISIONS = {
    "approved": "approved", "approve": "approved", "yes": "approved", "y": "approved",
    "declined": "declined", "decline": "declined", "no": "declined", "n": "declined",
}


def record_decision(approval_id: str, decision: str, decided_by_person_id: str, channel: str,
                    from_phone: str | None = None) -> C.Approval:
    """Record the answer. Raises FieldwatchError: NOT_FOUND, POLICY_DENIED (wrong person or number),
    ALREADY_DECIDED (details: decided_by, decided_at, status), EXPIRED, INVALID_INPUT."""
    status = _DECISIONS.get(str(decision).strip().lower())
    if status is None:
        raise FieldwatchError("INVALID_INPUT", f"decision must be approve or decline, got {decision!r}")
    if channel not in ("sms", "web", "voice"):
        raise FieldwatchError("INVALID_INPUT", f"unknown channel {channel!r}")
    s = store()
    item = s.get_approval(approval_id)
    if not item:
        raise FieldwatchError("NOT_FOUND", f"no approval {approval_id}")
    if decided_by_person_id != item["sent_to_person_id"]:
        raise FieldwatchError("POLICY_DENIED", "this approval was asked of someone else")
    if channel == "sms":
        if not from_phone:
            raise FieldwatchError("INVALID_INPUT", "a text reply needs the number it came from")
        if item.get("sent_to_phone") and e164(from_phone) != item["sent_to_phone"]:
            raise FieldwatchError("POLICY_DENIED", "reply came from a different number than the question went to")
    _raise_if_decided(item)
    if _expired(item):
        s.decide(approval_id, "expired", {})  # best effort; reads already report expired
        raise FieldwatchError("EXPIRED", "this approval has expired", expires_at=item["expires_at"])
    fields = {"decided_by": decided_by_person_id, "decided_at": iso(clock()), "channel": channel}
    if not s.decide(approval_id, status, fields):
        _raise_if_decided(s.get_approval(approval_id) or item)
        raise FieldwatchError("ALREADY_DECIDED", "this approval changed while recording", retryable=True)
    result = _to_model(s.get_approval(approval_id))
    emit_approval_recorded(result)
    return result


def _raise_if_decided(item: dict) -> None:
    if item["status"] == "expired":
        raise FieldwatchError("EXPIRED", "this approval has expired", expires_at=item["expires_at"])
    if item["status"] != "pending":
        who, when = item.get("decided_by"), item.get("decided_at")
        text = f"already {item['status']}" + (f" by {who}" if who else "") + (f" at {when}" if when else "")
        raise FieldwatchError("ALREADY_DECIDED", text, status=item["status"], decided_by=who, decided_at=when)
