"""One Lambda behind every AgentCore Gateway tool (Build Plan T19).

The Gateway invokes this function with just the tool arguments as the event; the tool name arrives in the
client context as `<target>___<tool>` (spike finding 22). We strip the prefix, look the tool up in the registry,
validate the arguments with its Pydantic input model, call its fn and return JSON (the Gateway turns that into
MCP text content, finding 24).

Errors are returned, never raised, as the specs' envelope:
    {"error": {"code": "INVALID_INPUT" | "NOT_FOUND" | "UPSTREAM_TIMEOUT" | ..., "message": str, "retryable": bool}}

Tool functions may raise any exception. Codes: pydantic.ValidationError -> INVALID_INPUT; LookupError,
FileNotFoundError or a "unknown ..." ValueError -> NOT_FOUND; TimeoutError / requests / httpx / botocore timeouts
or a tool taking longer than TOOL_TIMEOUT_S -> UPSTREAM_TIMEOUT (retryable). An exception with a string `code`
attribute from the specs' list (POLICY_DENIED, RATE_LIMITED, QUIET_HOURS, ...) keeps that code, so write tools
can `raise ToolError("POLICY_DENIED", "approval is not approved")` (below) or any class that carries `.code`.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from pydantic import ValidationError

from cloud.gateway.schemas import exposed_names
from fieldwatch.registry import BY_NAME
from fieldwatch.toolset import ToolResultError, call_spec, render, strip_target_prefix

log = logging.getLogger()
log.setLevel(logging.INFO)

# The specs ask read tools to answer within 3 s; real raster reads on a cold container need longer, so the
# default leaves headroom under the Lambda timeout. Override with FIELDWATCH_TOOL_TIMEOUT_S.
TOOL_TIMEOUT_S = float(os.getenv("FIELDWATCH_TOOL_TIMEOUT_S", "25"))
TOOL_NAME_KEY = "bedrockAgentCoreToolName"

KNOWN_CODES = {"INVALID_INPUT", "NOT_FOUND", "POLICY_DENIED", "RATE_LIMITED", "QUIET_HOURS", "UPSTREAM_TIMEOUT",
               "UPSTREAM_ERROR", "INTERNAL"}


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.retryable = code, retryable


def error(code: str, message: str, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def _tool_name(context: Any) -> str | None:
    custom = getattr(getattr(context, "client_context", None), "custom", None)
    if isinstance(custom, dict):
        raw = custom.get(TOOL_NAME_KEY)
        return strip_target_prefix(raw) if raw else None
    return None


def _timeouts() -> tuple[type[BaseException], ...]:
    kinds: list[type[BaseException]] = [TimeoutError, FutureTimeout]
    for mod, attr in (("requests.exceptions", "Timeout"), ("httpx", "TimeoutException"),
                      ("botocore.exceptions", "ReadTimeoutError"), ("botocore.exceptions", "ConnectTimeoutError")):
        try:
            kinds.append(getattr(__import__(mod, fromlist=[attr]), attr))
        except (ImportError, AttributeError):
            pass
    return tuple(kinds)


def _classify(exc: BaseException) -> dict:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in KNOWN_CODES:
        return error(code, str(exc), bool(getattr(exc, "retryable", False)))
    if isinstance(exc, ValidationError):
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors())
        return error("INVALID_INPUT", problems)
    if isinstance(exc, _timeouts()):
        return error("UPSTREAM_TIMEOUT", f"{type(exc).__name__}: {exc}" if str(exc) else "upstream timed out", True)
    if isinstance(exc, (LookupError, FileNotFoundError)) or (isinstance(exc, ValueError) and str(exc).startswith("unknown")):
        return error("NOT_FOUND", str(exc))
    if isinstance(exc, ToolResultError):
        return error("UPSTREAM_ERROR", str(exc), True)
    log.exception("tool failed")
    return error("INTERNAL", f"{type(exc).__name__}: {exc}")


def _run(spec, arguments: dict) -> Any:
    """Call the tool with a deadline. The worker thread is abandoned on timeout (the Lambda freezes it)."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return render(pool.submit(call_spec, spec, arguments).result(timeout=TOOL_TIMEOUT_S))
    finally:
        pool.shutdown(wait=False)


def handler(event: Any, context: Any) -> Any:
    name = _tool_name(context)
    if name is None:
        return error("INVALID_INPUT", f"no {TOOL_NAME_KEY} in the client context")
    spec = BY_NAME.get(name)
    if spec is None or name not in exposed_names():
        return error("NOT_FOUND", f"unknown tool {name!r}")
    arguments = event if isinstance(event, dict) else {}
    log.info("tool=%s investigation_id=%s", name, arguments.get("investigation_id"))
    try:
        result = _run(spec, arguments)
    except Exception as exc:  # noqa: BLE001 - every failure becomes an error envelope
        return _classify(exc)
    return result if isinstance(result, dict) else {"result": result}
