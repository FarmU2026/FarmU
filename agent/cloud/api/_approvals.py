"""Adapter to the approvals lane (WS8) through `fieldwatch.interfaces.ApprovalsAPI`.

    interfaces.approvals().record_decision(approval_id, decision, decided_by_person_id, channel, from_phone=None)
        -> contracts.Approval

It does the conditional write on fw-approvals (pending, unexpired, asked of that person; a text reply must
come from the number it was sent to) and emits ApprovalRecorded. It raises interfaces.FieldwatchError; this
adapter maps the codes to the API's error body:

    NOT_FOUND        -> 404 NOT_FOUND
    POLICY_DENIED    -> 403 POLICY_DENIED
    ALREADY_DECIDED  -> 409 ALREADY_DECIDED  (message says who and when)
    EXPIRED          -> 409 ALREADY_DECIDED  (the web page only knows the spec's codes; the message says expired)
    INVALID_INPUT    -> 400 INVALID_INPUT

If the approvals module can't be imported the API answers 501 NOT_IMPLEMENTED (and the SMS path passes the
reply on as ReplyReceived).
"""
from __future__ import annotations

import logging

from fieldwatch import contracts as C
from fieldwatch import interfaces
from fieldwatch.interfaces import ApprovalsAPI, FieldwatchError

from ._http import ApiError

log = logging.getLogger("fieldwatch.api")


def module() -> ApprovalsAPI | None:
    """The approvals lane, or None when it can't be imported."""
    try:
        return interfaces.approvals()
    except ImportError:
        log.warning("fieldwatch.approvals is not importable", exc_info=True)
        return None


def available() -> bool:
    return module() is not None


def _api_error(e: FieldwatchError) -> ApiError:
    message = e.message
    if e.code == "ALREADY_DECIDED":
        message = message[:1].upper() + message[1:]  # "already approved by per_x at <time>"
        return ApiError("ALREADY_DECIDED", message, 409, e.retryable)
    if e.code == "EXPIRED":
        return ApiError("ALREADY_DECIDED", "This request expired before a decision was made", 409)
    if e.code == "NOT_FOUND":
        return ApiError("NOT_FOUND", "No such approval", 404)
    if e.code in ("POLICY_DENIED", "INVALID_INPUT"):
        return ApiError(e.code, message, e.http_status)
    return ApiError("INTERNAL", message or "Could not record the decision", 500, e.retryable)


def record_decision(approval_id: str, decision: str, person_id: str, channel: str,
                    from_phone: str | None = None) -> C.Approval:
    """Record `decision` ("approved" | "declined") as `person_id` over `channel` ("sms" | "web" | "voice")."""
    mod = module()
    if mod is None:
        raise ApiError("NOT_IMPLEMENTED", "Approvals are not available yet", 501)
    try:
        return mod.record_decision(approval_id, decision, person_id, channel, from_phone=from_phone)
    except FieldwatchError as e:
        raise _api_error(e) from None
