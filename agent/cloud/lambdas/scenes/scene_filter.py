"""Scene filter Lambda (docs/implementation-specs.md "Scene filter"; plan T14).

Trigger: SQS `fw-scene-queue` (batch 10, partial batch failures). A message body is one of
  - a full STAC item (Earth Search new-item message, optionally wrapped in an SNS notification envelope), or
  - a replay envelope {replay: true, replay_id, stac_item, field_ids?, as_of?} from the replay Lambda.

Rules, in order: skip collections other than sentinel-2-l2a / sentinel-2-c1-l2a; look the fields up in fw-tile-index by the
item's grid:code; skip when no field matches or (live scenes only) the scene is more than 80% cloudy; claim
`scene#<scene_id>` in fw-idempotency with a conditional put (replays claim `scene#<scene_id>#replay#<replay_id>`);
start one Step Functions Express execution per scene with the SceneAnalysisInput.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from . import common as cm
from .common import log


def parse_body(body: str | dict) -> dict:
    """Message body -> normalized envelope {replay, replay_id, stac_item, field_ids, as_of}. Raises ValueError."""
    data = json.loads(body) if isinstance(body, str) else body
    if isinstance(data, dict) and data.get("Type") == "Notification" and "Message" in data:  # SNS -> SQS envelope
        data = json.loads(data["Message"])
    if not isinstance(data, dict):
        raise ValueError("message is not a JSON object")
    if data.get("replay") and isinstance(data.get("stac_item"), dict):
        return {"replay": True, "replay_id": data.get("replay_id"), "stac_item": data["stac_item"],
                "field_ids": data.get("field_ids"), "as_of": data.get("as_of")}
    if data.get("type") == "Feature" or "assets" in data:
        return {"replay": False, "replay_id": None, "stac_item": data, "field_ids": None, "as_of": None}
    raise ValueError("message is neither a STAC item nor a replay envelope")


def fields_for_tile(tile_id: str) -> list[dict]:
    """FieldRefs registered for a tile in fw-tile-index (tolerates MGRS-14TPM / 14TPM / T14TPM keys)."""
    tbl = cm.table("tile-index")
    seen: dict[tuple[str, str], dict] = {}
    for key in cm.tile_key_candidates(tile_id):
        for row in cm.query_all(tbl, KeyConditionExpression="tile_id = :t", ExpressionAttributeValues={":t": key}):
            if row.get("farm_id") and row.get("field_id"):
                seen.setdefault((row["farm_id"], row["field_id"]),
                                {"farm_id": row["farm_id"], "field_id": row["field_id"]})
    return list(seen.values())


def _safe_name(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", "-".join(parts))[:80]


def start_execution(scene: dict, fields: list[dict], replay: bool, replay_id: str | None, as_of: str | None) -> str:
    payload = {"schema_version": "1", "scene": scene, "fields": fields, "replay": replay,
               "replay_id": replay_id, "as_of": as_of}
    resp = cm.client("stepfunctions").start_execution(
        stateMachineArn=os.environ["STATE_MACHINE_ARN"],
        name=_safe_name(scene["scene_id"][:50], cm.now().strftime("%H%M%S%f")),
        input=json.dumps(payload),
    )
    return resp.get("executionArn", "")


def process_message(body: str | dict) -> str:
    """Handle one message; returns the outcome ('started' or 'skipped:<why>'). Raises on transient errors."""
    try:
        env = parse_body(body)
        item = env["stac_item"]
    except (ValueError, KeyError) as exc:
        log.warning("dropping unreadable message: %s", exc)
        return "skipped:invalid"

    collection = item.get("collection")
    if collection not in cm.ACCEPTED_SOURCES:
        return "skipped:collection"
    try:
        scene = cm.scene_from_item(item)
    except ValueError as exc:
        log.warning("dropping item %s: %s", item.get("id"), exc)
        return "skipped:invalid"

    fields = fields_for_tile(scene["tile_id"])
    if env["field_ids"]:
        wanted = set(env["field_ids"])
        fields = [f for f in fields if f["field_id"] in wanted]
    if not fields:
        return "skipped:no_fields"
    if not env["replay"] and scene["cloud_cover"] > cm.MAX_SCENE_CLOUD:
        return "skipped:cloud"  # an explicit replay is the operator's choice; the analyzer still reports too_cloudy per field

    key = f"scene#{scene['scene_id']}"
    if env["replay"]:
        key += f"#replay#{env['replay_id'] or 'noid'}"
    if not cm.claim(key, scene_id=scene["scene_id"], replay=bool(env["replay"]), fields=len(fields)):
        return "skipped:duplicate"
    try:
        arn = start_execution(scene, fields, bool(env["replay"]), env["replay_id"], env["as_of"])
    except Exception:
        cm.release(key)  # let the redelivery start it
        raise
    log.info("started %s for %s (%d fields, replay=%s)", arn, scene["scene_id"], len(fields), env["replay"])
    return "started"


def handler(event: dict, context: Any = None) -> dict:
    failures = []
    for record in event.get("Records", []):
        try:
            outcome = process_message(record["body"])
            log.info("message %s -> %s", record.get("messageId"), outcome)
        except Exception:  # noqa: BLE001 - report the one message, keep the batch going
            log.exception("message %s failed", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
