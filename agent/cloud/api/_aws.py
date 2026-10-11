"""AWS access for the API Lambdas: clients, resource names, DynamoDB conversion, bus puts, SSM.

Names come from the environment at call time (set by infra/stacks/api.py), falling back to
fieldwatch.config defaults, so tests can set env vars per test.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from fieldwatch import config, interfaces

from ._http import dumps


def region() -> str:
    return os.getenv("AWS_REGION") or config.REGION


@lru_cache(maxsize=None)
def _client(service: str, region_name: str):
    if service == "s3":
        # SigV4 presigned URLs on the regional endpoint (what browsers get for overlays and uploads).
        from botocore.config import Config
        return boto3.client("s3", region_name=region_name,
                            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    return boto3.client(service, region_name=region_name)


@lru_cache(maxsize=None)
def _resource(service: str, region_name: str):
    return boto3.resource(service, region_name=region_name)


def client(service: str):
    return _client(service, region())


def reset_clients() -> None:
    """Tests: drop cached clients so a fresh moto mock is used."""
    _client.cache_clear()
    _resource.cache_clear()
    _ssm_cache.clear()


def table(short: str):
    """table("farms") -> the fw-farms Table resource."""
    prefix = os.getenv("FIELDWATCH_TABLE_PREFIX", config.TABLE_PREFIX)
    return _resource("dynamodb", region()).Table(prefix + short)


def bucket() -> str:
    return os.getenv("FIELDWATCH_BUCKET", config.BUCKET)


def bus() -> str:
    return os.getenv("FIELDWATCH_EVENT_BUS", config.EVENT_BUS)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch() -> int:
    return int(time.time())


# ---------- DynamoDB conversion ----------


def to_ddb(obj: Any) -> Any:
    """Floats -> Decimal (DynamoDB rejects floats); drop None values at the top level."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if v is not None}
    return json.loads(dumps(obj), parse_float=Decimal)


def from_ddb(obj: Any) -> Any:
    """Decimal -> int/float, recursively."""
    if isinstance(obj, list):
        return [from_ddb(x) for x in obj]
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def query_all(tbl, **kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        page = tbl.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return [from_ddb(i) for i in items]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def scan_all(tbl, **kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        page = tbl.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return [from_ddb(i) for i in items]
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def get_item(short: str, **key) -> dict | None:
    item = table(short).get_item(Key=key).get("Item")
    return from_ddb(item) if item else None


def query_pk(short: str, pk_name: str, pk: str, **kwargs) -> list[dict]:
    return query_all(table(short), KeyConditionExpression=Key(pk_name).eq(pk), **kwargs)


# ---------- EventBridge ----------


def put_event(detail_type: str, detail: Any, source: str = interfaces.Sources.API) -> None:
    """Publish a contract event through interfaces.publish_event (validated against EVENT_REGISTRY)."""
    if not hasattr(detail, "model_dump"):
        detail = interfaces.event_model(detail_type).model_validate(detail)
    if type(detail).__name__ != detail_type:
        raise ValueError(f"detail_type {detail_type} does not match {type(detail).__name__}")
    interfaces.publish_event(detail, source=source, client=client("events"), bus=bus())


# ---------- SSM ----------

_ssm_cache: dict[str, str | None] = {}


def ssm_param(name: str, *, required: bool = True) -> str | None:
    """SecureString parameter value, cached per container. Missing + not required -> None."""
    if name not in _ssm_cache:
        try:
            value = client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except client("ssm").exceptions.ParameterNotFound:
            if required:
                raise
            value = None
        _ssm_cache[name] = value
    return _ssm_cache[name]


# ---------- S3 ----------


def presign_get(key: str, expires: int = 900) -> str:
    return client("s3").generate_presigned_url("get_object", Params={"Bucket": bucket(), "Key": key},
                                               ExpiresIn=expires)


def presign_put(key: str, content_type: str, expires: int = 900) -> str:
    return client("s3").generate_presigned_url(
        "put_object", Params={"Bucket": bucket(), "Key": key, "ContentType": content_type}, ExpiresIn=expires)
