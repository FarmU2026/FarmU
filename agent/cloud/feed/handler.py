"""Projector Lambda: EventBridge (bus `fieldwatch`) -> FeedMessages -> AppSync Events + thread history.

Environment:
    APPSYNC_HTTP_HOST   <api-id>.appsync-api.<region>.amazonaws.com (the Events HTTP endpoint host)
    FARMS_TABLE         fw-farms   (thread history items: farm_id / `THREAD#<at>#<id>`)
    INVESTIGATIONS_TABLE fw-investigations (investigation_id -> farm_id for events that omit it)
    AWS_REGION          set by Lambda

Order of work per event: write thread items (idempotent overwrite, same key for the same event), then
publish. If the publish fails the Lambda raises and EventBridge retries; the thread write repeats harmlessly.
A duplicate delivery can publish twice; every payload carries `event_id` so clients can drop repeats.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import boto3

from . import mapping
from .publisher import FeedPublisher

log = logging.getLogger()
log.setLevel(logging.INFO)

_farm_cache: dict[str, str] = {}


def farm_for_investigation(table, investigation_id: str) -> str:
    """fw-investigations item -> farm_id (top level `farm_id` or `field.farm_id`). Cached per container."""
    if investigation_id in _farm_cache:
        return _farm_cache[investigation_id]
    item = table.get_item(Key={"investigation_id": investigation_id}).get("Item")
    farm_id = None
    if item:
        farm_id = item.get("farm_id") or (item.get("field") or {}).get("farm_id")
    if not farm_id:
        raise mapping.UnknownFarm(f"no farm for investigation {investigation_id}")
    _farm_cache[investigation_id] = farm_id
    return farm_id


def write_thread(table, items: list[mapping.ThreadItem]) -> None:
    for it in items:
        table.put_item(Item=it.to_item())  # same event -> same key -> idempotent overwrite


def process(event: dict[str, Any], *, publisher: FeedPublisher, farms_table, investigations_table) -> dict[str, Any]:
    """Handle one EventBridge envelope. Pure of module globals so tests can inject fakes."""
    detail_type = event["detail-type"]
    if detail_type not in mapping.EVENT_MODELS:
        log.info("ignoring %s", detail_type)
        return {"ignored": detail_type}
    projection = mapping.project(
        detail_type, event["detail"], event.get("id", ""),
        farm_lookup=lambda inv_id: farm_for_investigation(investigations_table, inv_id))
    write_thread(farms_table, projection.thread)
    published = 0
    for chan, messages in projection.publishes().items():
        published += publisher.publish(chan, messages)
    log.info("%s -> %d message(s) published, %d thread item(s)", detail_type, published, len(projection.thread))
    return {"published": published, "thread": len(projection.thread)}


def handler(event, _context=None):
    session = boto3.Session()
    region = os.environ.get("AWS_REGION", "us-west-2")
    ddb = boto3.resource("dynamodb", region_name=region)
    return process(
        event,
        publisher=FeedPublisher(os.environ["APPSYNC_HTTP_HOST"], region, session.get_credentials()),
        farms_table=ddb.Table(os.environ.get("FARMS_TABLE", "fw-farms")),
        investigations_table=ddb.Table(os.environ.get("INVESTIGATIONS_TABLE", "fw-investigations")),
    )
