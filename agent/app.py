"""AgentCore Runtime entrypoint (CodeZip). The CLI project in cloud/agentcore/ stages this file as app.py.

Two request kinds (fieldwatch.contracts), selected by "action":
  {"action": "start", "investigation_id": "inv-...", "farm_id": "...", "field": "north pivot", "anomaly": {...}}
  {"action": "reply", "investigation_id": "inv-...", "interrupt_id": "...", "text": "YES", "approval_id": "apr-..."}

The entrypoint acknowledges at once and runs the investigation as a background task. While it runs, /ping
reports HealthyBusy (BedrockAgentCoreApp async-task tracking) so the Runtime keeps the session's microVM alive.
Progress goes out as InvestigationProgress / InvestigationConcluded on the `fieldwatch` bus and status lands in
fw-investigations, so the caller (the session starter Lambda) never waits on the result.

Set FIELDWATCH_RUNTIME_SYNC=1 to run inline and return the AgentStep instead (`agentcore dev`, local checks).
"""
import json
import os
import threading

import boto3  # noqa: F401  (kept importable here so a missing wheel fails at startup, not mid-investigation)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

# Deployed config comes from SSM so the committed CLI project holds no account-specific values.
# Runs before fieldwatch.config is imported: config reads the environment at import time.
_SSM_ENV = {
    "/fieldwatch/sessions-bucket": "FIELDWATCH_SESSIONS_BUCKET",
    "/fieldwatch/bucket": "FIELDWATCH_BUCKET",
    "/fieldwatch/gateway-url": "FIELDWATCH_GATEWAY_URL",
}


def bootstrap_env_from_ssm(client=None, params: dict[str, str] | None = None) -> dict[str, str]:
    """Copy SSM parameters into os.environ (never overriding values already set). Missing parameters are skipped."""
    loaded: dict[str, str] = {}
    client = client or boto3.client("ssm", region_name=os.getenv("AWS_REGION", "us-west-2"))
    for name, var in (params or _SSM_ENV).items():
        if os.environ.get(var):
            continue
        try:
            os.environ[var] = loaded[var] = client.get_parameter(Name=name)["Parameter"]["Value"]
        except Exception:  # ParameterNotFound, no permission: run with whatever is configured
            pass
    return loaded


if os.getenv("FIELDWATCH_SSM_BOOTSTRAP") == "1":
    bootstrap_env_from_ssm()

from fieldwatch import contracts as C  # noqa: E402
from fieldwatch import investigation  # noqa: E402

app = BedrockAgentCoreApp()
log = app.logger

_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}  # investigation_id -> its background thread


def parse_request(payload) -> C.StartInvestigation | C.ResumeInvestigation:
    """Validate a payload as StartInvestigation or ResumeInvestigation. Raises ValueError / ValidationError."""
    if isinstance(payload, dict) and set(payload) == {"prompt"} and isinstance(payload["prompt"], str):
        try:  # `agentcore invoke '<json>'` wraps its argument as {"prompt": "<json>"}
            payload = json.loads(payload["prompt"])
        except ValueError:
            raise ValueError("payload must be a JSON object") from None
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    action = payload.get("action", "start")
    if action == "start":
        return C.StartInvestigation.model_validate(payload)
    if action == "reply":
        return C.ResumeInvestigation.model_validate(payload)
    raise ValueError(f"unknown action {action!r}; expected 'start' or 'reply'")


def _error(code: str, message: str, retryable: bool = False) -> dict:
    return C.ErrorBody(error=C.ErrorDetail(code=code, message=message, retryable=retryable)).model_dump()


def _worker(req, task_id: int) -> None:
    try:
        # looked up on the module at call time so tests can swap in a fake agent
        investigation.run_request(req)
    except Exception:  # run_request already turns failures into AgentStep(failed); this is the last resort
        log.exception("investigation %s crashed", req.investigation_id)
    finally:
        with _lock:
            _running.pop(req.investigation_id, None)
        app.complete_async_task(task_id)


def submit(req) -> dict:
    """Start the investigation in the background and return the acknowledgement."""
    inv = req.investigation_id
    with _lock:
        if inv in _running:
            return {"accepted": False, "reason": "already_running", "investigation_id": inv, "action": req.action}
        task_id = app.add_async_task(f"investigation:{inv}")  # HealthyBusy until complete_async_task
        thread = threading.Thread(target=_worker, args=(req, task_id), name=f"inv-{inv}", daemon=True)
        _running[inv] = thread
    thread.start()
    return {"accepted": True, "investigation_id": inv, "action": req.action}


def wait_idle(timeout: float = 30.0) -> bool:
    """Block until background investigations finish (tests and local runs)."""
    with _lock:
        threads = list(_running.values())
    for t in threads:
        t.join(timeout)
    return not any(t.is_alive() for t in threads)


@app.entrypoint
def invoke(payload, context=None) -> dict:
    try:
        req = parse_request(payload)
    except (ValueError, ValidationError) as e:
        return _error("BAD_REQUEST", str(e)[:500])
    if os.getenv("FIELDWATCH_RUNTIME_SYNC") == "1":
        return investigation.run_request(req).model_dump(mode="json")
    return submit(req)


if __name__ == "__main__":
    app.run()
