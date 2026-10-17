"""Session starter: turns bus events into Runtime invocations (docs/implementation-specs.md "Agent service").

EventBridge rules on the `fieldwatch` bus send these here:
  AnomalyDetected, StartInvestigation (source fieldwatch.api)  -> create the fw-investigations item, start the agent
  ApprovalRecorded, ReplyReceived (yes/no)                     -> resume the agent at its pending interrupt
  PhotoReceived, DocumentReceived, ActionCompleted             -> recorded on the item (the agent has no contract for
                                                                   them yet; see the WS7 report)
  ReplyReceived (free_text / ask / more)                       -> a question run (below)

Question runs: a farmer's free-text message (or MORE / ASK) is never an approval and never resumes the agent. It starts
a NEW short run, a StartInvestigation with `question` set (and `about_investigation_id` when the message belongs to an
open or recently concluded investigation), whose read-only agent answers by text. An approval that is still pending
stays pending, untouched. The field is the one the text names, else the investigation's, else the farm's first.
The run's id derives from the reply_id (a duplicate event starts nothing) and its fw-investigations item has
kind "question". Only yes (with a recorded approval) and no resume an interrupt.

The agent acknowledges at once and works in the background, so this Lambda only waits for the ack.
Retries: failures raise, so EventBridge retries and finally sends the event to the rule's dead-letter queue.
Events that can never succeed (bad payload, nothing to resume) are logged and dropped without raising.

Runtime ARN: env FIELDWATCH_RUNTIME_ARN, else SSM parameter FIELDWATCH_RUNTIME_ARN_PARAM (/fieldwatch/runtime-arn),
written by hand after `agentcore deploy`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fieldwatch import contracts as C
from fieldwatch.interfaces import InvestigationRecord, Sources, Ssm, Tables

log = logging.getLogger()
log.setLevel(logging.INFO)

SESSION_PREFIX = "fieldwatch-session-"
MIN_SESSION_ID = 33  # AgentCore requires at least 33 characters
RECORD_ONLY = {"PhotoReceived", "DocumentReceived", "ActionCompleted"}
QUESTION_INTENTS = {"free_text", "ask", "more"}
YES_NO_WORDS = {"YES", "Y", "NO", "N"}  # notify turns a YES/NO with nothing pending into free_text; not a question
MORE_QUESTION = "MORE: explain the last finding in more detail"
CONTEXT_STATUSES = {"awaiting_reply", "awaiting_upload", "concluded", "running", "resuming"}
CONTEXT_DAYS = 7  # how far back "the last finding" reaches


class NotReady(Exception):
    """The investigation is mid-run; raising makes EventBridge retry the event later."""


def session_id_for(investigation_id: str) -> str:
    """fieldwatch-session-<investigation_id>, padded deterministically if the id is too short."""
    sid = f"{SESSION_PREFIX}{investigation_id}"
    if len(sid) < MIN_SESSION_ID:
        sid += "-" + hashlib.sha1(investigation_id.encode()).hexdigest()[: MIN_SESSION_ID - len(sid) - 1]
    return sid


def investigation_id_for_anomaly(anomaly_id: str) -> str:
    """anm-north-pivot-20260718 -> inv-north-pivot-20260718 (deterministic, so a duplicate event is a no-op)."""
    for p in ("anm-", "anm_"):
        if anomaly_id.startswith(p):
            return "inv" + p[3:] + anomaly_id[len(p):]
    return f"inv-{anomaly_id}"


def question_id_for(reply_id: str) -> str:
    """inv-q-<hash of the reply id>: deterministic, so a retried ReplyReceived finds its run, not a second one."""
    return "inv-q-" + hashlib.sha1(reply_id.encode()).hexdigest()[:20]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_conditional_failure(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class Starter:
    def __init__(self, table, runtime, runtime_arn_loader, fields_table=None):
        self.table = table
        self.fields_table = fields_table  # fw-fields: where a question run finds the farm's fields
        self.runtime = runtime
        self._arn_loader = runtime_arn_loader
        self._arn = None

    @property
    def runtime_arn(self) -> str:
        if not self._arn:
            self._arn = self._arn_loader()
        return self._arn

    # ------------------------------------------------------------ entry
    def handle(self, envelope: dict) -> dict:
        kind, detail = envelope.get("detail-type", ""), envelope.get("detail") or {}
        try:
            if kind == "AnomalyDetected":
                ev = C.AnomalyDetected.model_validate(detail)
                req = C.StartInvestigation(
                    investigation_id=investigation_id_for_anomaly(ev.anomaly_id),
                    farm_id=ev.field.farm_id, field=ev.field.field_id, anomaly=ev,
                )
                return self._start(req, anomaly_id=ev.anomaly_id, replay=ev.replay)
            if kind == "StartInvestigation":
                if envelope.get("source") != Sources.API:
                    return self._drop(kind, "source_not_allowed")
                req = C.StartInvestigation.model_validate(detail)
                return self._start(req, anomaly_id=req.anomaly.anomaly_id if req.anomaly else None)
            if kind == "ApprovalRecorded":
                ev = C.ApprovalRecorded.model_validate(detail)
                return self._resume(ev.investigation_id, "YES" if ev.decision == "approved" else "NO",
                                    approval_id=ev.approval_id, dedupe_key=ev.approval_id)
            if kind == "ReplyReceived":
                ev = C.ReplyReceived.model_validate(detail)
                return self._reply(ev)
            if kind in RECORD_ONLY:
                return self._record(kind, detail)
        except ValidationError as e:
            return self._drop(kind, f"invalid_payload: {e.error_count()} errors")
        return self._drop(kind, "unknown_event")

    @staticmethod
    def _drop(kind: str, why: str) -> dict:
        log.warning("dropping %s: %s", kind, why)
        return {"routed": "dropped", "event": kind, "reason": why}

    # ------------------------------------------------------------ start
    def _start(self, req: C.StartInvestigation, *, anomaly_id: str | None = None, replay: bool = False,
               kind: str | None = None, person_id: str | None = None) -> dict:
        inv = req.investigation_id
        item = InvestigationRecord(investigation_id=inv, farm_id=req.farm_id, field_id=req.field, status="queued",
                                   created_at=_now(), updated_at=_now(), replay=replay, anomaly_id=anomaly_id,
                                   kind=kind, person_id=person_id).to_item()
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(investigation_id)")
        except ClientError as e:
            if not _is_conditional_failure(e):
                raise
            existing = self.table.get_item(Key={"investigation_id": inv}).get("Item") or {}
            if existing.get("status") != "queued":  # a duplicate event; the agent already has it
                log.info("investigation %s already exists (%s); not starting again", inv, existing.get("status"))
                return {"routed": "duplicate", "investigation_id": inv}
            # still queued: an earlier attempt created the item but never reached the agent. Try again.
        # the question fields are left out unless set, so a Runtime that predates them still accepts every other start
        self._invoke(inv, req.model_dump_json(exclude=None if req.question else {"question", "about_investigation_id"}))
        return {"routed": "start", "investigation_id": inv, "session_id": session_id_for(inv)}

    # ------------------------------------------------------------ resume
    def _reply(self, ev: C.ReplyReceived) -> dict:
        if ev.intent in QUESTION_INTENTS:
            return self._question(ev)  # never resumes anything: free text is not an approval
        if not ev.investigation_id:
            return self._drop("ReplyReceived", "not_for_an_investigation")
        if ev.intent == "no":
            return self._resume(ev.investigation_id, "NO", approval_id=ev.approval_id,
                                dedupe_key=ev.approval_id or ev.reply_id)
        if ev.intent == "yes":
            if not ev.approval_id:  # a YES only counts once it is a recorded approval
                return self._drop("ReplyReceived", "yes_without_approval")
            return self._resume(ev.investigation_id, "YES", approval_id=ev.approval_id, dedupe_key=ev.approval_id)
        return self._drop("ReplyReceived", f"intent_{ev.intent}_does_not_resume_yet")

    # ------------------------------------------------------------ questions
    def _question(self, ev: C.ReplyReceived) -> dict:
        """Start a question run for a farmer's message. Any open investigation's pending interrupt is not touched."""
        text = " ".join(ev.text.split())
        if not text or (ev.intent == "free_text" and text.upper().rstrip(".!") in YES_NO_WORDS):
            return self._drop("ReplyReceived", "nothing_to_answer")
        question = MORE_QUESTION if ev.intent == "more" else text[:480]
        fields = self._fields(ev.farm_id)
        # The field the question is about: the one the text names, else the field of the alert being replied to, else
        # the newest recent one. Another field's investigation is never attached (its pending approval would bleed in).
        named = self._named_field(text, fields)
        about = self._about(ev, fields, named)
        field = named or self._pick_field(text, fields, about)
        if about and about.get("field_id") != field:
            about = None
        if not field:
            return self._drop("ReplyReceived", "question_without_a_field")
        req = C.StartInvestigation(investigation_id=question_id_for(ev.reply_id), farm_id=ev.farm_id, field=field,
                                   question=question, about_investigation_id=about["investigation_id"] if about else None)
        return self._start(req, kind="question", person_id=ev.person_id)

    def _fields(self, farm_id: str) -> list[dict]:
        if self.fields_table is None:
            return []
        return self.fields_table.query(KeyConditionExpression=Key("farm_id").eq(farm_id)).get("Items", [])

    def _about(self, ev: C.ReplyReceived, fields: list[dict], field_id: str | None = None) -> dict | None:
        """The investigation a question is about: the one the reply names, else the newest recent one of the field the
        text names (or, when no field is named, of any field). Question runs themselves never count, and neither does
        another farm's item or another field's."""
        if ev.investigation_id:
            item = self.table.get_item(Key={"investigation_id": ev.investigation_id}).get("Item")
            if (item and item.get("farm_id") == ev.farm_id and item.get("kind") != "question"
                    and (field_id is None or item.get("field_id") == field_id)):
                return item
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CONTEXT_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        found = []
        for f in fields:
            if field_id is not None and f["field_id"] != field_id:
                continue
            page = self.table.query(IndexName="by-field", KeyConditionExpression=Key("field_id").eq(f["field_id"]),
                                    ScanIndexForward=False, Limit=10)
            found += [i for i in page.get("Items", []) if i.get("farm_id") == ev.farm_id and i.get("kind") != "question"
                      and i.get("status") in CONTEXT_STATUSES and i.get("created_at", "") >= cutoff]
        return max(found, key=lambda i: i["created_at"], default=None)

    @staticmethod
    def _named_field(text: str, fields: list[dict]) -> str | None:
        """The field the text names (case-insensitive, by name or id; longest name wins), or None."""
        low = text.lower()

        def mentioned(f: dict) -> bool:  # whole words, so a field called "A" is not found in every sentence
            return any(n and re.search(rf"(?<!\w){re.escape(n)}(?!\w)", low)
                       for n in (str(f.get("name", "")).lower(), f["field_id"].lower()))

        named = [f for f in fields if mentioned(f)]
        return max(named, key=lambda f: len(str(f.get("name", "")))).get("field_id") if named else None

    @classmethod
    def _pick_field(cls, text: str, fields: list[dict], about: dict | None) -> str | None:
        """The field the text names, else the investigation's field, else the farm's first."""
        named = cls._named_field(text, fields)
        if named:
            return named
        if about and about.get("field_id"):
            return about["field_id"]
        return fields[0]["field_id"] if fields else None

    def _claim(self, inv: str, key: str) -> str | None:
        """Atomically take the pending interrupt id (once per dedupe key). None if there's nothing to take."""
        try:
            out = self.table.update_item(
                Key={"investigation_id": inv},
                UpdateExpression="SET #s = :resuming, updated_at = :t ADD handled_keys :k REMOVE pending_interrupt_id",
                ConditionExpression="attribute_exists(pending_interrupt_id) AND "
                                    "(attribute_not_exists(handled_keys) OR NOT contains(handled_keys, :key))",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":resuming": "resuming", ":t": _now(), ":k": {key}, ":key": key},
                ReturnValues="ALL_OLD",
            )
            return out["Attributes"]["pending_interrupt_id"]
        except ClientError as e:
            if not _is_conditional_failure(e):
                raise
            return None

    def _release(self, inv: str, key: str, interrupt_id: str) -> None:
        """Undo _claim after a failed invoke so the retry can claim again."""
        self.table.update_item(
            Key={"investigation_id": inv},
            UpdateExpression="SET pending_interrupt_id = :i, #s = :s, updated_at = :t DELETE handled_keys :k",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":i": interrupt_id, ":s": "awaiting_reply", ":t": _now(), ":k": {key}},
        )

    def _resume(self, inv: str, text: str, *, approval_id: str | None, dedupe_key: str) -> dict:
        interrupt_id = self._claim(inv, dedupe_key)
        if interrupt_id is None:
            item = self.table.get_item(Key={"investigation_id": inv}).get("Item") or {}
            if dedupe_key in (item.get("handled_keys") or set()):
                return {"routed": "duplicate", "investigation_id": inv}
            if item.get("status") in ("queued", "running", "resuming"):
                raise NotReady(f"{inv} is {item['status']}; retry later")
            return self._drop("resume", f"no_pending_interrupt for {inv} (status {item.get('status')})")
        req = C.ResumeInvestigation(investigation_id=inv, interrupt_id=interrupt_id, text=text, approval_id=approval_id)
        try:
            self._invoke(inv, req.model_dump_json())
        except Exception:
            self._release(inv, dedupe_key, interrupt_id)
            raise
        return {"routed": "resume", "investigation_id": inv, "session_id": session_id_for(inv), "text": text}

    # ------------------------------------------------------------ record only
    def _record(self, kind: str, detail: dict) -> dict:
        inv = detail.get("investigation_id")
        if not inv:
            return self._drop(kind, "not_for_an_investigation")
        try:
            self.table.update_item(
                Key={"investigation_id": inv},
                UpdateExpression="SET last_event = :e, last_event_type = :k, updated_at = :t",
                ConditionExpression="attribute_exists(investigation_id)",
                ExpressionAttributeValues={":e": json.dumps(detail)[:3000], ":k": kind, ":t": _now()},
            )
        except ClientError as e:
            if _is_conditional_failure(e):
                return self._drop(kind, f"unknown_investigation {inv}")
            raise
        return {"routed": "recorded", "event": kind, "investigation_id": inv}

    # ------------------------------------------------------------ the Runtime call
    def _invoke(self, inv: str, body: str) -> None:
        resp = self.runtime.invoke_agent_runtime(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=session_id_for(inv),
            contentType="application/json",
            accept="application/json",
            payload=body.encode(),
        )
        raw = resp["response"].read() if hasattr(resp.get("response"), "read") else resp.get("response", b"")
        try:
            ack = json.loads(raw or b"{}")
        except ValueError:
            raise RuntimeError(f"Runtime returned a non-JSON acknowledgement: {raw[:200]!r}")
        if "error" in ack:
            raise RuntimeError(f"Runtime rejected the request: {ack['error']}")
        if ack.get("accepted") is False:
            raise NotReady(f"Runtime did not accept {inv}: {ack.get('reason')}")
        log.info("started/resumed %s on session %s", inv, session_id_for(inv))


# ---------------------------------------------------------------- Lambda wiring

_starter: Starter | None = None


def _default_starter() -> Starter:
    import boto3
    from botocore.config import Config

    region = os.getenv("AWS_REGION", "us-west-2")

    def load_arn() -> str:
        arn = os.getenv("FIELDWATCH_RUNTIME_ARN", "")
        if arn:
            return arn
        param = os.getenv("FIELDWATCH_RUNTIME_ARN_PARAM", Ssm.RUNTIME_ARN)
        return boto3.client("ssm", region_name=region).get_parameter(Name=param)["Parameter"]["Value"]

    table = boto3.resource("dynamodb", region_name=region).Table(os.getenv("FIELDWATCH_INVESTIGATIONS_TABLE", Tables.INVESTIGATIONS))
    fields = boto3.resource("dynamodb", region_name=region).Table(os.getenv("FIELDWATCH_FIELDS_TABLE", Tables.FIELDS))
    runtime = boto3.client("bedrock-agentcore", region_name=region,
                           config=Config(read_timeout=50, connect_timeout=5, retries={"max_attempts": 1}))
    return Starter(table, runtime, load_arn, fields)


def handler(event, context=None) -> dict:
    global _starter
    if _starter is None:
        _starter = _default_starter()
    result = _starter.handle(event)
    log.info("result %s", json.dumps(result))
    return result
