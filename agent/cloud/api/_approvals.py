"""Adapter to the approvals lane (WS8, fieldwatch/approvals.py), which may not exist yet.

Expected interface (WS8):

    record_decision(approval_id: str, decision: "approved" | "declined", decided_by: str,
                    channel: "sms" | "web" | "voice") -> Approval | dict
        Conditional write on fw-approvals (status == "pending", unexpired, decided_by must be the
        person asked unless channel == "web" with a valid report token) and emits ApprovalRecorded.
        Raises an exception whose class name contains "AlreadyDecided" (with .approval or
        .decided_by / .decided_at attributes if possible), "Expired", or "NotFound".

The API maps those to ALREADY_DECIDED (409) and NOT_FOUND (404); without the module it answers 501.
"""
from __future__ import annotations

import importlib
from typing import Any

from ._http import ApiError


def module():
    try:
        return importlib.import_module("fieldwatch.approvals")
    except ModuleNotFoundError as e:
        if e.name in ("fieldwatch.approvals",):
            return None
        raise


def available() -> bool:
    mod = module()
    return bool(mod and hasattr(mod, "record_decision"))


def record_decision(approval_id: str, decision: str, decided_by: str, channel: str) -> Any:
    mod = module()
    if mod is None or not hasattr(mod, "record_decision"):
        raise ApiError("NOT_IMPLEMENTED", "Approvals are not available yet", 501)
    try:
        return mod.record_decision(approval_id=approval_id, decision=decision, decided_by=decided_by,
                                   channel=channel)
    except Exception as e:  # noqa: BLE001 - mapped by name so we don't depend on WS8's classes
        name = type(e).__name__
        if "AlreadyDecided" in name or "Conflict" in name:
            appr = getattr(e, "approval", None)
            who = getattr(e, "decided_by", None) or (appr.get("decided_by") if isinstance(appr, dict) else getattr(appr, "decided_by", None))
            when = getattr(e, "decided_at", None) or (appr.get("decided_at") if isinstance(appr, dict) else getattr(appr, "decided_at", None))
            msg = f"Already decided by {who}" + (f" at {when}" if when else "") if who else str(e) or "Already decided"
            raise ApiError("ALREADY_DECIDED", msg) from None
        if "Expired" in name:
            raise ApiError("ALREADY_DECIDED", "This request expired before a decision was made") from None
        if "NotFound" in name or isinstance(e, KeyError):
            raise ApiError("NOT_FOUND", "No such approval") from None
        raise
