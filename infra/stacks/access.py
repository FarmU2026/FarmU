"""Who can touch FieldWatch in AWS (docs/decisions.md D3).

- fieldwatch-github-deploy: assumed by GitHub Actions on FarmU2026/FarmU (main branch or the
  "deploy" environment) through the account's EXISTING GitHub OIDC provider. The provider is only
  referenced, never created or modified: it belongs to another project in this account.
- fieldwatch-dev: what Kurt and Aaron assume for building and debugging. No CDK deploys (those run as
  CloudFormation's admin execution role, which would sidestep the boundary); Caleb deploys.
- fieldwatch-kurt / fieldwatch-aaron: IAM users with no credentials. Caleb creates console passwords or
  access keys for them by hand in the IAM console and shares them out of band.

Every role and user carries the fieldwatch-boundary permissions boundary (infra/policies/), created
once by hand before bootstrapping (infra/README.md).
"""
from aws_cdk import Duration, Stack, Tags
from aws_cdk import aws_iam as iam
from constructs import Construct

# GitHub sends the immutable subject form for this repo ("owner@owner_id/repo@repo_id"), which survives
# renames and can't be claimed by a new repo with the same name. Plain names kept as fallbacks.
GITHUB_REPOS = ["FarmU2026@340763669/FarmU@1413406950", "FarmU2026/FarmU", "kdumbl/FarmU"]
GITHUB_HOST = "token.actions.githubusercontent.com"
BOUNDARY_NAME = "fieldwatch-boundary"
DEVELOPERS = ["kurt", "aaron"]


class Access(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, data, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "fieldwatch")
        boundary = iam.ManagedPolicy.from_managed_policy_name(self, "Boundary", BOUNDARY_NAME)

        # --- GitHub Actions deploy role (existing provider, referenced by ARN only) ---
        provider = iam.OpenIdConnectProvider.from_open_id_connect_provider_arn(
            self, "GitHubProvider", f"arn:aws:iam::{self.account}:oidc-provider/{GITHUB_HOST}"
        )
        deploy = iam.Role(
            self, "GitHubDeploy",
            role_name="fieldwatch-github-deploy",
            max_session_duration=Duration.hours(1),
            permissions_boundary=boundary,
            assumed_by=iam.FederatedPrincipal(
                provider.open_id_connect_provider_arn,
                conditions={
                    "StringEquals": {f"{GITHUB_HOST}:aud": "sts.amazonaws.com"},
                    "StringLike": {f"{GITHUB_HOST}:sub": [
                        sub for repo in GITHUB_REPOS
                        for sub in (f"repo:{repo}:ref:refs/heads/main", f"repo:{repo}:environment:deploy")
                    ]},
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
            description="GitHub Actions deploys for FieldWatch (FarmU2026/FarmU) via CDK bootstrap roles",
        )
        deploy.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole", "sts:TagSession"],
            resources=[f"arn:aws:iam::{self.account}:role/cdk-hnb659fds-*-{self.account}-{self.region}"],
        ))
        deploy.add_to_policy(iam.PolicyStatement(
            actions=["cloudformation:Describe*", "cloudformation:List*", "cloudformation:GetTemplate",
                     "bedrock-agentcore:Get*", "bedrock-agentcore:List*", "ssm:GetParameter"],
            resources=["*"],
        ))

        # --- Developer role: everything needed to build and debug, nothing account-wide ---
        dev = iam.Role(
            self, "Dev",
            role_name="fieldwatch-dev",
            max_session_duration=Duration.hours(12),
            permissions_boundary=boundary,
            assumed_by=iam.AccountPrincipal(self.account).with_conditions(
                {"StringLike": {"aws:PrincipalArn": [f"arn:aws:iam::{self.account}:user/fieldwatch-*"]}}
            ),
            description="FieldWatch developers (Kurt, Aaron): models, data, logs, invoke; no deploys",
        )
        table_arns = [f"arn:aws:dynamodb:{self.region}:{self.account}:table/fw-*"]
        statements = [
            (["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse",
              "bedrock:ConverseStream", "bedrock:Get*", "bedrock:List*"], ["*"]),
            (["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeGateway",
              "bedrock-agentcore:Get*", "bedrock-agentcore:List*"], ["*"]),
            (["dynamodb:*"], table_arns + [a + "/index/*" for a in table_arns]),
            (["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
             [data.bucket.bucket_arn, data.bucket.arn_for_objects("*")]),
            (["events:PutEvents", "events:Describe*", "events:List*"], ["*"]),
            (["lambda:InvokeFunction", "lambda:Get*", "lambda:List*"], ["*"]),
            (["logs:Describe*", "logs:Get*", "logs:FilterLogEvents", "logs:StartQuery",
              "logs:StopQuery", "logs:GetQueryResults", "logs:StartLiveTail", "logs:StopLiveTail"], ["*"]),
            (["cloudformation:Describe*", "cloudformation:List*", "cloudformation:GetTemplate",
              "cloudwatch:Get*", "cloudwatch:List*", "cloudwatch:Describe*",
              "xray:Get*", "xray:BatchGet*", "states:Describe*", "states:List*", "states:StartExecution",
              "scheduler:Get*", "scheduler:List*", "ssm:GetParameter", "sts:GetCallerIdentity"], ["*"]),
        ]
        for actions, resources in statements:
            dev.add_to_policy(iam.PolicyStatement(actions=actions, resources=resources))

        # --- Developer users: no credentials here; Caleb creates them in the console ---
        for name in DEVELOPERS:
            user = iam.User(self, f"User-{name}", user_name=f"fieldwatch-{name}", permissions_boundary=boundary)
            user.add_to_policy(iam.PolicyStatement(actions=["sts:AssumeRole"], resources=[dev.role_arn]))
            user.add_to_policy(iam.PolicyStatement(
                actions=["iam:ChangePassword", "iam:GetUser", "iam:ListAccessKeys", "iam:CreateAccessKey",
                         "iam:DeleteAccessKey", "iam:UpdateAccessKey", "iam:ListMFADevices",
                         "iam:CreateVirtualMFADevice", "iam:EnableMFADevice"],
                resources=[f"arn:aws:iam::{self.account}:user/fieldwatch-{name}",
                           f"arn:aws:iam::{self.account}:mfa/fieldwatch-{name}"],
            ))
            user.add_to_policy(iam.PolicyStatement(actions=["iam:GetAccountPasswordPolicy"], resources=["*"]))

        self.deploy_role = deploy
        self.dev_role = dev
