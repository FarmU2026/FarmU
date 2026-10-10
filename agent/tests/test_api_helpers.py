"""Shared fixtures for the web API tests (WS12): a moto AWS with the fw- tables, bucket, bus and queue.

Tables come from infra/stacks/data_plane.py's TABLES list (read with ast, so no CDK import needed).
"""
from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

REPO = Path(__file__).resolve().parents[2]
REGION = "us-west-2"
BUCKET = "fw-test-bucket"
LINK_KEY = "test-link-key-0123456789"


def data_plane_tables() -> list[tuple]:
    tree = ast.parse((REPO / "infra" / "stacks" / "data_plane.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "TABLES" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("TABLES not found in data_plane.py")


def create_tables(ddb) -> None:
    for name, pk, sk, gsis, _ttl in data_plane_tables():
        attrs = {pk: "S"}
        keys = [{"AttributeName": pk, "KeyType": "HASH"}]
        if sk:
            attrs[sk] = "S"
            keys.append({"AttributeName": sk, "KeyType": "RANGE"})
        gsi_defs = []
        for gname, gpk, gsk in gsis:
            attrs[gpk] = "S"
            gk = [{"AttributeName": gpk, "KeyType": "HASH"}]
            if gsk:
                attrs[gsk] = "S"
                gk.append({"AttributeName": gsk, "KeyType": "RANGE"})
            gsi_defs.append({"IndexName": gname, "KeySchema": gk, "Projection": {"ProjectionType": "ALL"}})
        kwargs = dict(TableName=name, KeySchema=keys, BillingMode="PAY_PER_REQUEST",
                      AttributeDefinitions=[{"AttributeName": k, "AttributeType": v} for k, v in attrs.items()])
        if gsi_defs:
            kwargs["GlobalSecondaryIndexes"] = gsi_defs
        ddb.create_table(**kwargs)


@pytest.fixture
def aws(monkeypatch):
    """moto-backed AWS. Yields a namespace with clients, the queue URL and a list of emitted events."""
    import boto3
    from moto import mock_aws

    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": REGION, "AWS_REGION": REGION, "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
                 "AWS_CONFIG_FILE": "/nonexistent", "FIELDWATCH_BUCKET": BUCKET, "FIELDWATCH_LINK_KEY": LINK_KEY,
                 "FIELDWATCH_EVENT_BUS": "fieldwatch", "FIELDWATCH_TABLE_PREFIX": "fw-"}.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "FIELDWATCH_SCENE_QUEUE_URL", "FIELDWATCH_CONTEXT_SM_ARN", "FIELDWATCH_REPLAY_FN",
              "FIELDWATCH_RESET_FN", "FIELDWATCH_SEND_FN", "FIELDWATCH_TWILIO_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)

    from cloud.api import _aws
    from fieldwatch import links

    with mock_aws():
        _aws.reset_clients()
        links.clear_key_cache()
        create_tables(boto3.client("dynamodb", region_name=REGION))
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        boto3.client("events", region_name=REGION).create_event_bus(Name="fieldwatch")
        sqs = boto3.client("sqs", region_name=REGION)
        queue_url = sqs.create_queue(QueueName="fw-scene-queue")["QueueUrl"]

        events: list[tuple[str, dict]] = []
        real_put = _aws.put_event

        def recording_put(detail_type, detail, source="fieldwatch.api"):
            real_put(detail_type, detail, source)
            body = json.loads(detail.model_dump_json()) if hasattr(detail, "model_dump_json") else detail
            events.append((detail_type, body))

        monkeypatch.setattr(_aws, "put_event", recording_put)

        class NS:
            pass

        ns = NS()
        ns.s3, ns.sqs, ns.queue_url, ns.events = s3, sqs, queue_url, events
        ns.ddb = boto3.resource("dynamodb", region_name=REGION)
        ns.ssm = boto3.client("ssm", region_name=REGION)
        ns.sfn = boto3.client("stepfunctions", region_name=REGION)
        ns.table = lambda short: ns.ddb.Table("fw-" + short)

        # The API calls the real fieldwatch.approvals; point it at the same moto table the pages read, and
        # collect its ApprovalRecorded events next to the API's own (tests/conftest.py resets both).
        from fieldwatch import approvals

        approvals.use_store(approvals.DynamoStore(ns.ddb, prefix="fw-"))
        approvals.set_event_sink(lambda kind, detail: events.append((kind, detail)))

        def queue_messages():
            out = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10).get("Messages", [])
            return [json.loads(m["Body"]) for m in out]

        ns.queue_messages = queue_messages
        yield ns
        approvals.use_store(None)
        approvals.set_event_sink(None)
        _aws.reset_clients()
        links.clear_key_cache()


def put_approval(aws, approval_id: str = "apr_1", **overrides) -> dict:
    """A valid fw-approvals row (fieldwatch.contracts.Approval), pending and unexpired unless overridden."""
    item = {"approval_id": approval_id, "investigation_id": "inv_1", "action_type": "work_order",
            "summary": "Send a repair ticket", "sent_to_person_id": "per_4025550100", "status": "pending",
            "expires_at": "2999-01-01T00:00:00Z", **overrides}
    aws.table("approvals").put_item(Item=item)
    return item


def event(method: str, path: str, body=None, *, query: dict | None = None, headers: dict | None = None,
          form: dict | None = None, b64: bool = False, route_key: str | None = None) -> dict:
    """An API Gateway HTTP API (payload 2.0) event."""
    hdrs = {"host": "abc123.execute-api.us-west-2.amazonaws.com", **(headers or {})}
    if form is not None:
        raw = urlencode(form)
        hdrs.setdefault("content-type", "application/x-www-form-urlencoded")
    elif body is not None:
        raw = json.dumps(body)
        hdrs.setdefault("content-type", "application/json")
    else:
        raw = None
    if raw is not None and b64:
        raw = base64.b64encode(raw.encode()).decode()
    ev = {
        "version": "2.0", "rawPath": path, "rawQueryString": urlencode(query or {}),
        "headers": hdrs, "queryStringParameters": query or None,
        "requestContext": {"http": {"method": method, "path": path}, "domainName": hdrs["host"]},
        "body": raw, "isBase64Encoded": b64,
    }
    if route_key:
        ev["routeKey"] = route_key
    return ev


def call(module, method: str, path: str, body=None, **kw) -> tuple[int, dict | str, dict]:
    resp = module.handler(event(method, path, body, **kw), None)
    raw = resp.get("body") or ""
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    return resp["statusCode"], parsed, resp.get("headers", {})


# A field near the Thurston County test area, as the UI sends it: [lat, lon] points.
NORTH_PIVOT_LATLON = [[42.168, -96.548], [42.168, -96.532], [42.152, -96.532], [42.152, -96.548]]


def signup(farms_module, phone="402-555-0100", farmer="Dad", fields=None):
    body = {"phone": phone, "farmer": farmer,
            "fields": fields or [{"name": "North Pivot", "crop": "corn", "polygon": NORTH_PIVOT_LATLON}]}
    return call(farms_module, "POST", "/api/farms", body)


def put_analysis(aws_ns, field_id: str, date: str = "2026-07-18", **extra) -> dict:
    fixture = json.loads((REPO / "agent" / "fixtures" / "contracts" / "field_analysis_result.json").read_text())
    row = {
        "field_id": field_id, "date_source": f"{date}#sentinel-2-l2a", "date": date, "status": "ok",
        "max_score": 3.1, "clear_fraction": fixture["clear_fraction"], "indices": fixture["indices"],
        "regions": fixture["anomaly"]["regions"], "overlays": fixture["overlays"],
        "analysis_key": fixture["analysis_key"], **extra,
    }
    from cloud.api import _aws
    aws_ns.table("scene-analyses").put_item(Item=_aws.to_ddb(row))
    return row
