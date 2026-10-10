"""Adapter to the approvals lane (WS8, fieldwatch/approvals.py).

The real interface:

    record_decision(approval_id, decision, decided_by_person_id, channel, from_phone=None) -> Approval

It does the conditional write on fw-approvals (pending, unexpired, asked of that person; a text reply must
come from the number it was sent to) and emits ApprovalRecorded. It raises FieldwatchError with a `.code`;
this adapter maps those to the API's error body:

    NOT_FOUND        -> 404 NOT_FOUND
    POLICY_DENIED    -> 403 POLICY_DENIED
    ALREADY_DECIDED  -> 409 ALREADY_DECIDED  (message says who and when)
    EXPIRED          -> 409 ALREADY_DECIDED  (the web page only knows the spec's codes; the message says expired)
    INVALID_INPUT    -> 400 INVALID_INPUT

If fieldwatch.approvals can't be imported the API answers 501 NOT_IMPLEMENTED (and the SMS path passes the
reply on as ReplyReceived).
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

from ._http import ApiError

log = logging.getLogger("fieldwatch.api")


def module():
    """fieldwatch.approvals, or None when it can't be imported."""
    try:
        mod = importlib.import_module("fieldwatch.approvals")
    except ImportError:
        log.warning("fieldwatch.approvals is not importable", exc_info=True)
        return None
    return mod if hasattr(mod, "record_decision") else None


def available() -> bool:
    return module() is not None


def _api_error(e: Exception) -> ApiError:
    code = getattr(e, "code", "")
    message = getattr(e, "message", None) or str(e)
    retryable = bool(getattr(e, "retryable", False))
    if code == "ALREADY_DECIDED":
        message = message[:1].upper() + message[1:]  # "already approved by per_x at <time>"
        return ApiError("ALREADY_DECIDED", message, 409, retryable)
    if code == "EXPIRED":
        return ApiError("ALREADY_DECIDED", "This request expired before a decision was made", 409)
    if code == "NOT_FOUND":
        return ApiError("NOT_FOUND", "No such approval", 404)
    if code == "POLICY_DENIED":
        return ApiError("POLICY_DENIED", message, 403)
    if code == "INVALID_INPUT":
        return ApiError("INVALID_INPUT", message, 400)
    return ApiError("INTERNAL", message or "Could not record the decision", 500, retryable)


def record_decision(approval_id: str, decision: str, person_id: str, channel: str, from_phone: str | None = None) -> Any:
    """Record `decision` ("approved" | "declined") as `person_id` over `channel` ("sms" | "web" | "voice")."""
    mod = module()
    if mod is None:
        raise ApiError("NOT_IMPLEMENTED", "Approvals are not available yet", 501)
    try:
        return mod.record_decision(approval_id, decision, person_id, channel, from_phone=from_phone)
    except Exception as e:  # noqa: BLE001 - FieldwatchError carries .code; anything else is a bug
        if hasattr(e, "code") and hasattr(e, "body"):
            raise _api_error(e) from None
        raise
