"""WS7: the FieldWatch-Agent stack synthesizes with the right rules, retries and permissions (no bundling, no AWS)."""
import json
import sys
from pathlib import Path

import pytest

aws_cdk = pytest.importorskip("aws_cdk")
from aws_cdk import assertions  # noqa: E402

INFRA = Path(__file__).resolve().parents[2] / "infra"


@pytest.fixture(scope="module")
def template():
    sys.path.insert(0, str(INFRA))
    try:
        from stacks.agent import Agent
        from stacks.data_plane import DataPlane

        app = aws_cdk.App(context={"aws:cdk:bundling-stacks": []})  # skip pip bundling of the Lambda asset
        env = aws_cdk.Environment(account="123456789012", region="us-west-2")
        data = DataPlane(app, "FieldWatch-DataPlane", env=env)
        stack = Agent(app, "FieldWatch-Agent", data=data, env=env)
        yield assertions.Template.from_stack(stack)
    finally:
        sys.path.remove(str(INFRA))


def test_one_rule_per_event_on_the_fieldwatch_bus(template):
    rules = template.find_resources("AWS::Events::Rule")
    patterns = [r["Properties"]["EventPattern"] for r in rules.values()]
    types = sorted(t for p in patterns for t in p["detail-type"])
    assert types == sorted(["AnomalyDetected", "StartInvestigation", "ApprovalRecorded", "ReplyReceived",
                            "PhotoReceived", "DocumentReceived", "ActionCompleted"])
    start = next(p for p in patterns if p["detail-type"] == ["StartInvestigation"])
    assert start["source"] == ["fieldwatch.api"]


def test_rule_targets_retry_and_dead_letter(template):
    for r in template.find_resources("AWS::Events::Rule").values():
        (t,) = r["Properties"]["Targets"]
        assert t["RetryPolicy"]["MaximumRetryAttempts"] == 3 and "DeadLetterConfig" in t


def test_starter_lambda_is_x86_python312_with_dlq_and_env(template):
    template.has_resource_properties("AWS::Lambda::Function", {
        "Runtime": "python3.12", "Architectures": ["x86_64"], "Timeout": 60,
        "Handler": "cloud.lambdas.session_starter.handler.handler",
        "DeadLetterConfig": assertions.Match.object_like({}),
        "Environment": {"Variables": assertions.Match.object_like({
            "FIELDWATCH_RUNTIME_ARN_PARAM": "/fieldwatch/runtime-arn",
            "FIELDWATCH_INVESTIGATIONS_TABLE": "fw-investigations"})},
    })


def test_starter_can_invoke_runtime_and_read_only_its_ssm_param(template):
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    assert "bedrock-agentcore:InvokeAgentRuntime" in policies
    assert "parameter/fieldwatch/runtime-arn" in policies
    assert "link-signing-key" not in policies


def test_sessions_bucket_parameter_and_tags(template):
    template.has_resource_properties("AWS::SSM::Parameter", {"Name": "/fieldwatch/sessions-bucket", "Type": "String"})
    template.has_resource_properties("AWS::SQS::Queue", {"Tags": [{"Key": "project", "Value": "fieldwatch"}]})
