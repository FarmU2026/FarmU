"""Recommendations for GET /api/farms/{farm_id}: the farm payload's `diagnoses` and `pending`.

The local harness (web/server.py) puts every `Diagnosis.model_dump()` in `farm["diagnoses"]`, oldest first, and the
front end reads it as "the last entry is the newest". The cloud keeps the same list, built from fw-investigations
(the Recorder stores `diagnosis_json` when a run concludes) and decorated with what the UI needs after a reload:

    {<every Diagnosis field>,   field_name, what, where, why, ruled_out, dollars_low, dollars_high, how_sure,
                                recommended_action, action_detail, condition, affected_acres, hypotheses
     "investigation_id", "field_id",
     "decision":   "pending" | "approved" | "declined" | "expired" | "none" | None
                   (fw-approvals; "none": nothing to approve; None: concluded, no approval record)
     "approval_id", "action_type"   the approval's id and type (work_order, scout_pin, ...), or None
     "report_url"         signed link to the report page (scope report, subject = investigation id)
     "work_order_url"     signed link to the work-order page (scope work_order, subject = action id), once the farmer
                          approved a work order and the action exists
     "work_order_status"  "open" | "done" | None;  "completed_at"  fw-actions done_at | None
     "created_at", "updated_at"}

A run still waiting for YES/NO has no diagnosis yet, so it is a stub: the Diagnosis fields at their defaults, `what`
= the question we texted, `decision` "pending", and the farm's top-level `pending` = {investigation_id, interrupt_id}
(the harness shape). Question runs (kind "question") are never listed. At most LIMIT entries, one query per field.

Links are signed with fieldwatch.links (the key the pages verify with), valid for LINK_TTL_HOURS, and minted at the
top of the hour so the same link comes back on every poll. The base is FIELDWATCH_PUBLIC_URL (the front end), else "":
a site-relative link, which works because the page that reads this payload is served from the same site.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from boto3.dynamodb.conditions import Key

from fieldwatch import interfaces, links
from fieldwatch.interfaces import Env
from fieldwatch.schemas import Diagnosis

from . import _aws

log = logging.getLogger("fieldwatch.api")

LIMIT = 5  # newest entries returned per farm
PER_FIELD = 10  # rows read per field (newest first; question runs are skipped after the read)
LINK_TTL_HOURS = 24
PENDING_MAX_DAYS = 7  # a run "waiting for YES" with no approval record is only believed this long
RECOMMENDED = {"work_order": "repair_work_order", "scout_pin": "scouting_pin", "photo_request": "request_photo",
               "insurance_notice": "insurance_notice"}


def recent_investigations(field_id: str, limit: int = 10) -> list[dict]:
    """Newest first (GSI by-field on field_id + created_at)."""
    page = _aws.table("investigations").query(IndexName="by-field", KeyConditionExpression=Key("field_id").eq(field_id),
                                              ScanIndexForward=False, Limit=limit)
    return [_aws.from_ddb(i) for i in page.get("Items", [])]


def _approval_status(a: dict) -> str:
    st = a.get("status", "pending")
    if st == "pending" and str(a.get("expires_at", "9999")) < _aws.now_iso():
        return "expired"
    return st


def _approvals_by_investigation(person_ids: list[str], invs: list[dict]) -> dict[str, dict]:
    """The newest approval row of each investigation. fw-approvals has no index on investigation_id, so read the rows
    of the people the farm texts (GSI by-person-status) and keep the ones that match. An investigation that names
    its approval (`approval_id`) is read directly."""
    want = {i["investigation_id"] for i in invs}
    found: dict[str, dict] = {}
    for inv in invs:
        if inv.get("approval_id"):
            a = _aws.get_item("approvals", approval_id=inv["approval_id"])
            if a:
                found[inv["investigation_id"]] = a
    if want <= found.keys():
        return found
    named = set(found)
    for pid in person_ids:
        rows = _aws.query_all(_aws.table("approvals"), IndexName="by-person-status",
                              KeyConditionExpression=Key("sent_to_person_id").eq(pid))
        for a in rows:
            iid = a.get("investigation_id")
            if iid not in want or iid in named:
                continue
            if iid not in found or str(a.get("created_at", "")) > str(found[iid].get("created_at", "")):
                found[iid] = a
    return found


def _signed(farm_id: str, scope: str, subject_id: str, now: float) -> str | None:
    try:
        token = links.sign(farm_id, scope, subject_id, ttl_hours=LINK_TTL_HOURS, now=now)
        return links.url(os.getenv(Env.PUBLIC_URL, "").strip(), scope, subject_id, token)
    except Exception:  # no signing key (yet): the payload still loads, just without links
        log.warning("could not sign a %s link", scope, exc_info=True)
        return None


def _stored_diagnosis(inv: dict) -> dict | None:
    try:
        d = json.loads(inv.get("diagnosis_json") or "null")
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _empty(**given: Any) -> dict:
    return Diagnosis(**{"field_name": "", "what": "", "where": "", "why": "", "dollars_low": 0, "dollars_high": 0,
                        "how_sure": "", "recommended_action": "none", "action_detail": "", **given}).model_dump(mode="json")


def _entry(inv: dict, field: dict, diag: dict | None, appr: dict | None, farm_id: str, now: float) -> dict:
    """One `diagnoses` item. `diag` None: the run is waiting for the farmer, so only a stub of the Diagnosis."""
    name = field.get("name") or inv["field_id"]
    if diag is not None:
        try:
            body = Diagnosis.model_validate(diag).model_dump(mode="json")
        except ValueError:  # an older or hand-edited row: keep what is there on top of the defaults
            body = {**_empty(), **diag}
        body["field_name"] = body.get("field_name") or name
    else:
        stub = dict(field_name=name, what=inv.get("last_text") or "",
                    recommended_action=RECOMMENDED.get((appr or {}).get("action_type"), "none"),
                    action_detail=(appr or {}).get("summary") or "")
        try:
            body = _empty(**stub, condition=inv.get("condition") or None)
        except ValueError:  # a condition id the catalog does not know
            body = _empty(**stub)

    if appr:
        decision = _approval_status(appr)
    elif diag is None:
        decision = "pending"
    else:
        decision = "none" if body["recommended_action"] == "none" else None

    out = {
        **body,
        "investigation_id": inv["investigation_id"], "field_id": inv["field_id"], "decision": decision,
        "approval_id": (appr or {}).get("approval_id"), "action_type": (appr or {}).get("action_type"),
        "report_url": _signed(farm_id, "report", inv["investigation_id"], now),
        "work_order_url": None, "work_order_status": None, "completed_at": None,
        "created_at": inv.get("created_at"), "updated_at": inv.get("updated_at"),
    }
    if appr and appr.get("action_type") == "work_order" and decision == "approved":
        action_id = interfaces.actions().action_id_for(appr["approval_id"])
        act = _aws.get_item("actions", action_id=action_id)
        if act:
            done = act.get("status") == "done"
            out["work_order_url"] = _signed(farm_id, "work_order", action_id, now)
            out["work_order_status"] = "done" if done else "open"
            out["completed_at"] = act.get("done_at") if done else None
    return out


def build(farm_id: str, fields: list[dict], person_ids: list[str]) -> tuple[list[dict], dict | None]:
    """(`diagnoses`, oldest first; the farm's `pending` or None). Never raises: a failure here must not take the
    whole farm payload down, so it is logged and the farm loads without recommendations."""
    try:
        return _build(farm_id, fields, person_ids)
    except Exception:
        log.exception("could not build the diagnoses for %s", farm_id)
        return [], None


def _build(farm_id: str, fields: list[dict], person_ids: list[str]) -> tuple[list[dict], dict | None]:
    by_id = {f["field_id"]: f for f in fields}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PENDING_MAX_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows: list[tuple[dict, dict | None]] = []  # (investigation, stored diagnosis, or None while it waits for the farmer)
    for fid in by_id:
        for inv in recent_investigations(fid, PER_FIELD):
            if inv.get("kind") == "question" or inv.get("farm_id", farm_id) != farm_id:
                continue
            inv["field_id"] = fid
            diag = _stored_diagnosis(inv)
            if inv.get("status") == "concluded" and diag is not None:
                rows.append((inv, diag))
            elif inv.get("status") == "awaiting_reply" and inv.get("pending_interrupt_id"):
                rows.append((inv, None))
    rows.sort(key=lambda r: str(r[0].get("created_at", "")), reverse=True)
    rows = rows[:LIMIT]
    if not rows:
        return [], None

    approvals = _approvals_by_investigation(person_ids, [r[0] for r in rows])
    now = float(int(time.time()) // 3600 * 3600)
    entries: list[dict] = []
    pending: dict[str, Any] | None = None
    for inv, diag in rows:  # newest first
        appr = approvals.get(inv["investigation_id"])
        if diag is None and not appr and str(inv.get("created_at", "")) < cutoff:
            continue  # waiting for a YES nobody can give any more
        e = _entry(inv, by_id[inv["field_id"]], diag, appr, farm_id, now)
        entries.append(e)
        if diag is None and e["decision"] == "pending" and pending is None:
            pending = {"investigation_id": inv["investigation_id"], "interrupt_id": inv["pending_interrupt_id"]}
    entries.reverse()  # oldest first, like the harness: the last one is the newest
    return entries, pending
