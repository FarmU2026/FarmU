"""Put sample events on the `fieldwatch` bus to exercise the deployed projector. Uses REAL AWS credentials,
so only Caleb runs it, after deploying FieldWatch-Feed:

    cd agent
    python -m cloud.feed.send_sample --farm wohleb-farm [--seed-investigation] [--only message_out]

Events come from fixtures/contracts/ with farm_id swapped. --seed-investigation writes the fw-investigations
item the projector needs to find the farm for step/approval events.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "contracts"
FIXTURE_EVENTS = {
    "AnomalyDetected": "anomaly_detected", "InvestigationProgress": "investigation_progress",
    "InvestigationConcluded": "investigation_concluded", "ReplyReceived": "reply_received",
    "ApprovalRecorded": "approval_recorded",
}


def build(farm: str) -> list[tuple[str, dict]]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list[tuple[str, dict]] = []
    for detail_type, fixture in FIXTURE_EVENTS.items():
        d = json.loads((FIXTURES / f"{fixture}.json").read_text())
        d["occurred_at"] = now
        if "farm_id" in d:
            d["farm_id"] = farm
        if isinstance(d.get("field"), dict):
            d["field"]["farm_id"] = farm
        out.append((detail_type, d))
    stamp = now.replace(":", "").replace("-", "")
    inv = "inv-north-pivot-20260718"
    out += [
        ("MessageSent", {"schema_version": "1", "occurred_at": now, "message_id": f"msg-{stamp}-out", "farm_id": farm,
                         "person_id": "dad", "body": "Part of the north pivot field looks dry. Reply MORE for details.",
                         "investigation_id": inv, "status": "mirror_only", "provider": "mirror"}),
        ("MessageReceived", {"schema_version": "1", "occurred_at": now, "message_id": f"msg-{stamp}-in", "farm_id": farm,
                             "person_id": "dad", "body": "MORE", "investigation_id": inv}),
        ("ActionCompleted", {"schema_version": "1", "occurred_at": now, "action_id": "act-001",
                             "investigation_id": inv, "completed_by": "brother"}),
    ]
    return out


def main() -> None:
    import boto3  # real AWS

    ap = argparse.ArgumentParser()
    ap.add_argument("--farm", default="wohleb-farm")
    ap.add_argument("--seed-investigation", action="store_true")
    ap.add_argument("--only", help="a detail-type, e.g. MessageSent")
    ap.add_argument("--region", default="us-west-2")
    a = ap.parse_args()
    if a.seed_investigation:
        boto3.resource("dynamodb", region_name=a.region).Table("fw-investigations").put_item(Item={
            "investigation_id": "inv-north-pivot-20260718", "farm_id": a.farm,
            "field": {"farm_id": a.farm, "field_id": "north-pivot"}, "field_id": "north-pivot"})
    events = boto3.client("events", region_name=a.region)
    for detail_type, detail in build(a.farm):
        if a.only and a.only != detail_type:
            continue
        r = events.put_events(Entries=[{"EventBusName": "fieldwatch", "Source": "fieldwatch.sample",
                                        "DetailType": detail_type, "Detail": json.dumps(detail)}])
        print(detail_type, "->", r["Entries"][0].get("EventId") or r["Entries"][0])


if __name__ == "__main__":
    main()
