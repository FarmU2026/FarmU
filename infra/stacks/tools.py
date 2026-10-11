"""FieldWatch-Tools: the AgentCore Gateway and the Lambda behind its tools (Build Plan T19; spike findings 20-24).

    Gateway (MCP, AWS_IAM inbound, DEBUG errors)
      -> target "fw" (Lambda target, GATEWAY_IAM_ROLE outbound, inline schemas generated from the registry)
        -> one container-image Lambda (cloud.gateway.handler.handler) that dispatches to registry.BY_NAME

Tools are named `fw___<tool>` on the Gateway. Which registry tools are exposed is decided in one place,
`cloud.gateway.schemas.EXPOSED_ACCESS` (read-only today); schemas and handler both follow it, so exposing the
write tools (workstream 8) is a one-line change there plus a redeploy.

Deploy (Caleb; needs Docker for the geo image):   npx aws-cdk@2 deploy FieldWatch-Tools
"""
from __future__ import annotations

import sys

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from .common import AGENT, geo_lambda

sys.path.insert(0, str(AGENT))  # the schemas come from the registry, so synth imports agent/ code
from cloud.gateway.schemas import tool_definitions  # noqa: E402

GATEWAY_NAME = "fieldwatch-gateway"
TARGET_NAME = "fw"  # tools appear as fw___<tool> (agent/fieldwatch/toolset.py strips the prefix for the agent)
BOUNDARY_NAME = "fieldwatch-boundary"
SchemaProp = agentcore.CfnGatewayTarget.SchemaDefinitionProperty


def schema_property(node: dict) -> SchemaProp:
    """A flattened schema dict (cloud.gateway.schemas) as the typed L1 property it maps onto 1:1."""
    return SchemaProp(
        type=node["type"],
        description=node.get("description"),
        items=schema_property(node["items"]) if "items" in node else None,
        properties={k: schema_property(v) for k, v in node["properties"].items()} if "properties" in node else None,
        required=node.get("required"),
    )


def tool_property(tool: dict) -> agentcore.CfnGatewayTarget.ToolDefinitionProperty:
    return agentcore.CfnGatewayTarget.ToolDefinitionProperty(
        name=tool["name"], description=tool["description"], input_schema=schema_property(tool["inputSchema"])
    )


class Tools(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")
        iam.PermissionsBoundary.of(self).apply(iam.ManagedPolicy.from_managed_policy_name(self, "Boundary", BOUNDARY_NAME))

        # --- the Lambda: geo image because some registry tools read rasters ---
        self.function = geo_lambda(
            self, "GatewayTools",
            handler="cloud.gateway.handler.handler",
            timeout_s=60,
            environment={
                "FIELDWATCH_BUCKET": data.bucket.bucket_name,
                "FIELDWATCH_TABLE_PREFIX": "fw-",
                "FIELDWATCH_EVENT_BUS": "fieldwatch",
                "FIELDWATCH_DATA_BACKEND": "dynamo",  # list_fields / get_soil / estimate_loss read fw- tables, not the fixture
                "FIELDWATCH_TOOL_TIMEOUT_S": "45",
            },
        )
        # Read-only: every fw- table (and indexes), the bucket, and /fieldwatch/* parameters.
        self.function.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan",
                     "dynamodb:DescribeTable"],
            resources=[f"arn:aws:dynamodb:{self.region}:{self.account}:table/fw-*",
                       f"arn:aws:dynamodb:{self.region}:{self.account}:table/fw-*/index/*"],
        ))
        data.bucket.grant_read(self.function)
        self.function.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/fieldwatch/*"],
        ))

        # --- the role AgentCore assumes to invoke the Lambda (finding 20) ---
        role = iam.Role(
            self, "GatewayRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="AgentCore Gateway -> FieldWatch tools Lambda",
        )
        self.function.grant_invoke(role)

        # --- Gateway: MCP, IAM inbound auth, readable errors while building ---
        self.gateway = agentcore.CfnGateway(
            self, "Gateway",
            name=GATEWAY_NAME,
            description="FieldWatch agent tools",
            protocol_type="MCP",
            authorizer_type="AWS_IAM",
            role_arn=role.role_arn,
            exception_level="DEBUG",
            tags={"project": "fieldwatch"},
        )

        target = agentcore.CfnGatewayTarget(
            self, "Target",
            gateway_identifier=self.gateway.attr_gateway_identifier,
            name=TARGET_NAME,
            description="Registry tools (agent/fieldwatch/registry.py) served by one Lambda",
            target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                    lambda_=agentcore.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                        lambda_arn=self.function.function_arn,
                        tool_schema=agentcore.CfnGatewayTarget.ToolSchemaProperty(
                            inline_payload=[tool_property(t) for t in tool_definitions()]
                        ),
                    )
                )
            ),
            credential_provider_configurations=[
                agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                    credential_provider_type="GATEWAY_IAM_ROLE"
                )
            ],
        )
        target.node.add_dependency(role)  # the role's invoke policy must exist before the target is created

        # Other stacks (the Runtime role needs bedrock-agentcore:InvokeGateway on the ARN) read these.
        self.gateway_arn = self.gateway.attr_gateway_arn
        self.gateway_url = self.gateway.attr_gateway_url
        CfnOutput(self, "GatewayId", value=self.gateway.attr_gateway_identifier, export_name="FieldWatch-Tools-GatewayId")
        CfnOutput(self, "GatewayUrl", value=self.gateway.attr_gateway_url, export_name="FieldWatch-Tools-GatewayUrl")
        CfnOutput(self, "GatewayArn", value=self.gateway.attr_gateway_arn, export_name="FieldWatch-Tools-GatewayArn")
        CfnOutput(self, "ToolsFunctionName", value=self.function.function_name)
