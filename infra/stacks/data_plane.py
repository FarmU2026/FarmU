"""Shared data plane (Build Plan T03, docs/implementation-specs.md "Data model").

Deployed by the integrator only. Workstream stacks import these resources through the
`DataPlane` object passed to them in app.py; they never create their own tables or buses.
Hackathon setting: everything is destroyed with the stack (RemovalPolicy.DESTROY).
"""
from aws_cdk import Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_s3 as s3
from constructs import Construct

S = ddb.AttributeType.STRING

# name, partition key, sort key, GSIs [(name, pk, sk)], TTL attribute
TABLES = [
    ("fw-farms", "farm_id", "item", [("by-phone", "phone_e164", None)], None),
    ("fw-fields", "farm_id", "field_id", [("by-field", "field_id", None)], None),
    ("fw-tile-index", "tile_id", "field_id", [], None),
    ("fw-field-context", "field_id", None, [], None),
    ("fw-scene-analyses", "field_id", "date_source", [], "expires_at"),
    ("fw-watches", "field_id", "opened_at_watch_id", [], "expires_at"),
    ("fw-investigations", "investigation_id", None, [("by-field", "field_id", "created_at")], None),
    ("fw-approvals", "approval_id", None, [("by-person-status", "sent_to_person_id", "status")], "expires_at_epoch"),
    ("fw-actions", "action_id", None, [("by-assignee", "assignee_person_id", None)], None),
    ("fw-messages", "message_id", None, [("by-provider-id", "provider_message_id", None)], None),
    ("fw-uploads", "upload_id", None, [], "expires_at"),
    ("fw-idempotency", "key", None, [], "expires_at"),
]


class DataPlane(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")

        self.bucket = s3.Bucket(
            self, "Bucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[
                s3.LifecycleRule(prefix="analysis/", expiration=Duration.days(180)),
                s3.LifecycleRule(prefix="hazards/", expiration=Duration.days(180)),
                s3.LifecycleRule(prefix="uploads/", expiration=Duration.days(365)),
                s3.LifecycleRule(prefix="sessions/", expiration=Duration.days(30)),
            ],
        )

        self.tables: dict[str, ddb.TableV2] = {}
        for name, pk, sk, gsis, ttl in TABLES:
            table = ddb.TableV2(
                self, name,
                table_name=name,
                partition_key=ddb.Attribute(name=pk, type=S),
                sort_key=ddb.Attribute(name=sk, type=S) if sk else None,
                billing=ddb.Billing.on_demand(),
                removal_policy=RemovalPolicy.DESTROY,
                time_to_live_attribute=ttl,
                global_secondary_indexes=[
                    ddb.GlobalSecondaryIndexPropsV2(
                        index_name=gname,
                        partition_key=ddb.Attribute(name=gpk, type=S),
                        sort_key=ddb.Attribute(name=gsk, type=S) if gsk else None,
                    )
                    for gname, gpk, gsk in gsis
                ],
            )
            self.tables[name] = table

        self.bus = events.EventBus(self, "Bus", event_bus_name="fieldwatch")

        # The link-signing key is an SSM SecureString, which CloudFormation can't create.
        # Create it once by hand: see infra/README.md.
