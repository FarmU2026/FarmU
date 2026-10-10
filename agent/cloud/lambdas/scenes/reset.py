"""Reset Lambda (`POST /v1/replay/reset`): clears what replays created for the demo farm so the next replay starts clean.

Request: {farm_id?}  (default env DEMO_FARM_ID, "wohleb-farm").
Deletes, for the farm's fields:
  - fw-idempotency keys written by replays (replay = true): replay scene keys, analyzed#..., anomaly#...
  - fw-scene-analyses rows with replay = true
  - fw-investigations whose anomaly_id was issued by a replay (or that carry replay = true), found via the by-field index
  - fw-approvals and fw-actions that belong to those investigations
It never touches fields, watches, messages or live (non-replay) rows.
Response: {farm_id, deleted: {table: count}}.
"""
from __future__ import annotations

import os
from typing import Any

from . import common as cm
from .common import log


def _delete(table_short: str, keys: list[dict]) -> int:
    tbl = cm.table(table_short)
    with tbl.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
    return len(keys)


def reset(farm_id: str) -> dict:
    fields = [r["field_id"] for r in cm.query_all(
        cm.table("fields"), KeyConditionExpression="farm_id = :f", ExpressionAttributeValues={":f": farm_id})]
    deleted: dict[str, int] = {}

    # 1. idempotency keys written by replays; remember their anomaly ids first.
    idem = cm.scan_all(cm.table("idempotency"), FilterExpression="replay = :t",
                       ExpressionAttributeValues={":t": True})
    mine = [r for r in idem if r.get("farm_id", farm_id) == farm_id]  # replay scene keys carry no farm
    anomaly_ids = {r["anomaly_id"] for r in mine if r.get("anomaly_id")}

    # 2. scene analyses written by replays
    rows = []
    for fid in fields:
        for r in cm.query_all(cm.table("scene-analyses"), KeyConditionExpression="field_id = :f",
                              ExpressionAttributeValues={":f": fid}):
            if r.get("replay") is True:
                rows.append({"field_id": r["field_id"], "date_source": r["date_source"]})
    deleted["fw-scene-analyses"] = _delete("scene-analyses", rows)

    # 3. investigations from replayed anomalies
    inv_ids: set[str] = set()
    for fid in fields:
        for r in cm.query_all(cm.table("investigations"), IndexName="by-field", KeyConditionExpression="field_id = :f",
                              ExpressionAttributeValues={":f": fid}):
            if r.get("replay") is True or r.get("anomaly_id") in anomaly_ids:
                inv_ids.add(r["investigation_id"])
    deleted["fw-investigations"] = _delete("investigations", [{"investigation_id": i} for i in sorted(inv_ids)])

    # 4. approvals and actions of those investigations (no index on investigation_id: scan, demo-sized tables)
    for short, pk in (("approvals", "approval_id"), ("actions", "action_id")):
        hits = [{pk: r[pk]} for r in cm.scan_all(cm.table(short)) if r.get("investigation_id") in inv_ids]
        deleted[f"fw-{short}"] = _delete(short, hits)

    # 5. idempotency last, so a failure above can be retried with the anomaly ids still known
    deleted["fw-idempotency"] = _delete("idempotency", [{"key": r["key"]} for r in mine])
    log.info("reset farm %s: %s", farm_id, deleted)
    return {"farm_id": farm_id, "deleted": deleted}


def handler(event: dict, context: Any = None) -> dict:
    farm_id = (event or {}).get("farm_id") or os.getenv("DEMO_FARM_ID", "wohleb-farm")
    return reset(farm_id)
