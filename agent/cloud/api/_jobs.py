"""Hand-offs to other subsystems. The API never runs imagery or agents itself.

All targets are optional: each reads its env var and does nothing (returns None) when unset.
  FIELDWATCH_SCENE_QUEUE_URL   fw-scene-queue (WS2): check and replay envelopes
  FIELDWATCH_CONTEXT_SM_ARN    context builder state machine (WS15)
  FIELDWATCH_REPLAY_FN         replay Lambda (WS2), invoked asynchronously
  FIELDWATCH_RESET_FN          reset Lambda (WS2), invoked synchronously
  FIELDWATCH_SEND_FN           notify send Lambda (WS9), invoked asynchronously with a SendMessageRequest
"""
from __future__ import annotations

import json
import os
import re

from fieldwatch import contracts as C

from . import _aws
from ._http import dumps, log


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def send_scene_message(message: dict) -> str | None:
    """Envelope on fw-scene-queue. Returns the SQS MessageId, or None if no queue is configured."""
    url = _env("FIELDWATCH_SCENE_QUEUE_URL")
    if not url:
        return None
    out = _aws.client("sqs").send_message(QueueUrl=url, MessageBody=dumps(message))
    return out.get("MessageId")


def scene_check(farm_id: str, fields: list[dict], *, reason: str) -> str | None:
    """'Check now': the scene pipeline searches for the latest clear pass for these fields.

    Envelope (WS2 scene filter must accept it):
      {schema_version, type: "check", farm_id, field_ids, fields: [{field_id, polygon, crop}],
       as_of: null, reason: "signup" | "check_now", requested_at}
    """
    return send_scene_message({
        "schema_version": "1", "type": "check", "farm_id": farm_id,
        "field_ids": [f["field_id"] for f in fields],
        "fields": [{"field_id": f["field_id"], "polygon": f.get("polygon"), "crop": f.get("crop")} for f in fields],
        "as_of": None, "reason": reason, "requested_at": _aws.now_iso(),
    })


def start_context_build(farm_id: str, field_ids: list[str]) -> str | None:
    """Start the context builder (Step Functions Standard). Input: {schema_version, farm_id, field_ids}.
    Returns the execution name (our context_build_id) or None."""
    arn = _env("FIELDWATCH_CONTEXT_SM_ARN")
    if not arn:
        return None
    name = re.sub(r"[^A-Za-z0-9_-]", "-", f"{farm_id}-{_aws.now_iso()}")[:80]
    _aws.client("stepfunctions").start_execution(
        stateMachineArn=arn, name=name,
        input=json.dumps({"schema_version": "1", "farm_id": farm_id, "field_ids": field_ids}))
    return name


def invoke(env_name: str, payload: dict, *, wait: bool = False) -> dict | None:
    fn = _env(env_name)
    if not fn:
        return None
    out = _aws.client("lambda").invoke(FunctionName=fn, InvocationType="RequestResponse" if wait else "Event",
                                       Payload=dumps(payload).encode())
    if wait:
        body = out["Payload"].read()
        try:
            return json.loads(body) if body else {}
        except ValueError:
            return {"raw": body.decode("utf-8", "replace")}
    return {"status_code": out.get("StatusCode")}


def send_message(req: C.SendMessageRequest) -> bool:
    """Ask the notify send Lambda (WS9) to text someone. False when no send Lambda is configured."""
    try:
        return invoke("FIELDWATCH_SEND_FN", req.model_dump(mode="json")) is not None
    except Exception:  # noqa: BLE001 - a failed text must not fail the API call
        log.exception("send Lambda invoke failed")
        return False
