"""Scene pipeline (workstream 2): filter rules, field inputs, decision rule, idempotent writes, replay, reset, schedule,
and the shape of the Step Functions definition. AWS is mocked with moto and fakes; nothing here touches the network."""
from __future__ import annotations

import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from fieldwatch import contracts as C
from cloud.lambdas.scenes import analyze, check_latest, common as cm, decide, load_inputs, replay, reset, scene_filter

REPO = Path(__file__).resolve().parents[2]
SAMPLES = REPO / "agent" / "fixtures" / "contracts"

FARM = "wohleb-farm"
FIELD = "north-pivot"
TILE = "MGRS-14TPM"
SCENE_ID = "S2B_T14TPM_20260718T171620_L2A"
RING = [[-96.548, 42.152], [-96.532, 42.152], [-96.532, 42.168], [-96.548, 42.168]]


# ---------- fakes and fixtures ----------


class FakeSFN:
    def __init__(self):
        self.started: list[dict] = []
        self.fail = False

    def start_execution(self, **kw):
        if self.fail:
            raise RuntimeError("states unavailable")
        self.started.append(kw)
        return {"executionArn": f"arn:aws:states:us-west-2:1:express:{kw['name']}"}


class FakeEvents:
    def __init__(self):
        self.entries: list[dict] = []
        self.fail = False

    def put_events(self, Entries):  # noqa: N803 - boto3 casing
        if self.fail:
            return {"FailedEntryCount": len(Entries), "Entries": [{"ErrorCode": "Throttled"} for _ in Entries]}
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e"} for _ in Entries]}

    def of(self, detail_type: str) -> list[dict]:
        return [json.loads(e["Detail"]) for e in self.entries if e["DetailType"] == detail_type]


def _create_tables():
    sys.path.insert(0, str(REPO / "infra"))
    from stacks.data_plane import TABLES  # the real key schema, so tests fail if the data plane changes

    db = boto3.client("dynamodb")
    for name, pk, sk, gsis, _ttl in TABLES:
        attrs = {pk, *( [sk] if sk else []), *[g[1] for g in gsis], *[g[2] for g in gsis if g[2]]}
        db.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}] + ([{"AttributeName": sk, "KeyType": "RANGE"}] if sk else []),
            GlobalSecondaryIndexes=[{
                "IndexName": g[0], "Projection": {"ProjectionType": "ALL"},
                "KeySchema": [{"AttributeName": g[1], "KeyType": "HASH"}] + ([{"AttributeName": g[2], "KeyType": "RANGE"}] if g[2] else []),
            } for g in gsis] or None,
        ) if gsis else db.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
            KeySchema=[{"AttributeName": pk, "KeyType": "HASH"}] + ([{"AttributeName": sk, "KeyType": "RANGE"}] if sk else []),
        )


@pytest.fixture(autouse=True)
def sample_analyzer(monkeypatch):
    """The real analyzer (WS1) reads Sentinel-2 over the network; these tests run the pipeline on the contract sample."""
    from fieldwatch import registry

    monkeypatch.setitem(registry.BY_NAME, "analyze_field", registry.ToolSpec(
        "analyze_field", "d", "read", C.FieldAnalysisRequest, registry._sample("field_analysis_result", C.FieldAnalysisResult),
        output_model=C.FieldAnalysisResult))


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "x", "AWS_DEFAULT_REGION": "us-west-2",
                 "AWS_REGION": "us-west-2", "FIELDWATCH_BUCKET": "fw-test-bucket",
                 "STATE_MACHINE_ARN": "arn:aws:states:us-west-2:1:stateMachine:fw-scene-analysis"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        cm.reset_clients()
        _create_tables()
        boto3.client("s3", region_name="us-west-2").create_bucket(
            Bucket="fw-test-bucket", CreateBucketConfiguration={"LocationConstraint": "us-west-2"})
        queue_url = boto3.client("sqs").create_queue(QueueName="fw-scene-queue")["QueueUrl"]
        monkeypatch.setenv("SCENE_QUEUE_URL", queue_url)
        sfn, events = FakeSFN(), FakeEvents()
        cm.set_client("stepfunctions", sfn)
        cm.set_client("events", events)
        yield type("Aws", (), {"sfn": sfn, "events": events, "queue_url": queue_url})
        cm.reset_clients()


def stac_item(scene_id=SCENE_ID, collection="sentinel-2-c1-l2a", tile=TILE, cloud=5.0, when="2026-07-18T17:22:15.116000Z"):
    bands = ["blue", "green", "red", "rededge1", "nir", "nir08", "swir16", "scl", "coastal", "visual"]
    return {
        "type": "Feature", "id": scene_id, "collection": collection, "bbox": [-97.5, 41.4, -96.0, 42.5],
        "properties": {"datetime": when, "eo:cloud_cover": cloud, "grid:code": tile},
        "assets": {b: {"href": f"https://example.test/{scene_id}/{b}.tif"} for b in bands},
    }


def register_field(field_id=FIELD, farm_id=FARM, tile=TILE, geometry=True, **extra):
    db = cm.ddb()
    row = {"farm_id": farm_id, "field_id": field_id, "name": field_id, "crop_current": "corn", "status": "ready", **extra}
    if geometry:
        boto3.client("s3").put_object(
            Bucket="fw-test-bucket", Key=f"fields/{field_id}/geometry.geojson",
            Body=json.dumps({"type": "Polygon", "coordinates": [RING + [RING[0]]]}))
        row["geometry_key"] = f"fields/{field_id}/geometry.geojson"
    db.Table("fw-fields").put_item(Item=cm.to_ddb(row))
    if tile:
        db.Table("fw-tile-index").put_item(Item={"tile_id": tile, "field_id": field_id, "farm_id": farm_id})


def envelope(item=None, replay_id="rpl_1", **extra):
    return json.dumps({"replay": True, "replay_id": replay_id, "stac_item": item or stac_item(), **extra})


def scene_info(**kw) -> dict:
    return cm.scene_from_item(stac_item(**kw))


def idem_keys() -> set[str]:
    return {r["key"] for r in cm.scan_all(cm.table("idempotency"))}


# ---------- scene filter ----------


class TestSceneFilter:
    def test_tile_match_starts_one_execution_with_the_scene_input(self, aws):
        register_field()
        register_field("south-quarter")
        assert scene_filter.process_message(json.dumps(stac_item())) == "started"
        (call,) = aws.sfn.started
        assert call["stateMachineArn"].endswith("fw-scene-analysis")
        payload = json.loads(call["input"])
        assert payload["schema_version"] == "1" and payload["replay"] is False and payload["replay_id"] is None
        scene = C.SceneInfo(**payload["scene"])
        assert (scene.scene_id, scene.source, scene.tile_id, scene.cloud_cover) == (SCENE_ID, "sentinel-2-c1-l2a", TILE, 5.0)
        assert set(scene.assets) == {"blue", "green", "red", "rededge1", "nir", "nir08", "swir16", "scl"}
        assert {f["field_id"] for f in payload["fields"]} == {FIELD, "south-quarter"}
        assert all(C.FieldRef(**f) for f in payload["fields"])
        assert len(call["name"]) <= 80

    def test_legacy_collection_is_accepted(self, aws):
        register_field()
        item = stac_item("S2B_14TPM_20260718_0_L2A", collection="sentinel-2-l2a")
        assert scene_filter.process_message(json.dumps(item)) == "started"

    @pytest.mark.parametrize("stored", ["14TPM", "T14TPM", "MGRS-14TPM"])
    def test_tile_index_spelling_does_not_matter(self, aws, stored):
        register_field(tile=stored)
        assert scene_filter.process_message(json.dumps(stac_item())) == "started"
        assert len(json.loads(aws.sfn.started[0]["input"])["fields"]) == 1

    def test_other_collections_are_skipped(self, aws):
        register_field()
        assert scene_filter.process_message(json.dumps(stac_item(collection="sentinel-1-grd"))) == "skipped:collection"
        assert aws.sfn.started == [] and idem_keys() == set()

    def test_no_registered_fields_in_the_tile(self, aws):
        register_field(tile="MGRS-15TXK")
        assert scene_filter.process_message(json.dumps(stac_item())) == "skipped:no_fields"
        assert aws.sfn.started == []

    def test_cloud_rule(self, aws):
        register_field()
        assert scene_filter.process_message(json.dumps(stac_item(cloud=80.1))) == "skipped:cloud"
        assert scene_filter.process_message(json.dumps(stac_item(cloud=80.0))) == "started"

    def test_cloudy_scene_is_dropped_before_idempotency(self, aws):
        register_field()
        scene_filter.process_message(json.dumps(stac_item(cloud=95)))
        assert idem_keys() == set()  # a cloudy scene may not block a later, corrected delivery

    def test_repeat_delivery_stops_at_the_idempotency_check(self, aws):
        register_field()
        body = json.dumps(stac_item())
        assert scene_filter.process_message(body) == "started"
        assert scene_filter.process_message(body) == "skipped:duplicate"
        assert len(aws.sfn.started) == 1
        row = cm.table("idempotency").get_item(Key={"key": f"scene#{SCENE_ID}#field#{FIELD}"})["Item"]
        assert row["replay"] is False and int(row["expires_at"]) > datetime.now(timezone.utc).timestamp()

    def test_a_field_added_after_the_scene_still_gets_it(self, aws):
        register_field()
        body = json.dumps(stac_item())
        assert scene_filter.process_message(body) == "started"
        register_field(field_id="late-field")   # e.g. the farmer drew another field on the same tile
        assert scene_filter.process_message(body) == "started"
        assert scene_filter.process_message(body) == "skipped:duplicate"
        second = aws.sfn.started[1]
        assert [f["field_id"] for f in json.loads(second["input"])["fields"]] == ["late-field"]

    def test_sns_envelope_is_unwrapped(self, aws):
        register_field()
        body = json.dumps({"Type": "Notification", "TopicArn": "arn:aws:sns:eu-west-1:1:t", "Message": json.dumps(stac_item())})
        assert scene_filter.process_message(body) == "started"

    def test_garbage_is_dropped_not_retried(self, aws):
        assert scene_filter.process_message("not json at all {") == "skipped:invalid"
        assert scene_filter.process_message(json.dumps({"hello": "world"})) == "skipped:invalid"
        assert scene_filter.process_message(json.dumps({"type": "Feature", "id": "x", "collection": "sentinel-2-l2a",
                                                        "properties": {}, "assets": {}})) == "skipped:invalid"

    def test_replay_envelope(self, aws):
        register_field()
        assert scene_filter.process_message(envelope(as_of="2026-07-18")) == "started"
        payload = json.loads(aws.sfn.started[0]["input"])
        assert payload["replay"] is True and payload["replay_id"] == "rpl_1" and payload["as_of"] == "2026-07-18"
        assert f"scene#{SCENE_ID}#replay#rpl_1" in idem_keys()
        row = cm.table("idempotency").get_item(Key={"key": f"scene#{SCENE_ID}#replay#rpl_1"})["Item"]
        assert row["replay"] is True

    def test_replay_keys_on_scene_and_replay_id(self, aws):
        register_field()
        assert scene_filter.process_message(envelope(replay_id="a")) == "started"
        assert scene_filter.process_message(envelope(replay_id="a")) == "skipped:duplicate"
        assert scene_filter.process_message(envelope(replay_id="b")) == "started"
        assert len(aws.sfn.started) == 2

    def test_live_and_replay_keys_are_separate(self, aws):
        register_field()
        assert scene_filter.process_message(json.dumps(stac_item())) == "started"
        assert scene_filter.process_message(envelope()) == "started"

    def test_replay_may_analyze_a_cloudy_scene(self, aws):
        register_field()
        assert scene_filter.process_message(envelope(stac_item(cloud=90))) == "started"

    def test_replay_field_ids_limit_the_fields(self, aws):
        register_field()
        register_field("south-quarter")
        assert scene_filter.process_message(envelope(field_ids=["south-quarter", "not-in-tile"])) == "started"
        assert [f["field_id"] for f in json.loads(aws.sfn.started[0]["input"])["fields"]] == ["south-quarter"]

    def test_replay_with_no_matching_field_is_skipped(self, aws):
        register_field()
        assert scene_filter.process_message(envelope(field_ids=["nope"])) == "skipped:no_fields"

    def test_failed_start_releases_the_claim_so_redelivery_works(self, aws):
        register_field()
        aws.sfn.fail = True
        with pytest.raises(RuntimeError):
            scene_filter.process_message(json.dumps(stac_item()))
        assert idem_keys() == set()
        aws.sfn.fail = False
        assert scene_filter.process_message(json.dumps(stac_item())) == "started"

    def test_handler_reports_only_the_failed_messages(self, aws):
        register_field()
        good, bad = json.dumps(stac_item()), json.dumps(stac_item("S2B_T14TPM_20260719T171620_L2A", when="2026-07-19T17:22:00Z"))
        aws.sfn.fail = True
        out = scene_filter.handler({"Records": [{"messageId": "m1", "body": "garbage"}, {"messageId": "m2", "body": bad}]})
        assert out == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
        aws.sfn.fail = False
        assert scene_filter.handler({"Records": [{"messageId": "m3", "body": good}]}) == {"batchItemFailures": []}


# ---------- LoadFieldInputs ----------


class TestLoadInputs:
    def item(self, **kw):
        return {"field": {"farm_id": FARM, "field_id": FIELD}, "scene": scene_info(**kw), "replay": True, "replay_id": "r"}

    def test_builds_a_valid_request(self, aws):
        register_field(crop_current="Soybean", pivot={"center": [-96.54, 42.16], "radius_m": 800})
        out = load_inputs.handler(self.item())
        req = C.FieldAnalysisRequest.model_validate(out)
        assert req.field == C.FieldRef(farm_id=FARM, field_id=FIELD)
        assert req.crop == "soybeans" and req.as_of == "2026-07-18" and req.scene.scene_id == SCENE_ID
        assert [list(p) for p in req.polygon] == RING
        assert req.pivot == {"center": [-96.54, 42.16], "radius_m": 800.0}
        assert (req.min_clear_fraction, req.min_region_acres, req.z_threshold) == (0.7, 1.2, 2.0)

    def test_unknown_crop_and_inline_polygon(self, aws):
        register_field(geometry=False, crop_current="sugar beets", polygon=RING)
        req = C.FieldAnalysisRequest.model_validate(load_inputs.handler(self.item()))
        assert req.crop == "other" and len(req.polygon) == 4

    def test_missing_field_raises_so_the_map_records_an_error(self, aws):
        with pytest.raises(load_inputs.FieldNotFound):
            load_inputs.handler(self.item())

    def test_open_watches_are_judged_as_of_the_scene_date(self, aws):
        register_field()
        w = cm.table("watches")

        def put(watch_id, opened, expires, **kw):
            w.put_item(Item=cm.to_ddb({"field_id": FIELD, "opened_at_watch_id": f"{opened}#{watch_id}", "watch_id": watch_id,
                                       "type": "hail", "status": "open", "opened_at": opened, "expires_at": expires, **kw}))

        epoch = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())  # noqa: E731
        put("wch_open", "2026-07-10T03:00:00Z", epoch("2026-07-24T03:00:00Z"))
        put("wch_expired", "2026-06-01T00:00:00Z", epoch("2026-06-15T00:00:00Z"))
        put("wch_future", "2026-07-25T00:00:00Z", epoch("2026-08-08T00:00:00Z"))  # opened after the scene
        put("wch_closed", "2026-07-12T00:00:00Z", epoch("2026-07-26T00:00:00Z"), status="closed")
        put("wch_iso_expiry", "2026-07-15T00:00:00Z", "2026-07-29T00:00:00Z")
        req = C.FieldAnalysisRequest.model_validate(load_inputs.handler(self.item()))
        assert req.open_watch_ids == ["wch_iso_expiry", "wch_open"]


# ---------- AnalyzeField adapter ----------


class TestAnalyze:
    def test_calls_the_registry_analyzer_with_the_request_fields(self, monkeypatch):
        from fieldwatch import registry

        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return C.FieldAnalysisResult.model_validate(json.loads((SAMPLES / "field_analysis_result.json").read_text()))

        monkeypatch.setitem(registry.BY_NAME, "analyze_field", registry.ToolSpec(
            "analyze_field", "d", "read", C.FieldAnalysisRequest, fake, output_model=C.FieldAnalysisResult))
        request = json.loads((SAMPLES / "field_analysis_request.json").read_text())
        out = analyze.handler(request)
        assert seen["field"]["field_id"] == FIELD and seen["as_of"] == "2026-07-18" and seen["z_threshold"] == 2.0
        assert C.FieldAnalysisResult.model_validate(out).overlays.health.key.startswith("analysis/")  # overlays are S3 keys

    def test_stub_registry_entry_round_trips(self):
        request = json.loads((SAMPLES / "field_analysis_request.json").read_text())
        out = analyze.handler(request)
        assert C.FieldAnalysisResult.model_validate(out).status == "ok"

    def test_rejects_a_request_that_breaks_the_contract(self):
        with pytest.raises(Exception):
            analyze.handler({"field": {"farm_id": "f"}})

    def test_rejects_an_analyzer_result_that_breaks_the_contract(self, monkeypatch):
        from fieldwatch import registry

        monkeypatch.setitem(registry.BY_NAME, "analyze_field", registry.ToolSpec(
            "analyze_field", "d", "read", C.FieldAnalysisRequest, lambda **_: {"status": "ok"}))
        with pytest.raises(Exception):
            analyze.handler(json.loads((SAMPLES / "field_analysis_request.json").read_text()))


# ---------- Decide ----------


def sample_result(**changes) -> dict:
    out = json.loads((SAMPLES / "field_analysis_result.json").read_text())
    out.update(changes)
    return out


def decide_event(result=None, scene=None, replay=False, **req_changes) -> dict:
    request = json.loads((SAMPLES / "field_analysis_request.json").read_text())
    request["scene"] = scene or scene_info()
    request.update(req_changes)
    return {"request": request, "result": result or sample_result(), "scene": request["scene"], "replay": replay,
            "replay_id": "rpl_1" if replay else None}


def with_comparison(result: dict) -> dict:
    """The sample result plus the departure / change / normal overlays and their captions."""
    ov = result["overlays"]
    for kind in ("departure", "change", "normal"):
        ov[kind] = {"key": f"analysis/north-pivot/2026-07-18/s2/{kind}.png", "bounds": ov["health"]["bounds"]}
    result["comparison"] = {
        "scene_date": "2026-07-18", "previous_date": "2026-07-13",
        "baseline_dates": ["2023-07-15", "2024-07-18", "2025-07-16"], "baseline_method": "median",
        "median_departure_ndvi": -0.01,
        "departure_scale": {"stat": "z", "vmin": -3.0, "vmax": 3.0, "unit": "standard deviations"},
        "change_scale": {"stat": "ndvi_diff", "vmin": -0.25, "vmax": 0.25, "unit": "NDVI"},
    }
    return result


class TestDecisionRule:
    def ev(self, **changes):
        return decide.evaluate(C.FieldAnalysisResult.model_validate(sample_result(**changes)), 2.0, 1.2, FIELD, "2026-07-18")

    def test_passes(self):
        d = self.ev()
        assert d.emit and d.anomaly_id == "anm-north-pivot-20260718" and [r.region_id for r in d.regions] == ["r1"]

    @pytest.mark.parametrize("status", ["too_cloudy", "no_baseline", "error"])
    def test_non_ok_status_never_emits(self, status):
        assert not self.ev(status=status).emit

    @pytest.mark.parametrize("gate", ["senescence", "harvest", "regional"])
    def test_any_gate_blocks(self, gate):
        gates = {"senescence": False, "harvest": False, "regional": False, gate: True}
        d = self.ev(gates=gates)
        assert not d.emit and gate in d.reason

    def test_score_and_size_thresholds(self):
        result = sample_result()
        result["anomaly"]["regions"][0]["score"] = 1.9
        assert not decide.evaluate(C.FieldAnalysisResult.model_validate(result), 2.0, 1.2).emit
        result["anomaly"]["regions"][0].update(score=3.1, area_acres=1.1)
        assert not decide.evaluate(C.FieldAnalysisResult.model_validate(result), 2.0, 1.2).emit
        result["anomaly"]["regions"][0].update(score=2.0, area_acres=1.2)
        assert decide.evaluate(C.FieldAnalysisResult.model_validate(result), 2.0, 1.2).emit  # inclusive

    def test_only_qualifying_regions_are_emitted_strongest_first(self):
        result = sample_result()
        base = result["anomaly"]["regions"][0]
        result["anomaly"]["regions"] = [
            {**base, "region_id": "small", "area_acres": 0.5, "score": 9.0},
            {**base, "region_id": "mild", "score": 2.1},
            {**base, "region_id": "strong", "score": 4.0},
        ]
        d = decide.evaluate(C.FieldAnalysisResult.model_validate(result), 2.0, 1.2)
        assert [r.region_id for r in d.regions] == ["strong", "mild"]

    def test_no_regions_or_no_overlays(self):
        assert not self.ev(anomaly=None).emit
        assert not self.ev(overlays=None).emit


class TestDecide:
    def test_writes_the_row_and_emits_contract_valid_events(self, aws):
        out = decide.handler(decide_event(replay=True))
        assert out == {"field_id": FIELD, "status": "ok", "emit": True, "reason": out["reason"],
                       "emitted_anomaly_id": "anm-north-pivot-20260718"}
        row = cm.to_native(cm.table("scene-analyses").get_item(
            Key={"field_id": FIELD, "date_source": "2026-07-18#sentinel-2-c1-l2a"})["Item"])
        assert row["status"] == "ok" and row["max_score"] == 3.1 and row["analysis_key"].startswith("analysis/")
        assert row["replay"] is True and row["emit"] is True and row["expires_at"] > datetime.now(timezone.utc).timestamp() + 170 * 86400
        assert row["overlays"]["health"]["key"] and row["regions"][0]["region_id"] == "r1"

        (analyzed,) = aws.events.of("SceneAnalyzed")
        (anomaly,) = aws.events.of("AnomalyDetected")
        sa = C.SceneAnalyzed.model_validate(analyzed)
        assert (sa.scene_id, sa.date, sa.status, sa.max_score, sa.region_count) == (SCENE_ID, "2026-07-18", "ok", 3.1, 1)
        ad = C.AnomalyDetected.model_validate(anomaly)
        assert ad.anomaly_id == "anm-north-pivot-20260718" and ad.replay is True and ad.scene_id == SCENE_ID
        assert ad.overlays.true_color.key and ad.open_watch_ids == []
        assert {e["Source"] for e in aws.events.entries} == {"fieldwatch.scenes"}
        assert {e["EventBusName"] for e in aws.events.entries} == {"fieldwatch"}

    def test_stores_the_comparison_overlays_and_their_captions_on_the_row(self, aws):
        result = with_comparison(sample_result())
        decide.handler(decide_event(result=result))
        row = cm.to_native(cm.table("scene-analyses").get_item(
            Key={"field_id": FIELD, "date_source": "2026-07-18#sentinel-2-c1-l2a"})["Item"])
        for kind in ("departure", "change", "normal"):
            assert row["overlays"][kind]["key"] == f"analysis/north-pivot/2026-07-18/s2/{kind}.png"
            assert len(row["overlays"][kind]["bounds"]) == 4
        comp = row["comparison"]
        assert comp["scene_date"] == "2026-07-18" and comp["previous_date"] == "2026-07-13"
        assert comp["baseline_dates"] == ["2023-07-15", "2024-07-18", "2025-07-16"]
        assert comp["departure_scale"]["vmin"] == -3 and comp["change_scale"]["vmax"] == 0.25
        # the detection and the events are what they were without the extras
        assert row["emit"] is True and row["max_score"] == 3.1
        (anomaly,) = aws.events.of("AnomalyDetected")
        assert C.AnomalyDetected.model_validate(anomaly).overlays.departure.key.endswith("/departure.png")

    def test_a_result_without_a_comparison_still_stores(self, aws):
        decide.handler(decide_event())
        row = cm.to_native(cm.table("scene-analyses").get_item(
            Key={"field_id": FIELD, "date_source": "2026-07-18#sentinel-2-c1-l2a"})["Item"])
        assert row["comparison"] is None and set(row["overlays"]) >= {"true_color", "health"}

    def test_live_scene_is_not_flagged_as_replay(self, aws):
        decide.handler(decide_event(replay=False))
        assert C.AnomalyDetected.model_validate(aws.events.of("AnomalyDetected")[0]).replay is False

    def test_open_watch_ids_ride_along(self, aws):
        decide.handler(decide_event(open_watch_ids=["wch_1"]))
        assert aws.events.of("AnomalyDetected")[0]["open_watch_ids"] == ["wch_1"]

    def test_quiet_field_emits_only_scene_analyzed(self, aws):
        out = decide.handler(decide_event(result=sample_result(gates={"senescence": True, "harvest": False, "regional": False})))
        assert out["emit"] is False and "emitted_anomaly_id" not in out
        assert len(aws.events.of("SceneAnalyzed")) == 1 and aws.events.of("AnomalyDetected") == []

    def test_running_the_same_scene_twice_creates_one_anomaly(self, aws):
        first = decide.handler(decide_event(replay=True))
        second = decide.handler(decide_event(replay=True))
        assert first["emitted_anomaly_id"] and "emitted_anomaly_id" not in second
        assert len(aws.events.of("AnomalyDetected")) == 1 and len(aws.events.of("SceneAnalyzed")) == 1
        assert {"analyzed#north-pivot#" + SCENE_ID, "anomaly#north-pivot#2026-07-18"} <= idem_keys()
        assert len(cm.scan_all(cm.table("scene-analyses"))) == 1

    def test_a_second_scene_the_same_day_does_not_make_a_second_anomaly(self, aws):
        decide.handler(decide_event())
        other = scene_info(scene_id="S2A_T14TPM_20260718T171000_L2A")
        decide.handler(decide_event(scene=other, result=sample_result(clear_fraction=0.99)))
        assert len(aws.events.of("AnomalyDetected")) == 1

    def test_failed_emit_releases_the_claim_so_the_retry_emits(self, aws):
        from fieldwatch.interfaces import FieldwatchError

        aws.events.fail = True
        with pytest.raises(FieldwatchError):  # interfaces.publish_event: UPSTREAM_ERROR
            decide.handler(decide_event())
        assert idem_keys() == set()
        aws.events.fail = False
        assert decide.handler(decide_event())["emitted_anomaly_id"]
        assert len(aws.events.of("AnomalyDetected")) == 1

    def test_two_tiles_keep_the_higher_clear_fraction(self, aws):
        tile_a = scene_info(scene_id="A_T14TPM", when="2026-07-18T17:22:00Z")
        tile_b = scene_info(scene_id="B_T14TPN", tile="MGRS-14TPN", when="2026-07-18T17:21:00Z")
        decide.handler(decide_event(scene=tile_a, result=sample_result(clear_fraction=0.80)))
        better = decide.handler(decide_event(scene=tile_b, result=sample_result(clear_fraction=0.95)))
        assert better["status"] == "ok"
        worse = decide.handler(decide_event(scene=scene_info(scene_id="C_T14TPN", tile="MGRS-14TPN"),
                                            result=sample_result(clear_fraction=0.60)))
        assert worse["status"] == "superseded" and worse["emit"] is False
        row = cm.table("scene-analyses").get_item(Key={"field_id": FIELD, "date_source": "2026-07-18#sentinel-2-c1-l2a"})["Item"]
        assert row["scene_id"] == "B_T14TPN" and float(row["clear_fraction"]) == 0.95

    def test_error_recorder(self, aws):
        out = decide.handler({"error": {"Error": "States.TaskFailed", "Cause": "boom"}, "scene": scene_info(),
                              "field": {"farm_id": FARM, "field_id": FIELD}, "replay": True, "replay_id": "r"})
        assert out["status"] == "error" and out["error"]["code"] == "States.TaskFailed"
        row = cm.table("scene-analyses").get_item(Key={"field_id": FIELD, "date_source": "2026-07-18#sentinel-2-c1-l2a"})["Item"]
        assert row["status"] == "error" and row["error"]["message"] == "boom"
        sa = C.SceneAnalyzed.model_validate(aws.events.of("SceneAnalyzed")[0])
        assert sa.status == "error" and sa.max_score == 0 and aws.events.of("AnomalyDetected") == []
        assert idem_keys() == set()  # an error must not block the retry


# ---------- replay ----------


class StacStub:
    def __init__(self, features):
        self.features, self.requests = features, []

    def __call__(self, body):
        self.requests.append(body)
        if "ids" in body:
            return [f for f in self.features if f["id"] in body["ids"]]
        return list(self.features)


@pytest.fixture
def stac(monkeypatch):
    stub = StacStub([])
    monkeypatch.setattr(replay, "stac_search", stub)
    monkeypatch.setenv("REPLAY_SCENE_DELAY_S", "0")  # delayed messages stay invisible to the test's receive
    return stub


def queued_messages(aws) -> list[dict]:
    sqs = boto3.client("sqs")
    out = []
    while True:
        resp = sqs.receive_message(QueueUrl=aws.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0, VisibilityTimeout=0)
        if not resp.get("Messages"):
            return out
        out += [json.loads(m["Body"]) for m in resp["Messages"]]
        for m in resp["Messages"]:
            sqs.delete_message(QueueUrl=aws.queue_url, ReceiptHandle=m["ReceiptHandle"])


class TestReplay:
    def test_scene_id_sends_an_envelope_and_the_hail_backfill(self, aws, stac):
        stac.features = [stac_item()]
        out = replay.handler({"scene_id": SCENE_ID, "field_ids": [FIELD]})
        assert out["replay_id"].startswith("rpl_") and out["hail_events"] == 14 and out["delay_s"] == 0
        delayed = replay.handler({"scene_id": SCENE_ID, "skip_hail": False, "delay_s": 30, "replay_id": "rpl_d"})
        assert delayed["delay_s"] == 30  # the hail watches get a head start before the scene is analyzed
        attrs = boto3.client("sqs").get_queue_attributes(QueueUrl=aws.queue_url, AttributeNames=["All"])["Attributes"]
        assert attrs["ApproximateNumberOfMessagesDelayed"] == "1"
        assert out["scenes"][0]["scene_id"] == SCENE_ID

        hail = [e for e in aws.events.entries if e["DetailType"] == "MrmsFileAvailable"][:14]
        assert len(hail) == 14 and {e["Source"] for e in hail} == {"fieldwatch.replay"}
        details = [replay.MrmsFileAvailable.model_validate(json.loads(e["Detail"])) for e in hail]
        assert details[0].valid_time == "2026-07-05T17:00:00Z" and details[-1].valid_time == "2026-07-18T17:00:00Z"
        assert details[-1].key == ("CONUS/MESH_Max_1440min_00.50/20260718/"
                                   "MRMS_MESH_Max_1440min_00.50_20260718-170000.grib2.gz")
        assert all(d.bucket == "noaa-mrms-pds" and d.product == "MESH_Max_1440min_00.50" for d in details)

        (message,) = queued_messages(aws)
        assert message["replay"] is True and message["replay_id"] == out["replay_id"]
        assert message["stac_item"]["id"] == SCENE_ID and message["field_ids"] == [FIELD]

    def test_the_message_is_what_the_filter_accepts(self, aws, stac):
        register_field()
        stac.features = [stac_item()]
        replay.handler({"scene_id": SCENE_ID})
        (message,) = queued_messages(aws)
        assert scene_filter.process_message(json.dumps(message)) == "started"

    def test_skip_hail(self, aws, stac):
        stac.features = [stac_item()]
        out = replay.handler({"scene_id": SCENE_ID, "skip_hail": True})
        assert out["hail_events"] == 0 and out["delay_s"] == 0 and aws.events.entries == []

    def test_unknown_scene(self, aws, stac):
        out = replay.handler({"scene_id": "nope"})
        assert C.ErrorBody.model_validate(out).error.code == "SCENE_NOT_FOUND"
        assert queued_messages(aws) == [] and aws.events.entries == []

    @pytest.mark.parametrize("req", [{}, {"field_ids": [FIELD]}, {"as_of": "2026-07-18"}])
    def test_bad_requests(self, aws, stac, req):
        assert C.ErrorBody.model_validate(replay.handler(req)).error.code == "BAD_REQUEST"

    def test_field_ids_with_as_of_picks_the_latest_clear_registered_scene(self, aws, stac):
        register_field()
        wrong_tile = stac_item("S2A_T15TTG_20260719T171000_L2A", tile="MGRS-15TTG", when="2026-07-19T17:10:00Z")
        legacy = stac_item("S2B_14TPM_20260718_0_L2A", collection="sentinel-2-l2a")
        c1 = stac_item()
        older = stac_item("S2A_T14TPM_20260713T171000_L2A", when="2026-07-13T17:10:00Z")
        stac.features = [older, legacy, wrong_tile, c1]
        out = replay.handler({"field_ids": [FIELD], "as_of": "2026-07-19"})
        assert [s["scene_id"] for s in out["scenes"]] == [SCENE_ID]  # newest registered tile, c1 beats legacy at the same time
        body = stac.requests[0]
        assert body["datetime"].endswith("2026-07-19T23:59:59Z") and "bbox" in body
        (message,) = queued_messages(aws)
        assert message["field_ids"] == [FIELD] and message["as_of"] == "2026-07-19"

    def test_no_clear_scene_for_the_field(self, aws, stac):
        register_field()
        stac.features = [stac_item("x", tile="MGRS-15TTG")]
        assert replay.handler({"field_ids": [FIELD], "as_of": "2026-07-19"})["error"]["code"] == "NO_CLEAR_SCENE"

    def test_fields_on_the_same_scene_share_one_message(self, aws, stac):
        register_field()
        register_field("south-quarter")
        stac.features = [stac_item()]
        replay.handler({"field_ids": [FIELD, "south-quarter"], "as_of": "2026-07-18"})
        (message,) = queued_messages(aws)
        assert message["field_ids"] == [FIELD, "south-quarter"]

    def test_hail_file_times_cover_fourteen_days_ending_at_the_pass(self):
        times = replay.hail_file_times(datetime(2026, 7, 18, 17, 22, 15, tzinfo=timezone.utc))
        assert len(times) == 14 and times[-1].isoformat() == "2026-07-18T17:00:00+00:00"
        assert (times[-1] - times[0]).days == 13
        assert replay.hail_key(times[0]).endswith("/20260705/MRMS_MESH_Max_1440min_00.50_20260705-170000.grib2.gz")


# ---------- reset ----------


class TestReset:
    def seed(self):
        db = cm.ddb()
        register_field()
        register_field("other-farm-field", farm_id="other-farm", tile=None)
        db.Table("fw-scene-analyses").put_item(Item={"field_id": FIELD, "date_source": "2026-07-18#s", "replay": True})
        db.Table("fw-scene-analyses").put_item(Item={"field_id": FIELD, "date_source": "2026-07-25#s", "replay": False})
        for key, attrs in {
            f"scene#{SCENE_ID}#replay#r1": {"replay": True},
            f"analyzed#{FIELD}#{SCENE_ID}": {"replay": True, "farm_id": FARM},
            f"anomaly#{FIELD}#2026-07-18": {"replay": True, "farm_id": FARM, "anomaly_id": "anm-north-pivot-20260718"},
            "scene#live-scene": {"replay": False},
            "msg#abc": {},
            "anomaly#x#2026-07-18": {"replay": True, "farm_id": "other-farm", "anomaly_id": "anm-x-20260718"},
        }.items():
            db.Table("fw-idempotency").put_item(Item={"key": key, **attrs})
        inv = db.Table("fw-investigations")
        inv.put_item(Item={"investigation_id": "inv-1", "field_id": FIELD, "anomaly_id": "anm-north-pivot-20260718", "created_at": "t1"})
        inv.put_item(Item={"investigation_id": "inv-live", "field_id": FIELD, "anomaly_id": "anm-north-pivot-20260801", "created_at": "t2"})
        inv.put_item(Item={"investigation_id": "inv-flagged", "field_id": FIELD, "anomaly_id": "anm-other", "replay": True, "created_at": "t3"})
        inv.put_item(Item={"investigation_id": "inv-other", "field_id": "other-farm-field", "anomaly_id": "anm-x-20260718", "created_at": "t4"})
        db.Table("fw-approvals").put_item(Item={"approval_id": "apr-1", "investigation_id": "inv-1"})
        db.Table("fw-approvals").put_item(Item={"approval_id": "apr-live", "investigation_id": "inv-live"})
        db.Table("fw-actions").put_item(Item={"action_id": "act-1", "investigation_id": "inv-1"})
        db.Table("fw-actions").put_item(Item={"action_id": "act-live", "investigation_id": "inv-live"})

    def test_deletes_only_replay_state_for_the_demo_farm(self, aws):
        self.seed()
        s3 = boto3.client("s3", region_name="us-west-2")
        for inv_id in ("inv-1", "inv-live"):  # a paused session would make the next replay's start fail
            s3.put_object(Bucket="fw-test-bucket", Key=f"sessions/session_fieldwatch-{inv_id}/session.json", Body=b"{}")
        out = reset.handler({"farm_id": FARM})
        assert out["farm_id"] == FARM
        assert out["deleted"] == {"fw-scene-analyses": 1, "fw-investigations": 2, "agent-sessions": 1,
                                  "fw-approvals": 1, "fw-actions": 1, "fw-idempotency": 3}
        left = [o["Key"] for o in s3.list_objects_v2(Bucket="fw-test-bucket", Prefix="sessions/").get("Contents", [])]
        assert left == ["sessions/session_fieldwatch-inv-live/session.json"]  # live sessions are kept
        db = cm.ddb()
        ids = lambda name, k: {r[k] for r in cm.scan_all(db.Table(name))}  # noqa: E731
        assert ids("fw-investigations", "investigation_id") == {"inv-live", "inv-other"}
        assert ids("fw-approvals", "approval_id") == {"apr-live"} and ids("fw-actions", "action_id") == {"act-live"}
        assert ids("fw-scene-analyses", "date_source") == {"2026-07-25#s"}
        assert ids("fw-idempotency", "key") == {"scene#live-scene", "msg#abc", "anomaly#x#2026-07-18"}
        assert len(cm.scan_all(db.Table("fw-fields"))) == 2  # fields are never touched

    def test_default_farm_and_idempotent(self, aws, monkeypatch):
        monkeypatch.setenv("DEMO_FARM_ID", FARM)
        self.seed()
        reset.handler({})
        again = reset.handler({})
        assert all(n == 0 for n in again["deleted"].values())

    def test_replay_again_after_reset_runs_the_whole_chain(self, aws, stac):
        register_field()
        stac.features = [stac_item()]
        for expected_anomalies in (1, 2):
            replay.handler({"scene_id": SCENE_ID, "skip_hail": True})
            for message in queued_messages(aws):
                assert scene_filter.process_message(json.dumps(message)) == "started"
            payload = json.loads(aws.sfn.started[-1]["input"])
            request = load_inputs.handler({"field": payload["fields"][0], **{k: payload[k] for k in ("scene", "replay", "replay_id")}})
            result = analyze.handler(request)
            decide.handler({"request": request, "result": result, "scene": payload["scene"], "replay": True, "replay_id": payload["replay_id"]})
            assert len(aws.events.of("AnomalyDetected")) == expected_anomalies
            reset.handler({"farm_id": FARM})
        assert len(aws.events.of("AnomalyDetected")) == 2  # one per replay, because a reset ran in between


# ---------- check latest ----------


class TestCheckLatest:
    def test_enqueues_recent_scenes_for_each_registered_tile(self, aws, monkeypatch):
        register_field(tile="14TPM")
        register_field("f2", tile="MGRS-14TNM")
        register_field("f3", tile="14TPM")
        calls = []

        def fake(body):
            calls.append(body)
            code = body["query"]["grid:code"]["eq"]
            return [stac_item(f"scene-{code}", tile=code)]

        monkeypatch.setattr(check_latest, "stac_search", fake)
        out = check_latest.handler({})
        assert out["tiles"] == ["14TPM", "MGRS-14TNM"] and out["scenes_queued"] == 2
        assert sorted(c["query"]["grid:code"]["eq"] for c in calls) == ["MGRS-14TNM", "MGRS-14TPM"]
        assert calls[0]["collections"] == ["sentinel-2-c1-l2a"] and calls[0]["query"]["eo:cloud_cover"] == {"lt": 40.0}
        messages = queued_messages(aws)
        assert {m["id"] for m in messages} == {"scene-MGRS-14TPM", "scene-MGRS-14TNM"}
        assert all(m["type"] == "Feature" and "replay" not in m for m in messages)  # plain items, not replay envelopes

    def test_check_now_request_limits_to_the_fields_tiles(self, aws, monkeypatch):
        register_field(tile="14TPM")
        register_field("f2", tile="MGRS-14TNM")
        monkeypatch.setattr(check_latest, "stac_search", lambda body: [])
        out = check_latest.handler({"schema_version": "1", "farm_id": "x", "field_ids": ["f2"], "reason": "check_now"})
        assert out["tiles"] == ["MGRS-14TNM"]

    def test_no_tiles_no_calls(self, aws, monkeypatch):
        monkeypatch.setattr(check_latest, "stac_search", lambda b: pytest.fail("should not search"))
        assert check_latest.handler({}) == {"tiles": [], "scenes_queued": 0}

    @pytest.mark.parametrize("tile,code", [("14TPM", "MGRS-14TPM"), ("T14TPM", "MGRS-14TPM"), ("MGRS-14TPM", "MGRS-14TPM")])
    def test_grid_code(self, tile, code):
        assert check_latest.grid_code(tile) == code


# ---------- the whole per-scene chain, as the state machine runs it ----------


def run_scene(aws, body: str) -> list[dict]:
    """Filter -> (Map: load -> analyze -> decide per field), the way the Express workflow chains them."""
    assert scene_filter.process_message(body) == "started"
    payload = json.loads(aws.sfn.started[-1]["input"])
    results = []
    for ref in payload["fields"]:
        item = {"field": ref, "scene": payload["scene"], "replay": payload["replay"], "replay_id": payload["replay_id"]}
        try:
            request = load_inputs.handler(item)
            result = analyze.handler(request)
            results.append(decide.handler({"request": request, "result": result, **{k: item[k] for k in ("scene", "replay", "replay_id")}}))
        except Exception as exc:  # the state machine's Catch
            results.append(decide.handler({**item, "error": {"Error": type(exc).__name__, "Cause": str(exc)}}))
    return results


def test_end_to_end_with_the_stub_analyzer_and_one_bad_field(aws):
    register_field()
    register_field("broken", geometry=False)  # no polygon: LoadFieldInputs raises, only this field errors
    results = {r["field_id"]: r for r in run_scene(aws, envelope(as_of="2026-07-18"))}
    assert results[FIELD]["emitted_anomaly_id"] == "anm-north-pivot-20260718"
    assert results["broken"]["status"] == "error"
    statuses = {e.field.field_id: e.status for e in map(C.SceneAnalyzed.model_validate, aws.events.of("SceneAnalyzed"))}
    assert statuses == {FIELD: "ok", "broken": "error"}
    assert len(aws.events.of("AnomalyDetected")) == 1
    # the same replay envelope again (SQS redelivery): stopped at the filter
    assert scene_filter.process_message(envelope(as_of="2026-07-18")) == "skipped:duplicate"
    # a second replay of the same scene without a reset: runs, but cannot create a second anomaly
    again = {r["field_id"]: r for r in run_scene(aws, envelope(replay_id="rpl_2"))}
    assert "emitted_anomaly_id" not in again[FIELD] and len(aws.events.of("AnomalyDetected")) == 1


# ---------- the CDK stack ----------


@pytest.fixture(scope="module")
def template():
    pytest.importorskip("aws_cdk")
    import aws_cdk as cdk
    from aws_cdk import assertions

    sys.path.insert(0, str(REPO / "infra"))
    from stacks.data_plane import DataPlane
    from stacks.scenes import Scenes

    app = cdk.App(context={"aws:cdk:bundling-stacks": []})  # skip pip bundling; the shape is what we check
    env = cdk.Environment(account="123456789012", region="us-west-2")
    data = DataPlane(app, "FieldWatch-DataPlane", env=env)
    stack = Scenes(app, "FieldWatch-Scenes", data=data, env=env)
    return assertions.Template.from_stack(stack)


def _machine(template) -> dict:
    (resource,) = template.find_resources("AWS::StepFunctions::StateMachine").values()
    parts = resource["Properties"]["DefinitionString"]["Fn::Join"][1]
    return {"props": resource["Properties"],
            "definition": json.loads("".join(p if isinstance(p, str) else "TOKEN" for p in parts))}


class TestStack:
    def test_express_state_machine_shape(self, template):
        m = _machine(template)
        assert m["props"]["StateMachineType"] == "EXPRESS" and m["props"]["StateMachineName"] == "fw-scene-analysis"
        assert m["props"]["LoggingConfiguration"]["Level"] == "ERROR"
        states = m["definition"]["States"]
        assert m["definition"]["StartAt"] == "ForEachField" and states["ForEachField"]["Next"] == "Summarize"
        each = states["ForEachField"]
        assert each["Type"] == "Map" and each["MaxConcurrency"] == 10 and each["ItemsPath"] == "$.fields"
        assert each["ItemSelector"]["field.$"] == "$$.Map.Item.Value"
        inner = each["ItemProcessor"]["States"]
        assert each["ItemProcessor"]["StartAt"] == "LoadFieldInputs"
        assert inner["LoadFieldInputs"]["Next"] == "AnalyzeField" and inner["AnalyzeField"]["Next"] == "Decide"
        assert inner["Decide"]["End"] is True

    def test_every_step_is_caught_so_one_bad_field_never_fails_the_scene(self, template):
        inner = _machine(template)["definition"]["States"]["ForEachField"]["ItemProcessor"]["States"]
        for step in ("LoadFieldInputs", "AnalyzeField", "Decide"):
            (catch,) = inner[step]["Catch"]
            assert catch["ErrorEquals"] == ["States.ALL"] and catch["Next"] == "RecordError"
        assert inner["RecordError"]["Catch"][0]["Next"] == "FieldFailed"
        assert inner["FieldFailed"]["Parameters"]["status"] == "error"
        assert inner["AnalyzeField"]["TimeoutSeconds"] == 120

    def test_lambda_steps_pass_the_right_payloads(self, template):
        inner = _machine(template)["definition"]["States"]["ForEachField"]["ItemProcessor"]["States"]
        assert inner["LoadFieldInputs"]["ResultPath"] == "$.request" and "Parameters" not in inner["LoadFieldInputs"]
        assert inner["AnalyzeField"]["InputPath"] == "$.request" and inner["AnalyzeField"]["ResultPath"] == "$.result"
        assert set(inner["Decide"]["Parameters"]) == {"request.$", "result.$", "scene.$", "replay.$", "replay_id.$"}
        assert "TooManyRequestsException" in json.dumps(inner["AnalyzeField"]["Retry"])

    def test_queue_and_dead_letter_queue(self, template):
        queues = {r["Properties"]["QueueName"]: r["Properties"] for r in template.find_resources("AWS::SQS::Queue").values()}
        assert set(queues) == {"fw-scene-queue", "fw-scene-dlq"}
        assert queues["fw-scene-queue"]["RedrivePolicy"]["maxReceiveCount"] == 5
        assert queues["fw-scene-queue"]["VisibilityTimeout"] >= 6 * 30
        (mapping,) = template.find_resources("AWS::Lambda::EventSourceMapping").values()
        assert mapping["Properties"]["BatchSize"] == 10
        assert mapping["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]

    def test_lambdas(self, template):
        fns = {r["Properties"]["FunctionName"]: r["Properties"] for r in template.find_resources("AWS::Lambda::Function").values()}
        assert set(fns) == {"fw-scene-filter", "fw-scene-load-inputs", "fw-scene-analyze-field", "fw-scene-decide",
                            "fw-scene-replay", "fw-scene-reset", "fw-scene-check-latest"}
        assert all(f["Architectures"] == ["x86_64"] for f in fns.values())
        analyze_fn = fns["fw-scene-analyze-field"]
        assert analyze_fn["PackageType"] == "Image" and analyze_fn["MemorySize"] == 3008 and analyze_fn["Timeout"] == 90
        assert analyze_fn["ImageConfig"]["Command"] == ["cloud.lambdas.scenes.analyze.handler"]
        assert fns["fw-scene-filter"]["Handler"] == "cloud.lambdas.scenes.scene_filter.handler"
        assert fns["fw-scene-filter"]["Timeout"] == 30 and fns["fw-scene-filter"]["MemorySize"] == 512

    def test_daily_schedule(self, template):
        (schedule,) = template.find_resources("AWS::Scheduler::Schedule").values()
        assert schedule["Properties"]["ScheduleExpression"].startswith("cron(") and schedule["Properties"]["State"] == "ENABLED"

    def test_outputs_for_the_api_lane(self, template):
        outputs = template.to_json()["Outputs"]
        assert {"SceneQueueUrl", "SceneQueueArn", "ReplayFunctionName", "ResetFunctionName", "StateMachineArn"} <= set(outputs)
        assert outputs["StateMachineArn"]["Export"]["Name"] == "FieldWatch-Scenes-StateMachineArn"

    def test_no_other_project_names_and_no_live_subscription_by_default(self, template):
        text = json.dumps(template.to_json())
        assert "preflight" not in text and '"pr-' not in text
        assert template.find_resources("AWS::SNS::Subscription") == {}
        assert "us-east-1" not in text
