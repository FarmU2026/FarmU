"""Replay Lambda (docs/implementation-specs.md "Replay API"; data-pipelines B5). Invoked by the API lane (`POST /v1/replay`).

Request:  {scene_id?, field_ids?, as_of?, farm_id?, delay_s?, skip_hail?, replay_id?}
          either scene_id (optionally limited to field_ids) or field_ids + as_of (latest clear scene on or before as_of).
Response: {replay_id, scenes: [{scene_id, source, datetime, tile_id, field_ids}], hail_events, delay_s}
          or an ErrorBody {error: {code, message, retryable}}.

What it does: (1) publishes MrmsFileAvailable (source `fieldwatch.replay`) for the archived 24-hour hail-size files covering
the 14 days before the scene, so the hazards lane opens the watches as they would have been; (2) sends a replay envelope
{replay: true, replay_id, stac_item, field_ids?, as_of?} for each scene into fw-scene-queue, delayed a little so the watches
exist when the scene is analyzed. Replaying twice without a reset is a no-op downstream (idempotent anomaly ids).
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from fieldwatch import contracts as C

from . import common as cm
from .common import log


class ReplayError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


class MrmsFileAvailable(C.EventBase):
    """`MrmsFileAvailable` (docs event catalog). Not in contracts.py yet: needs a contract PR."""

    bucket: str
    key: str
    product: str
    valid_time: C.ISOTime


# ---------- Earth Search ----------


def stac_search(body: dict) -> list[dict]:
    resp = requests.post(f"{cm.STAC_URL}/search", json=body, timeout=25)
    if resp.status_code >= 500:
        raise ReplayError("STAC_UNAVAILABLE", f"Earth Search returned {resp.status_code}", retryable=True)
    resp.raise_for_status()
    return resp.json().get("features", [])


def fetch_item(scene_id: str) -> dict:
    items = stac_search({"ids": [scene_id], "limit": 1})
    if not items:
        raise ReplayError("SCENE_NOT_FOUND", f"Earth Search has no scene {scene_id}")
    return items[0]


# ---------- fields ----------


def field_item(field_id: str) -> dict:
    rows = cm.query_all(cm.table("fields"), IndexName="by-field", KeyConditionExpression="field_id = :f",
                        ExpressionAttributeValues={":f": field_id})
    if not rows:
        raise ReplayError("FIELD_NOT_FOUND", f"no field {field_id}")
    return rows[0]


def in_tile_index(tile_code: str, field_id: str) -> bool:
    tbl = cm.table("tile-index")
    return any(tbl.get_item(Key={"tile_id": k, "field_id": field_id}).get("Item") for k in cm.tile_key_candidates(tile_code))


def _bbox(ring: list[list[float]]) -> tuple[float, float, float, float]:
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    return min(lons), min(lats), max(lons), max(lats)


def _contains(outer: list[float] | None, inner: tuple[float, float, float, float]) -> bool:
    if not outer or len(outer) < 4:
        return True  # no footprint on the item: let the tile-index check decide
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def clear_scene_for_field(field_id: str, as_of: str) -> dict:
    """Latest clear scene on or before as_of whose tile the field is registered under (prefers c1 over legacy l2a)."""
    row = field_item(field_id)
    ring = cm.field_polygon(row)
    box = _bbox(ring)
    day = datetime.fromisoformat(as_of[:10]).replace(tzinfo=timezone.utc)
    window = int(os.getenv("REPLAY_LOOKBACK_DAYS", "30"))
    found = stac_search({
        "collections": list(cm.ACCEPTED_SOURCES),
        "bbox": list(box),
        "datetime": f"{(day - timedelta(days=window)).strftime('%Y-%m-%d')}T00:00:00Z/{day.strftime('%Y-%m-%d')}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": float(os.getenv("REPLAY_MAX_CLOUD", "20"))}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "limit": 20,
    })
    found.sort(key=lambda i: (i["properties"]["datetime"], i.get("collection") == cm.PREFERRED_SOURCE), reverse=True)
    for item in found:
        tile = item["properties"].get("grid:code")
        if tile and _contains(item.get("bbox"), box) and in_tile_index(tile, field_id):
            return item
    raise ReplayError("NO_CLEAR_SCENE", f"no clear scene over field {field_id} on or before {as_of}")


# ---------- hail backfill ----------


def hail_file_times(scene_dt: datetime, days: int = cm.HAIL_BACKFILL_DAYS) -> list[datetime]:
    """Valid times of the 24-hour max-hail files, 24 h apart, the last at the half hour just before the scene.
    Each file covers the 24 hours before its valid time, so together they cover the 14 days before the scene."""
    end = scene_dt.astimezone(timezone.utc).replace(minute=(scene_dt.minute // 30) * 30, second=0, microsecond=0)
    return [end - timedelta(days=k) for k in range(days - 1, -1, -1)]


def hail_key(valid: datetime) -> str:
    day, stamp = valid.strftime("%Y%m%d"), valid.strftime("%Y%m%d-%H%M%S")
    return f"CONUS/{cm.MRMS_DAILY_PRODUCT}/{day}/MRMS_{cm.MRMS_DAILY_PRODUCT}_{stamp}.grib2.gz"


def publish_hail_backfill(scene_dt: datetime) -> int:
    entries = []
    for valid in hail_file_times(scene_dt):
        event = MrmsFileAvailable(occurred_at=cm.iso(cm.now()), bucket=cm.MRMS_BUCKET, key=hail_key(valid),
                                  product=cm.MRMS_DAILY_PRODUCT, valid_time=cm.iso(valid))
        entries.append({"source": cm.REPLAY_EVENT_SOURCE, "detail_type": "MrmsFileAvailable",
                        "detail": event.model_dump(mode="json")})
    cm.put_events(entries)
    return len(entries)


# ---------- handler ----------


def replay(req: dict) -> dict:
    scene_id, field_ids, as_of = req.get("scene_id"), req.get("field_ids") or None, req.get("as_of")
    if not scene_id and not (field_ids and as_of):
        raise ReplayError("BAD_REQUEST", "send scene_id, or field_ids with as_of")
    replay_id = req.get("replay_id") or f"rpl_{uuid.uuid4().hex[:12]}"

    # (stac item, fields to analyze or None for every registered field)
    plan: dict[str, tuple[dict, list[str] | None]] = {}
    if scene_id:
        plan[scene_id] = (fetch_item(scene_id), field_ids)
    else:
        for fid in field_ids:
            item = clear_scene_for_field(fid, as_of)
            _, fids = plan.setdefault(item["id"], (item, []))
            fids.append(fid)
    if not plan:
        raise ReplayError("BAD_REQUEST", "nothing to replay")

    scenes = []
    for item, fids in plan.values():
        info = cm.scene_from_item(item)  # validates grid:code etc.
        scenes.append((item, fids, info))

    hail = 0
    if not req.get("skip_hail"):
        for dt in sorted({cm.parse_time(info["datetime"]) for _, _, info in scenes}):
            hail += publish_hail_backfill(dt)
    delay = int(req.get("delay_s", os.getenv("REPLAY_SCENE_DELAY_S", "30"))) if hail else 0
    delay = max(0, min(delay, 900))

    queue_url = os.environ["SCENE_QUEUE_URL"]
    for item, fids, info in scenes:
        envelope = {"replay": True, "replay_id": replay_id, "stac_item": item, "field_ids": fids,
                    "as_of": as_of or info["datetime"][:10]}
        cm.client("sqs").send_message(QueueUrl=queue_url, MessageBody=json.dumps(envelope), DelaySeconds=delay)

    return {
        "replay_id": replay_id,
        "scenes": [{"scene_id": i["scene_id"], "source": i["source"], "datetime": i["datetime"],
                    "tile_id": i["tile_id"], "field_ids": f or []} for _, f, i in scenes],
        "hail_events": hail,
        "delay_s": delay,
    }


def handler(event: dict, context: Any = None) -> dict:
    if isinstance(event.get("body"), str):  # tolerate an API Gateway proxy event
        event = json.loads(event["body"])
    try:
        return replay(event)
    except ReplayError as exc:
        log.warning("replay refused: %s %s", exc.code, exc)
        return {"error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}}
