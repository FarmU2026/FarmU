"""Scheduled "check latest" (EventBridge Scheduler, daily): the cheap alternative to the live Earth Search SNS topic.

Finds the distinct tiles in fw-tile-index, searches Earth Search for recent scenes over each (grid:code query, cloud limit,
last LOOKBACK_DAYS days), and sends each STAC item to fw-scene-queue as a plain (non-replay) message. The scene filter
dedupes with fw-idempotency, so re-sending yesterday's scene is harmless.

Env: SCENE_QUEUE_URL; CHECK_COLLECTIONS (default sentinel-2-c1-l2a), CHECK_MAX_CLOUD (40), CHECK_LOOKBACK_DAYS (4).
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from typing import Any

from . import common as cm
from .common import log
from .replay import stac_search


def registered_tiles() -> list[str]:
    rows = cm.scan_all(cm.table("tile-index"), ProjectionExpression="tile_id")
    return sorted({r["tile_id"] for r in rows if r.get("tile_id")})


def grid_code(tile_id: str) -> str:
    base = tile_id.strip()
    if base.upper().startswith("MGRS-"):
        base = base[5:]
    if base.upper().startswith("T") and base[1:2].isdigit():
        base = base[1:]
    return f"MGRS-{base.upper()}"


def check_latest() -> dict:
    collections = [c.strip() for c in os.getenv("CHECK_COLLECTIONS", cm.PREFERRED_SOURCE).split(",") if c.strip()]
    max_cloud = float(os.getenv("CHECK_MAX_CLOUD", "40"))
    start = cm.now() - timedelta(days=int(os.getenv("CHECK_LOOKBACK_DAYS", "4")))
    queue_url = os.environ["SCENE_QUEUE_URL"]
    tiles = registered_tiles()
    sent = 0
    for tile in tiles:
        items = stac_search({
            "collections": collections,
            "query": {"grid:code": {"eq": grid_code(tile)}, "eo:cloud_cover": {"lt": max_cloud}},
            "datetime": f"{cm.iso(start)}/{cm.iso(cm.now())}",
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
            "limit": 10,
        })
        for i in range(0, len(items), 10):
            chunk = items[i:i + 10]
            cm.client("sqs").send_message_batch(QueueUrl=queue_url, Entries=[
                {"Id": str(n), "MessageBody": json.dumps(item)} for n, item in enumerate(chunk)])
            sent += len(chunk)
    log.info("check latest: %d tiles, %d scenes queued", len(tiles), sent)
    return {"tiles": tiles, "scenes_queued": sent}


def handler(event: dict | None = None, context: Any = None) -> dict:
    return check_latest()
