"""FieldWatch-Api: API Gateway HTTP API + one Lambda per route group (WS12, plan T27).

Route groups and their routes come from agent/cloud/api/routes.py (one source of truth, unit-tested
against the handlers' routers). State lives in the shared DataPlane (fw- tables, bucket, bus).

Hand-off targets owned by other lanes come from the stack objects app.py passes in
(`Api(..., scenes=scenes, context=context, notifications=notifications)`); the env var names are
fieldwatch.interfaces.Env. Each can also be a CDK context value (overrides the stack); unset means the API
skips that hand-off:
    scenes.check_fn / -c checkFunctionName=<fn>      FIELDWATCH_CHECK_FN   ("check now", sign-up)
    scenes.replay_fn / -c replayFunctionName=<fn>    FIELDWATCH_REPLAY_FN  (POST /v1/replay)
    scenes.reset_fn / -c resetFunctionName=<fn>      FIELDWATCH_RESET_FN   (POST /v1/replay/reset)
    context.state_machine / -c contextStateMachineArn=<arn>   FIELDWATCH_CONTEXT_SM_ARN (WS15)
    notifications.send / -c sendFunctionName=<fn>    FIELDWATCH_SEND_FN    (WS9 send)
    notifications.mirror / -c mirrorFunctionName=<fn> FIELDWATCH_MIRROR_FN (phone panel / mirror reply)
    notifications.reply / -c notifyReplyFunctionName=<fn> FIELDWATCH_NOTIFY_REPLY_FN (POST /sms/inbound is
                                                     forwarded to WS9's webhook handler)
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

# Stage throttling (requests/second, burst), a cost guardrail: every unthrottled route is a way to burn Lambda,
# Bedrock and Step Functions money. Route keys must match agent/cloud/api/routes.py exactly (a setting for an
# unknown key fails the deploy). The legacy /api/... routes stay unauthenticated (the ported UI calls them without
# a token), so rate limits are their only guard.
DEFAULT_THROTTLE = (25, 50)
ROUTE_THROTTLES: dict[str, tuple[int, int]] = {
    "POST /v1/replay": (1, 2),
    "POST /v1/replay/reset": (1, 2),
    "POST /api/farms": (2, 5),
    "POST /api/farms/{farm_id}/check": (2, 5),
    "POST /api/farms/{farm_id}/fields/{field_id}/ask": (2, 5),
}


def _load_routes() -> tuple[dict[str, list[str]], dict[str, str]]:
    spec = importlib.util.spec_from_file_location("fieldwatch_api_routes", AGENT / "cloud" / "api" / "routes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ROUTES, mod.HANDLERS


class Api(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, scenes=None, context=None, notifications=None,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")
        iam.PermissionsBoundary.of(self).apply(
            iam.ManagedPolicy.from_managed_policy_name(self, "Boundary", BOUNDARY_NAME))

        def opt(key: str) -> str:
            return str(self.node.try_get_context(key) or "").strip()

        def target(obj, attr: str, key: str):
            """(function name for the env, the Function to grant invoke on or None) from context or a stack."""
            name = opt(key)
            if name:
                return name, None
            fn = getattr(obj, attr, None) if obj is not None else None
            return (fn.function_name, fn) if fn is not None else ("", None)

        sm = getattr(context, "state_machine", None)
        sm_arn = opt("contextStateMachineArn") or (sm.state_machine_arn if sm is not None else "")
        # env var -> (function name, Function or None), and which route groups may invoke it
        targets = {
            "FIELDWATCH_CHECK_FN": (*target(scenes, "check_fn", "checkFunctionName"), ("farms",)),
            "FIELDWATCH_REPLAY_FN": (*target(scenes, "replay_fn", "replayFunctionName"), ("dashboard",)),
            "FIELDWATCH_RESET_FN": (*target(scenes, "reset_fn", "resetFunctionName"), ("dashboard",)),
            "FIELDWATCH_SEND_FN": (*target(notifications, "send", "sendFunctionName"), ("farms", "pages", "dashboard")),
            "FIELDWATCH_MIRROR_FN": (*target(notifications, "mirror", "mirrorFunctionName"), ("farms", "dashboard")),
            "FIELDWATCH_NOTIFY_REPLY_FN": (*target(notifications, "reply", "notifyReplyFunctionName"),
                                           ("sms_inbound",)),
        }
        fns = {k: v[0] for k, v in targets.items()}

        env = {
            "FIELDWATCH_BUCKET": data.bucket.bucket_name,
            "FIELDWATCH_EVENT_BUS": data.bus.event_bus_name,
            "FIELDWATCH_TABLE_PREFIX": "fw-",
            "FIELDWATCH_APPROVALS_BACKEND": "dynamo",  # fieldwatch/approvals.py: fw-approvals, not the local JSON file
            "FIELDWATCH_LINK_KEY_PARAM": "/fieldwatch/link-signing-key",
            "FIELDWATCH_CONTEXT_SM_ARN": sm_arn,
            "FIELDWATCH_SMS_PROVIDER": opt("smsProvider") or "none",
            "FIELDWATCH_SMS_FROM": opt("smsFrom"),
            "FIELDWATCH_TWILIO_WEBHOOK_URL": opt("twilioWebhookUrl"),
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
                               timeout_s=15 if group == "sms_inbound" else 30, memory_mb=512,
                               reserved_concurrency=10)
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
            if sm_arn and group == "farms":
                fn.add_to_role_policy(iam.PolicyStatement(actions=["states:StartExecution"], resources=[sm_arn]))
            arns = []
            for name, target_fn, groups in targets.values():
                if not name or group not in groups:
                    continue
                if target_fn is not None:
                    target_fn.grant_invoke(fn)
                else:
                    arns.append(name if name.startswith("arn:") else self.format_arn(
                        service="lambda", resource="function", resource_name=name,
                        arn_format=ArnFormat.COLON_RESOURCE_NAME))
            if arns:
                fn.add_to_role_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=arns))

        known = {k for keys in routes.values() for k in keys}
        assert set(ROUTE_THROTTLES) <= known, f"throttle for unknown routes: {set(ROUTE_THROTTLES) - known}"
        stage = self.http_api.default_stage.node.default_child  # L1 CfnStage: the L2 has no throttle settings
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_rate_limit=DEFAULT_THROTTLE[0], throttling_burst_limit=DEFAULT_THROTTLE[1])
        # route_settings is an untyped map in the L1, so values are raw CloudFormation (PascalCase) dicts
        stage.route_settings = {
            key: {"ThrottlingRateLimit": rate, "ThrottlingBurstLimit": burst}
            for key, (rate, burst) in ROUTE_THROTTLES.items()}

        CfnOutput(self, "ApiUrl", value=self.http_api.api_endpoint, description="Base URL of the FieldWatch web API")
        CfnOutput(self, "SmsWebhookUrl", value=f"{self.http_api.api_endpoint}/sms/inbound",
                  description="Twilio 'A message comes in' webhook (HTTP POST)")

