"""LoadFieldInputs (state machine step 1): fw-fields row + open fw-watches -> FieldAnalysisRequest.

Input (one Map item): {field: FieldRef, scene: SceneInfo, replay, replay_id, as_of}.
Output: a FieldAnalysisRequest (contracts.py) as JSON. "As of" rule: only watches open at the scene's date count, and
as_of is the scene's date, so a replayed July scene sees July's world (docs/implementation-specs.md "Replay correctness").
"""
from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from fieldwatch import contracts as C

from . import common as cm


class FieldNotFound(Exception):
    pass


def thresholds() -> dict[str, float]:
    return {
        "min_clear_fraction": float(os.getenv("MIN_CLEAR_FRACTION", "0.7")),
        "min_region_acres": float(os.getenv("MIN_REGION_ACRES", "1.2")),
        "z_threshold": float(os.getenv("Z_THRESHOLD", "2.0")),
    }


def watch_ids_open_at(farm_field_id: str, scene_dt) -> list[str]:
    """Watches for the field that were open on the scene date (not closed, opened by the end of that day, not yet expired)."""
    rows = cm.query_all(cm.table("watches"), KeyConditionExpression="field_id = :f",
                        ExpressionAttributeValues={":f": farm_field_id})
    ids: list[str] = []
    for w in map(cm.to_native, rows):
        if str(w.get("status", "open")).lower() in cm.TERMINAL_WATCH_STATUSES:
            continue
        sort_key = str(w.get("opened_at_watch_id", ""))
        opened = cm.parse_time(w.get("opened_at")) or cm.parse_time(sort_key.split("#", 1)[0])
        expires = cm.parse_time(w.get("expires_at"))
        if opened and opened > scene_dt + timedelta(days=1):
            continue
        if expires and expires < scene_dt:
            continue
        watch_id = w.get("watch_id") or (sort_key.split("#", 1)[1] if "#" in sort_key else None)
        if watch_id:
            ids.append(str(watch_id))
    return sorted(set(ids))


def load_request(event: dict) -> dict:
    ref = C.FieldRef(**event["field"])
    scene = C.SceneInfo(**event["scene"])
    scene_dt = cm.parse_time(scene.datetime)
    if scene_dt is None:
        raise ValueError(f"bad scene datetime {scene.datetime!r}")

    row = cm.table("fields").get_item(Key={"farm_id": ref.farm_id, "field_id": ref.field_id}).get("Item")
    if not row:
        raise FieldNotFound(f"no field {ref.farm_id}/{ref.field_id} in fw-fields")
    row = cm.to_native(row)

    pivot = row.get("pivot") or None
    if pivot:
        center = pivot.get("center") or []
        pivot = {"center": [float(center[0]), float(center[1])], "radius_m": float(pivot.get("radius_m", 0))} \
            if len(center) == 2 else None

    request = C.FieldAnalysisRequest(
        field=ref,
        polygon=[tuple(p) for p in cm.field_polygon(row)],
        crop=cm.normalize_crop(row.get("crop_current") or row.get("crop")),
        as_of=scene_dt.date().isoformat(),
        scene=scene,
        pivot=pivot,
        open_watch_ids=watch_ids_open_at(ref.field_id, scene_dt),
        **thresholds(),
    )
    return request.model_dump(mode="json")


def handler(event: dict, context: Any = None) -> dict:
    return load_request(event)
