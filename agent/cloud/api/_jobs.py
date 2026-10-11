"""Hand-offs to other subsystems. The API never runs imagery or agents itself.

Every target is another lane's Lambda or state machine, named by an interfaces.Env variable; each call does
nothing (returns None) when its variable is unset:
  FIELDWATCH_CHECK_FN          scenes check-latest Lambda (WS2), async, contracts.CheckLatestRequest
  FIELDWATCH_REPLAY_FN         scenes replay Lambda (WS2), async, contracts.ReplayRequest
  FIELDWATCH_RESET_FN          scenes reset Lambda (WS2), sync, contracts.ResetRequest
  FIELDWATCH_CONTEXT_SM_ARN    context builder state machine (WS15)
  FIELDWATCH_SEND_FN           notify send Lambda (WS9), async, contracts.SendMessageRequest

The API never writes to fw-scene-queue: the queue only takes contracts.SceneQueueMessage bodies (STAC items
and replay envelopes), which the scenes lane builds.
"""
from __future__ import annotations

import json
import os
import re

from fieldwatch import contracts as C
from fieldwatch.interfaces import Env

from . import _aws
from ._http import dumps, log


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def configured(env_name: str) -> bool:
    return bool(_env(env_name))


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


def scene_check(farm_id: str, fields: list[dict], *, reason: str) -> dict | None:
    """'Check now' / sign-up: the scenes lane's check-latest Lambda searches Earth Search for recent clear
    passes over these fields' tiles and queues them. None when no check Lambda is configured."""
    req = C.CheckLatestRequest(farm_id=farm_id, field_ids=[f["field_id"] for f in fields], reason=reason,
                               requested_at=_aws.now_iso())
    return invoke(Env.CHECK_FN, req.model_dump(mode="json"))


def replay(req: C.ReplayRequest) -> dict | None:
    return invoke(Env.REPLAY_FN, req.model_dump(mode="json"))


def reset(req: C.ResetRequest) -> dict | None:
    return invoke(Env.RESET_FN, req.model_dump(mode="json"), wait=True)


def start_context_build(farm_id: str, field_ids: list[str]) -> str | None:
    """Start the context builder (Step Functions Standard). Input: {schema_version, farm_id, field_ids}.
    Returns the execution name (our context_build_id) or None."""
    arn = _env(Env.CONTEXT_SM_ARN)
    if not arn:
        return None
    name = re.sub(r"[^A-Za-z0-9_-]", "-", f"{farm_id}-{_aws.now_iso()}")[:80]
    _aws.client("stepfunctions").start_execution(
        stateMachineArn=arn, name=name,
        input=json.dumps({"schema_version": "1", "farm_id": farm_id, "field_ids": field_ids}))
    return name


def send_message(req: C.SendMessageRequest) -> bool:
    """Ask the notify send Lambda (WS9) to text someone. False when no send Lambda is configured."""
    try:
        return invoke(Env.SEND_FN, req.model_dump(mode="json")) is not None
    except Exception:  # noqa: BLE001 - a failed text must not fail the API call
        log.exception("send Lambda invoke failed")
        return False
