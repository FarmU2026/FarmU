"""FieldWatch-Api: API Gateway HTTP API + one Lambda per route group (WS12, plan T27).

Route groups and their routes come from agent/cloud/api/routes.py (one source of truth, unit-tested
against the handlers' routers). State lives in the shared DataPlane (fw- tables, bucket, bus).

Optional hand-off targets owned by other lanes. Each is a constructor argument (app.py can wire a
stack's resource in later) or a CDK context value; unset means the API skips that hand-off:
    -c sceneQueueArn=arn:aws:sqs:us-west-2:<acct>:fw-scene-queue        (WS2 scene pipeline)
    -c contextStateMachineArn=arn:aws:states:us-west-2:<acct>:stateMachine:<name>   (WS15)
    -c replayFunctionName=<fn>   -c resetFunctionName=<fn>              (WS2 replay / reset Lambdas)
    -c sendFunctionName=<fn>                                           (WS9 notify send Lambda)
    -c smsProvider=twilio  -c smsFrom=+1...                             (shown by GET /api/status)
    -c twilioWebhookUrl=https://...                                     (only behind a custom domain)
SSM parameters read at runtime (create by hand; CloudFormation can't create SecureStrings):
    /fieldwatch/link-signing-key, /fieldwatch/dashboard-passcode, /fieldwatch/twilio-auth-token
"""
from __future__ import annotations

import importlib.util

from aws_cdk import ArnFormat, CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_iam as iam
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from stacks.common import AGENT, python_lambda

BOUNDARY_NAME = "fieldwatch-boundary"


def _load_routes() -> tuple[dict[str, list[str]], dict[str, str]]:
    spec = importlib.util.spec_from_file_location("fieldwatch_api_routes", AGENT / "cloud" / "api" / "routes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ROUTES, mod.HANDLERS


def _queue_url(arn: str) -> str:
    _, _, _, region, account, name = arn.split(":", 5)
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


class Api(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, scene_queue_arn: str | None = None,
                 context_state_machine_arn: str | None = None, replay_function_name: str | None = None,
                 reset_function_name: str | None = None, send_function_name: str | None = None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")
        iam.PermissionsBoundary.of(self).apply(
            iam.ManagedPolicy.from_managed_policy_name(self, "Boundary", BOUNDARY_NAME))

        def opt(value: str | None, key: str) -> str:
            return (value or self.node.try_get_context(key) or "").strip()

        queue_arn = opt(scene_queue_arn, "sceneQueueArn")
        sm_arn = opt(context_state_machine_arn, "contextStateMachineArn")
        fns = {
            "FIELDWATCH_REPLAY_FN": opt(replay_function_name, "replayFunctionName"),
            "FIELDWATCH_RESET_FN": opt(reset_function_name, "resetFunctionName"),
            "FIELDWATCH_SEND_FN": opt(send_function_name, "sendFunctionName"),
        }

        env = {
            "FIELDWATCH_BUCKET": data.bucket.bucket_name,
            "FIELDWATCH_EVENT_BUS": data.bus.event_bus_name,
            "FIELDWATCH_TABLE_PREFIX": "fw-",
            "FIELDWATCH_LINK_KEY_PARAM": "/fieldwatch/link-signing-key",
            "FIELDWATCH_SCENE_QUEUE_URL": _queue_url(queue_arn) if queue_arn else "",
            "FIELDWATCH_CONTEXT_SM_ARN": sm_arn,
            "FIELDWATCH_SMS_PROVIDER": opt(None, "smsProvider") or "none",
            "FIELDWATCH_SMS_FROM": opt(None, "smsFrom"),
            "FIELDWATCH_TWILIO_WEBHOOK_URL": opt(None, "twilioWebhookUrl"),
            **fns,
        }
        env = {k: v for k, v in env.items() if v}

        self.http_api = apigw.HttpApi(
            self, "HttpApi",
            api_name="fieldwatch-api",
            description="FieldWatch web API (WS12)",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],  # TODO: the CloudFront domain once WS14 lands
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST, apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["content-type", "authorization"],
                max_age=Duration.hours(1),
            ),
        )

        t = data.tables
        routes, handlers = _load_routes()
        self.functions = {}
        for group, keys in routes.items():
            cid = "".join(p.capitalize() for p in group.split("_")) + "Fn"
            fn = python_lambda(self, cid, handler=handlers[group], environment=env,
                               timeout_s=15 if group == "sms_inbound" else 30, memory_mb=512)
            self.functions[group] = fn
            integration = HttpLambdaIntegration(f"{cid}Integration", fn)
            for key in keys:
                method, path = key.split(" ", 1)
                self.http_api.add_routes(path=path, methods=[apigw.HttpMethod[method]], integration=integration)

            # --- least privilege per group ---
            data.bus.grant_put_events_to(fn)
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[self.format_arn(service="ssm", resource="parameter", resource_name="fieldwatch/*")]))
            for name in ("fw-fields", "fw-scene-analyses", "fw-investigations"):
                t[name].grant_read_data(fn)
            # Reply hand-off (fallback or WS9's notify.inbound) and WS8's approvals run in-process here.
            for name in ("fw-farms", "fw-approvals", "fw-messages", "fw-idempotency"):
                t[name].grant_read_write_data(fn)
            data.bucket.grant_read(fn, "analysis/*")
            if group == "farms":
                t["fw-fields"].grant_read_write_data(fn)
                data.bucket.grant_put(fn, "fields/*")
            if group == "pages":
                t["fw-actions"].grant_read_write_data(fn)
                t["fw-uploads"].grant_read_write_data(fn)
                data.bucket.grant_put(fn, "uploads/*")
            if queue_arn and group in ("farms", "dashboard"):
                fn.add_to_role_policy(iam.PolicyStatement(actions=["sqs:SendMessage"], resources=[queue_arn]))
            if sm_arn and group == "farms":
                fn.add_to_role_policy(iam.PolicyStatement(actions=["states:StartExecution"], resources=[sm_arn]))
            invoke = [fns["FIELDWATCH_SEND_FN"]]
            if group == "dashboard":
                invoke += [fns["FIELDWATCH_REPLAY_FN"], fns["FIELDWATCH_RESET_FN"]]
            arns = [f if f.startswith("arn:") else self.format_arn(service="lambda", resource="function",
                                                                  resource_name=f, arn_format=ArnFormat.COLON_RESOURCE_NAME)
                    for f in invoke if f]
            if arns:
                fn.add_to_role_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=arns))

        CfnOutput(self, "ApiUrl", value=self.http_api.api_endpoint, description="Base URL of the FieldWatch web API")
        CfnOutput(self, "SmsWebhookUrl", value=f"{self.http_api.api_endpoint}/sms/inbound",
                  description="Twilio 'A message comes in' webhook (HTTP POST)")

