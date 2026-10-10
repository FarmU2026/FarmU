"""Live feed (WS13, Build Plan T28): AppSync Events API + the projector Lambda.

- AppSync Events API, namespace `fieldwatch`. Channels (full paths):
      /fieldwatch/farms/{farm_id}/feed    dashboard feed
      /fieldwatch/farms/{farm_id}/phone   phone mirror
- Publish: IAM only, and only the projector's role holds appsync:EventPublish. Subscribe: API key
  (hackathon simplification; the plan's option is a Lambda authorizer that checks the dashboard token:
  add an AppSyncAuthProvider(LAMBDA) and switch the namespace's subscribe mode).
- Projector Lambda: EventBridge rule on the `fieldwatch` bus for every event the feed shows -> FeedMessage ->
  signed HTTP publish; also writes THREAD# history items into fw-farms. It is the only publisher.

Outputs: FeedHttpEndpoint, FeedRealtimeEndpoint, FeedApiKey, FeedChannelPattern.
"""
from aws_cdk import CfnOutput, Duration, Expiration, Stack, Tags
from aws_cdk import aws_appsync as appsync
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .common import python_lambda

DETAIL_TYPES = [
    "SceneAnalyzed", "AnomalyDetected", "WatchOpened", "InvestigationProgress", "InvestigationConcluded",
    "ReplyReceived", "ApprovalRecorded", "ActionCompleted", "MessageSent", "MessageReceived",
]  # keep in step with agent/cloud/feed/mapping.py EVENT_MODELS (tests/test_feed.py checks)
NAMESPACE = "fieldwatch"
API_KEY_NAME = "FeedKey"
IAM = appsync.AppSyncAuthorizationType.IAM
API_KEY = appsync.AppSyncAuthorizationType.API_KEY


class Feed(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")

        self.api = appsync.EventApi(
            self, "Events",
            api_name="fieldwatch-feed",
            authorization_config=appsync.EventApiAuthConfig(
                auth_providers=[
                    appsync.AppSyncAuthProvider(authorization_type=IAM),
                    appsync.AppSyncAuthProvider(
                        authorization_type=API_KEY,
                        api_key_config=appsync.AppSyncApiKeyConfig(
                            name=API_KEY_NAME, description="Subscribe-only key for the dashboard and phone mirror",
                            expires=Expiration.after(Duration.days(365))),
                    ),
                ],
                connection_auth_mode_types=[API_KEY, IAM],
                default_publish_auth_mode_types=[IAM],
                default_subscribe_auth_mode_types=[API_KEY],
            ),
        )
        self.api.add_channel_namespace(
            "FieldwatchNamespace",
            channel_namespace_name=NAMESPACE,
            authorization_config=appsync.NamespaceAuthConfig(
                publish_auth_mode_types=[IAM], subscribe_auth_mode_types=[API_KEY]),
        )

        farms = data.tables["fw-farms"]
        investigations = data.tables["fw-investigations"]
        self.projector = python_lambda(
            self, "Projector",
            handler="cloud.feed.handler.handler",
            timeout_s=30,
            memory_mb=256,
            environment={
                "APPSYNC_HTTP_HOST": self.api.http_dns,
                "FARMS_TABLE": farms.table_name,
                "INVESTIGATIONS_TABLE": investigations.table_name,
            },
        )
        self.api.grant_publish(self.projector)  # the only principal with appsync:EventPublish
        farms.grant_write_data(self.projector)
        investigations.grant_read_data(self.projector)

        dlq = sqs.Queue(self, "ProjectorDlq", retention_period=Duration.days(7))
        events.Rule(
            self, "FeedEvents",
            event_bus=data.bus,
            description="Every event the live feed shows -> projector",
            event_pattern=events.EventPattern(detail_type=DETAIL_TYPES),
            targets=[targets.LambdaFunction(
                self.projector, dead_letter_queue=dlq, retry_attempts=3, max_event_age=Duration.hours(1))],
        )

        CfnOutput(self, "FeedHttpEndpoint", value=f"https://{self.api.http_dns}/event",
                  description="Events HTTP endpoint (publish; also the host in subscribe auth)")
        CfnOutput(self, "FeedRealtimeEndpoint", value=f"wss://{self.api.realtime_dns}/event/realtime",
                  description="Events WebSocket endpoint (subscribe)")
        CfnOutput(self, "FeedApiKey", value=self.api.api_keys[API_KEY_NAME].attr_api_key,
                  description="Subscribe-only API key (hackathon); expires in 365 days")
        CfnOutput(self, "FeedChannelPattern", value="/fieldwatch/farms/{farm_id}/feed | /fieldwatch/farms/{farm_id}/phone")
