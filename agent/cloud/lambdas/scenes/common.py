"""Shared helpers for the scene-pipeline Lambdas: config, AWS clients, DynamoDB conversion, STAC parsing, events.

Clients are created lazily and cached so unit tests can run under moto (call `reset_clients()` between tests)
or inject fakes with `set_client()`.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import boto3

from fieldwatch import config
from fieldwatch import contracts as C

log = logging.getLogger("fieldwatch.scenes")
log.setLevel(logging.INFO)

# ---------- constants (docs/implementation-specs.md "Imagery pipeline") ----------

ACCEPTED_SOURCES = ("sentinel-2-l2a", "sentinel-2-c1-l2a")  # docs/decisions.md: both Earth Search collections
PREFERRED_SOURCE = "sentinel-2-c1-l2a"
BANDS = ("blue", "green", "red", "rededge1", "nir", "nir08", "swir16", "scl")
MAX_SCENE_CLOUD = 80.0  # scene filter drops anything cloudier than this
IDEMPOTENCY_TTL_DAYS = 7
SCENE_ANALYSIS_TTL_DAYS = 180
EVENT_SOURCE = "fieldwatch.scenes"
REPLAY_EVENT_SOURCE = "fieldwatch.replay"
STAC_URL = os.getenv("STAC_URL", "https://earth-search.aws.element84.com/v1")
MRMS_BUCKET = "noaa-mrms-pds"
MRMS_DAILY_PRODUCT = "MESH_Max_1440min_00.50"
HAIL_BACKFILL_DAYS = 14

TERMINAL_WATCH_STATUSES = {"closed", "expired", "cancelled", "canceled", "resolved"}

# ---------- env ----------


def region() -> str:
    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or config.REGION


def table_name(short: str) -> str:
    """`scene-analyses` -> `fw-scene-analyses` (prefix from FIELDWATCH_TABLE_PREFIX)."""
    return f"{os.getenv('FIELDWATCH_TABLE_PREFIX', config.TABLE_PREFIX)}{short}"


def event_bus() -> str:
    return os.getenv("FIELDWATCH_EVENT_BUS", config.EVENT_BUS)


def bucket() -> str:
    return os.getenv("FIELDWATCH_BUCKET", config.BUCKET)


# ---------- clients ----------

_CLIENTS: dict[str, Any] = {}
_RESOURCES: dict[str, Any] = {}


def client(service: str):
    if service not in _CLIENTS:
        _CLIENTS[service] = boto3.client(service, region_name=region())
    return _CLIENTS[service]


def set_client(service: str, obj: Any) -> None:
    _CLIENTS[service] = obj


def reset_clients() -> None:
    _CLIENTS.clear()
    _RESOURCES.clear()


def ddb():
    if "ddb" not in _RESOURCES:
        _RESOURCES["ddb"] = boto3.resource("dynamodb", region_name=region())
    return _RESOURCES["ddb"]


def table(short: str):
    return ddb().Table(table_name(short))


# ---------- time ----------


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime | None:
    """ISO string, date string or epoch seconds/ms -> aware UTC datetime; None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        v = float(value)
        if v > 1e11:  # epoch milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, timezone.utc)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        return parse_time(float(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def epoch_in(days: float) -> int:
    return int((now() + timedelta(days=days)).timestamp())


# ---------- DynamoDB value conversion ----------


def to_native(obj: Any) -> Any:
    """Decimal -> int/float, recursively (boto3 resource returns Decimals)."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_native(v) for v in obj]
    return obj


def to_ddb(obj: Any) -> Any:
    """JSON-able value -> something the boto3 resource accepts (floats become Decimal)."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def query_all(tbl, **kwargs) -> list[dict]:
    out: list[dict] = []
    while True:
        resp = tbl.query(**kwargs)
        out.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def scan_all(tbl, **kwargs) -> list[dict]:
    out: list[dict] = []
    while True:
        resp = tbl.scan(**kwargs)
        out.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


# ---------- idempotency (fw-idempotency, conditional puts) ----------


def claim(key: str, **attrs: Any) -> bool:
    """Record `key` in fw-idempotency; False when it was already there (a repeat delivery)."""
    item = {"key": key, "claimed_at": iso(now()), "expires_at": epoch_in(IDEMPOTENCY_TTL_DAYS), **attrs}
    try:
        table("idempotency").put_item(Item=to_ddb(item), ConditionExpression="attribute_not_exists(#k)",
                                      ExpressionAttributeNames={"#k": "key"})
        return True
    except ddb().meta.client.exceptions.ConditionalCheckFailedException:
        return False


def release(key: str) -> None:
    table("idempotency").delete_item(Key={"key": key})


# ---------- STAC items ----------


def tile_key_candidates(tile_id: str) -> list[str]:
    """fw-tile-index is keyed by the Sentinel-2 grid tile; accept `MGRS-14TPM`, `14TPM` and `T14TPM` spellings."""
    base = tile_id.strip()
    if base.upper().startswith("MGRS-"):
        base = base[5:]
    bare = base[1:] if base.upper().startswith("T") and base[1:2].isdigit() else base
    out: list[str] = []
    for cand in (tile_id.strip(), f"MGRS-{bare}", bare, f"T{bare}"):
        if cand not in out:
            out.append(cand)
    return out


def scene_from_item(item: dict) -> dict:
    """STAC item -> SceneInfo as a dict. Raises ValueError for items that are not usable Sentinel-2 scenes."""
    props = item.get("properties") or {}
    tile = props.get("grid:code")
    if not tile:
        raise ValueError("item has no grid:code")
    when = props.get("datetime") or props.get("start_datetime")
    if not when or not item.get("id"):
        raise ValueError("item has no id or datetime")
    assets = item.get("assets") or {}
    scene = {
        "scene_id": item["id"],
        "source": item.get("collection") or "",
        "datetime": when,
        "tile_id": tile,
        "cloud_cover": float(props.get("eo:cloud_cover", 0.0)),
        "assets": {b: assets[b]["href"] for b in BANDS if b in assets and assets[b].get("href")},
    }
    return C.SceneInfo(**scene).model_dump(mode="json")


def scene_date(scene: dict) -> str:
    return scene["datetime"][:10]


# ---------- events ----------


def put_events(entries: list[dict]) -> None:
    """entries: {source, detail_type, detail(dict)}. Raises when EventBridge rejects any entry."""
    bus = event_bus()
    for i in range(0, len(entries), 10):
        batch = entries[i:i + 10]
        resp = client("events").put_events(Entries=[
            {"Source": e["source"], "DetailType": e["detail_type"], "Detail": json.dumps(e["detail"]),
             "EventBusName": bus} for e in batch
        ])
        if resp.get("FailedEntryCount"):
            bad = [r for r in resp["Entries"] if r.get("ErrorCode")]
            raise RuntimeError(f"PutEvents failed for {len(bad)} entries: {bad[:2]}")


def emit(model: C.EventBase, source: str = EVENT_SOURCE) -> None:
    put_events([{"source": source, "detail_type": type(model).__name__, "detail": model.model_dump(mode="json")}])


# ---------- field records ----------

_CROP_ALIASES = {"soybean": "soybeans", "soy": "soybeans", "beans": "soybeans", "maize": "corn",
                 "sorghum": "milo", "winter wheat": "wheat", "spring wheat": "wheat"}


def normalize_crop(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = _CROP_ALIASES.get(text, text)
    return text if text in ("corn", "soybeans", "wheat", "milo", "alfalfa") else "other"


def ring_from_geojson(geo: dict) -> list[list[float]]:
    """Exterior ring (lon/lat, no repeated closing point) of the first polygon in any GeoJSON wrapper."""
    kind = geo.get("type")
    if kind == "FeatureCollection":
        geo = (geo.get("features") or [{}])[0]
        kind = geo.get("type")
    if kind == "Feature":
        geo = geo.get("geometry") or {}
        kind = geo.get("type")
    coords = geo.get("coordinates")
    if kind == "Polygon":
        ring = coords[0]
    elif kind == "MultiPolygon":
        ring = coords[0][0]
    else:
        raise ValueError(f"unsupported geometry type {kind!r}")
    ring = [[float(p[0]), float(p[1])] for p in ring]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        raise ValueError("polygon has fewer than 3 points")
    return ring


def field_polygon(field_item: dict) -> list[list[float]]:
    """A field's boundary ring: inline `polygon`/`geometry` on the fw-fields row, else the S3 `geometry_key`."""
    item = to_native(field_item)
    if item.get("polygon"):
        ring = [[float(p[0]), float(p[1])] for p in item["polygon"]]
        return ring[:-1] if len(ring) > 3 and ring[0] == ring[-1] else ring
    if isinstance(item.get("geometry"), dict):
        return ring_from_geojson(item["geometry"])
    key = item.get("geometry_key")
    if not key:
        raise ValueError("field has no polygon, geometry or geometry_key")
    body = client("s3").get_object(Bucket=bucket(), Key=key)["Body"].read()
    return ring_from_geojson(json.loads(body))
