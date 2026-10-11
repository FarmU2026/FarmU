"""FieldWatch-Agent: the session starter that wakes the Runtime agent (workstream 7, T23).

The agent itself (AgentCore Runtime) is deployed by the AgentCore CLI project in agent/cloud/agentcore/,
which this stack cannot create. They meet in two places, both written by hand/script after `agentcore deploy`
(agent/cloud/agentcore/README.md):
  - SSM /fieldwatch/runtime-arn  -> read by the session starter Lambda at runtime
  - SSM /fieldwatch/sessions-bucket -> written here; read by the agent at startup (FIELDWATCH_SSM_BOOTSTRAP=1)

Event routing: bus `fieldwatch` -> rules -> SessionStarter Lambda -> bedrock-agentcore InvokeAgentRuntime.
Failures retry (Lambda async retries, then the dead-letter queue); the rules' own DLQ catches delivery failures.
"""
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from stacks.common import python_lambda

RUNTIME_ARN_PARAM = "/fieldwatch/runtime-arn"
SESSIONS_BUCKET_PARAM = "/fieldwatch/sessions-bucket"
BUCKET_PARAM = "/fieldwatch/bucket"  # agent/app.py _SSM_ENV reads these at Runtime startup

# (rule id, detail types, sources or None)
RULES = [
    ("AnomalyDetected", ["AnomalyDetected"], None),
    ("StartInvestigation", ["StartInvestigation"], ["fieldwatch.api"]),
    ("ApprovalRecorded", ["ApprovalRecorded"], None),
    ("ReplyReceived", ["ReplyReceived"], None),
    ("PhotoReceived", ["PhotoReceived", "DocumentReceived"], None),
    ("ActionCompleted", ["ActionCompleted"], None),
]


class Agent(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")

        self.dlq = sqs.Queue(
            self, "StarterDlq", retention_period=Duration.days(14), enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.starter = python_lambda(
            self, "SessionStarter",
            handler="cloud.lambdas.session_starter.handler.handler",
            timeout_s=60,  # the Runtime may cold-start (~11 s) before it acknowledges
            memory_mb=256,
            reserved_concurrency=5,  # each invocation starts a Bedrock-backed investigation: cap the cost
            environment={
                "FIELDWATCH_RUNTIME_ARN_PARAM": RUNTIME_ARN_PARAM,
                "FIELDWATCH_INVESTIGATIONS_TABLE": "fw-investigations",  # fixed name (data_plane.py)
                "FIELDWATCH_FIELDS_TABLE": "fw-fields",  # question runs pick the farm's field from it
            },
            retry_attempts=2,
            max_event_age=Duration.hours(6),
            dead_letter_queue=self.dlq,
        )
        data.tables["fw-investigations"].grant_read_write_data(self.starter)
        data.tables["fw-fields"].grant_read_data(self.starter)
        self.starter.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"],
        ))
        self.starter.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{RUNTIME_ARN_PARAM}"],
        ))

        for rid, detail_types, sources in RULES:
            events.Rule(
                self, f"On{rid}",
                event_bus=data.bus,
                event_pattern=events.EventPattern(detail_type=detail_types, **({"source": sources} if sources else {})),
                targets=[targets.LambdaFunction(
                    self.starter, dead_letter_queue=self.dlq, retry_attempts=3, max_event_age=Duration.hours(2),
                )],
            )

        # The agent finds its S3 session bucket here, so the committed CLI project holds no account values.
        ssm.StringParameter(
            self, "SessionsBucketParam", parameter_name=SESSIONS_BUCKET_PARAM,
            string_value=data.bucket.bucket_name,
            description="S3 bucket for Strands sessions (prefix sessions/); read by the Runtime agent",
        )
        ssm.StringParameter(
            self, "BucketParam", parameter_name=BUCKET_PARAM, string_value=data.bucket.bucket_name,
            description="FieldWatch data bucket (overlays, context); read by the Runtime agent at startup",
        )

        CfnOutput(self, "StarterDlqUrl", value=self.dlq.queue_url)
        CfnOutput(self, "StarterFunction", value=self.starter.function_name)
