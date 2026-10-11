"""Shared helpers for test_runtime*.py: a scripted fake model and moto resources named like the data plane."""
import json
import uuid

import boto3
from strands.models.model import Model

REGION = "us-west-2"

DIAGNOSIS = {
    "field_name": "north pivot",
    "what": "The crop in a ring on the outer edge is stressed.",
    "where": "outer ring, last tower",
    "why": "Rain has been normal and the soil is uniform, so a nozzle or tower likely isn't watering.",
    "ruled_out": ["drought", "hail", "sandy soil"],
    "dollars_low": 9000,
    "dollars_high": 14000,
    "how_sure": "pretty sure",
    "recommended_action": "repair_work_order",
    "action_detail": "Check the nozzles and the end gun on the last tower.",
}


def _tool_use(name: str, payload: dict):
    tid = f"tu-{uuid.uuid4().hex[:8]}"
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tid, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def _text(text: str):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": text}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}}},
    ]


def _results_seen(messages) -> set[str]:
    names, seen = {}, set()
    for m in messages:
        for c in m.get("content", []):
            if "toolUse" in c:
                names[c["toolUse"]["toolUseId"]] = c["toolUse"]["name"]
            if "toolResult" in c:
                seen.add(names.get(c["toolResult"]["toolUseId"], "?"))
    return seen


class FakeModel(Model):
    """Rule-based stand-in for Bedrock: ask for a work order, then emit the Diagnosis."""

    def update_config(self, **kw):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kw):  # pragma: no cover
        yield {"output": output_model()}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        tools = {t["name"] for t in (tool_specs or [])}
        done = _results_seen(messages)
        if "create_work_order" in tools and "create_work_order" not in done:
            events = _tool_use(
                "create_work_order",
                {"field_id": "north-pivot", "assignee": "Brother", "what_to_check": "nozzles on the last tower"},
            )
        elif "Diagnosis" in tools and "Diagnosis" not in done:
            events = _tool_use("Diagnosis", DIAGNOSIS)
        else:
            events = _text("ok")
        for e in events:
            yield e


def make_investigations_table():
    """fw-investigations as the data plane defines it (pk investigation_id, GSI by-field)."""
    ddb = boto3.resource("dynamodb", region_name=REGION)
    return ddb.create_table(
        TableName="fw-investigations",
        KeySchema=[{"AttributeName": "investigation_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "investigation_id", "AttributeType": "S"},
            {"AttributeName": "field_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "by-field",
            "KeySchema": [{"AttributeName": "field_id", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )


def make_fields_table():
    """fw-fields as the data plane defines it (pk farm_id, sk field_id)."""
    return boto3.resource("dynamodb", region_name=REGION).create_table(
        TableName="fw-fields",
        KeySchema=[{"AttributeName": "farm_id", "KeyType": "HASH"}, {"AttributeName": "field_id", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "farm_id", "AttributeType": "S"},
                              {"AttributeName": "field_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def make_bus_with_queue():
    """The `fieldwatch` bus plus an SQS queue catching everything on it. Returns (events client, sqs client, queue url)."""
    events = boto3.client("events", region_name=REGION)
    sqs = boto3.client("sqs", region_name=REGION)
    events.create_event_bus(Name="fieldwatch")
    url = sqs.create_queue(QueueName="catch-all")["QueueUrl"]
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    events.put_rule(Name="all", EventBusName="fieldwatch", EventPattern=json.dumps({"source": [{"prefix": "fieldwatch."}]}))
    events.put_targets(Rule="all", EventBusName="fieldwatch", Targets=[{"Id": "q", "Arn": arn}])
    return events, sqs, url


def drain(sqs, url) -> list[dict]:
    """All bus events caught so far, as the EventBridge envelopes."""
    out = []
    while True:
        msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
        if not msgs:
            return out
        for m in msgs:
            out.append(json.loads(m["Body"]))
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
