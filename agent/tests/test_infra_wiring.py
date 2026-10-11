"""The whole CDK app wires stacks by object: the API gets the other lanes' Lambdas, config.js gets real values,
and FieldWatch-Monitoring alarms on every DLQ (no bundling, no AWS)."""
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

aws_cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402

INFRA = Path(__file__).resolve().parents[2] / "infra"


@pytest.fixture(scope="module")
def app():
    sys.path.insert(0, str(INFRA))
    real_app, real_synth = aws_cdk.App, aws_cdk.App.synth
    holder = {}

    def make_app(*a, **kw):  # app.py builds cdk.App(); give it no-bundling context and keep a handle
        kw.setdefault("context", {})["aws:cdk:bundling-stacks"] = []
        holder["app"] = real_app(*a, **kw)
        return holder["app"]

    os.environ.setdefault("CDK_DEFAULT_ACCOUNT", "123456789012")
    try:
        aws_cdk.App = make_app
        aws_cdk.App.synth = lambda self, *a, **k: None  # noqa: ARG005
        runpy.run_path(str(INFRA / "app.py"), run_name="__main__")
    finally:
        aws_cdk.App = real_app
        real_app.synth = real_synth
        sys.path.remove(str(INFRA))
    return holder["app"]


def _template(app, name):
    return assertions.Template.from_stack(app.node.find_child(name))


def test_api_gets_the_other_lanes_functions(app):
    t = _template(app, "FieldWatch-Api")
    envs = [f["Properties"]["Environment"]["Variables"] for f in t.find_resources("AWS::Lambda::Function").values()]
    names = set().union(*envs)
    for var in ("FIELDWATCH_CHECK_FN", "FIELDWATCH_REPLAY_FN", "FIELDWATCH_RESET_FN", "FIELDWATCH_SEND_FN",
                "FIELDWATCH_MIRROR_FN", "FIELDWATCH_NOTIFY_REPLY_FN", "FIELDWATCH_CONTEXT_SM_ARN"):
        assert var in names, var
    assert "FIELDWATCH_SCENE_QUEUE_URL" not in names  # the API never writes to the scene queue


def test_frontend_config_comes_from_the_api_and_feed_stacks(app):
    t = json.dumps(_template(app, "FieldWatch-Frontend").to_json())
    assert "Fn::ImportValue" in t and "FieldWatch-Api" in t and "FieldWatch-Feed" in t


def test_monitoring_alarms_on_every_dlq_and_publishes_to_an_unsubscribed_topic(app):
    t = _template(app, "FieldWatch-Monitoring")
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    dlq = [a for a in alarms.values() if a["Properties"]["MetricName"] == "ApproximateNumberOfMessagesVisible"]
    assert len(dlq) == 5  # scenes, session starter, notifications, projector, hail
    assert any(a["Properties"]["MetricName"] == "Errors" for a in alarms.values())
    assert any(a["Properties"]["MetricName"] == "ExecutionsFailed" for a in alarms.values())
    t.resource_count_is("AWS::SNS::Topic", 1)
    t.resource_count_is("AWS::SNS::Subscription", 0)
    t.resource_count_is("AWS::CloudWatch::Dashboard", 1)


def test_api_stage_is_throttled_with_tighter_limits_on_costly_routes(app):
    t = _template(app, "FieldWatch-Api")
    (stage,) = t.find_resources("AWS::ApiGatewayV2::Stage").values()
    props = stage["Properties"]
    assert props["DefaultRouteSettings"] == {"ThrottlingRateLimit": 25, "ThrottlingBurstLimit": 50}
    rs = props["RouteSettings"]
    assert rs["POST /v1/replay"] == {"ThrottlingRateLimit": 1, "ThrottlingBurstLimit": 2}
    assert rs["POST /v1/replay/reset"] == {"ThrottlingRateLimit": 1, "ThrottlingBurstLimit": 2}
    for key in ("POST /api/farms", "POST /api/farms/{farm_id}/check",
                "POST /api/farms/{farm_id}/fields/{field_id}/ask"):
        assert rs[key] == {"ThrottlingRateLimit": 2, "ThrottlingBurstLimit": 5}, key
    # every throttled key is a real route of the API (an unknown key would fail the deploy)
    routes = {r["Properties"]["RouteKey"] for r in t.find_resources("AWS::ApiGatewayV2::Route").values()}
    assert set(rs) <= routes
    # ...and exists before the stage updates: otherwise adding a throttled route fails the deploy and its rollback
    assert set(t.find_resources("AWS::ApiGatewayV2::Route")) <= set(stage.get("DependsOn", []))


def _reserved(app, stack):
    """{function logical-id prefix or FunctionName: ReservedConcurrentExecutions or None} for a stack's Lambdas."""
    out = {}
    for lid, f in _template(app, stack).find_resources("AWS::Lambda::Function").items():
        p = f["Properties"]
        out[p.get("FunctionName", lid)] = p.get("ReservedConcurrentExecutions")
    return out


def test_cost_heavy_lambdas_have_reserved_concurrency(app):
    api = _reserved(app, "FieldWatch-Api")
    assert api and set(api.values()) == {10}  # every route-group Lambda
    assert set(_reserved(app, "FieldWatch-Agent").values()) == {5}  # the session starter
    scenes = _reserved(app, "FieldWatch-Scenes")
    assert scenes["fw-scene-analyze-field"] == 10
    notify = _reserved(app, "FieldWatch-Notifications")
    assert notify["fw-notify-send"] == 5
    assert notify["fw-notify-reply"] is None and notify["fw-notify-mirror"] is None


def test_total_reserved_concurrency_stays_well_under_the_account_limit(app):
    total = sum(v or 0 for s in app.node.children if hasattr(s, "template_options")
                for v in _reserved(app, s.node.id).values())
    assert 0 < total < 100


def test_monitoring_alarms_on_the_hazards_and_notification_lambdas(app):
    """Kurt's functions: hail filter + drought poller (hazards), send + reply + mirror (notifications)."""
    t = _template(app, "FieldWatch-Monitoring")
    alarms = t.find_resources("AWS::CloudWatch::Alarm")
    errors = [lid for lid, a in alarms.items() if a["Properties"]["MetricName"] == "Errors"]
    for name in ("HailFilter", "DroughtPoller", "NotifySend", "NotifyReply", "NotifyMirror"):
        (lid,) = [e for e in errors if e.startswith(name + "Errors")]
        props = alarms[lid]["Properties"]
        assert props["Threshold"] == 0 and props["Period"] == 300 and props["AlarmActions"], name
    assert any(lid.startswith("HailDlqNotEmpty") for lid in alarms)
