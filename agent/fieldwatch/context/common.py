"""Boundary, storage and artifact helpers; no agent or web imports."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from shapely.geometry import Polygon, box
from fieldwatch import config

def table(name):
    import boto3
    return boto3.resource("dynamodb", region_name=config.REGION).Table(config.TABLE_PREFIX + name)

def field(field_id):
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("FIELDWATCH_CONTEXT_BACKEND") == "cloud":
        from boto3.dynamodb.conditions import Key
        rows = table("fields").query(IndexName="by-field", KeyConditionExpression=Key("field_id").eq(field_id)).get("Items", [])
        if len(rows) != 1:
            raise LookupError(f"field id must identify one field: {field_id}")
        return rows[0]
    farm = json.loads(config.FARM_FILE.read_text())
    matches = [f for f in farm["fields"] if f.get("id", f.get("field_id")) == field_id or f.get("name","").lower() == field_id.lower()]
    if len(matches) != 1:
        raise LookupError(f"unknown or ambiguous field: {field_id}")
    return dict(matches[0], farm_id=farm["farm_id"], field_id=matches[0].get("id",field_id))

def polygon(item):
    geom = Polygon(item["polygon"]) if item.get("polygon") else box(*map(float,item["bbox"]))
    if not geom.is_valid or geom.area <= 0:
        raise ValueError("invalid field polygon")
    return geom

def bucket():
    return os.getenv("BUCKET_NAME") or config.BUCKET

def write_artifact(key, data, content_type):
    if bucket():
        import boto3
        boto3.client("s3",region_name=config.REGION).put_object(Bucket=bucket(),Key=key,Body=data,ContentType=content_type)
        return key
    root = Path(os.getenv("FIELDWATCH_CONTEXT_CACHE", str(config.DATA / "context-cache")))
    destination = (root / key).resolve()
    if not destination.is_relative_to(root.resolve()):
        raise ValueError("artifact path escapes context cache")
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_bytes(data)
    return str(destination)

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
