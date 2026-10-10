"""Last step per field: decide ready / partial / failed, stamp fw-field-context, flip fw-fields.status.

Input: {farm_id, field_id, stages: [{stage, ok, stub?, error?}]}; the LoadBoundary failure path passes
stages=[{stage: "load_boundary", ok: false}].
Output (one entry of the state machine's `results`): {field_id, status, failed_stages, stub_stages}.

  ready    every stage ok
  partial  some stages failed; fw-fields.status is still `ready`, so the field is usable with gaps
  failed   LoadBoundary failed or every stage failed; fw-fields.status = `failed` (the specs list only
           building and ready, so the web API/UI should treat `failed` as "try onboarding again")
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .common import FIELD_CONTEXT_TABLE, FIELDS_TABLE, SCHEMA_VERSION, table, to_ddb

log = logging.getLogger(__name__)


def decide(stages: list[dict]) -> tuple[str, list[str]]:
    failed = [s["stage"] for s in stages if not s.get("ok")]
    if not stages or "load_boundary" in failed or len(failed) == len(stages):
        return "failed", failed
    return ("partial" if failed else "ready"), failed


def handler(event, _context=None):
    farm_id, field_id = event["farm_id"], event["field_id"]
    stages = event.get("stages") or []
    status, failed = decide(stages)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if status != "failed":
        table(FIELD_CONTEXT_TABLE).update_item(
            Key={"field_id": field_id},
            UpdateExpression="SET schema_version = :v, built_at = :t",
            ExpressionAttributeValues={":v": SCHEMA_VERSION, ":t": now},
        )
    table(FIELDS_TABLE).update_item(
        Key={"farm_id": farm_id, "field_id": field_id},
        UpdateExpression="SET #s = :s, context_status = :cs, context_failed_stages = :fs, context_built_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues=to_ddb({":s": "failed" if status == "failed" else "ready", ":cs": status,
                                          ":fs": failed, ":t": now}),
        ConditionExpression="attribute_exists(field_id)",
    )
    log.info("finalized %s/%s: %s (failed: %s)", farm_id, field_id, status, failed)
    return {"field_id": field_id, "status": status, "failed_stages": failed,
            "stub_stages": [s["stage"] for s in stages if s.get("ok") and s.get("stub")]}
