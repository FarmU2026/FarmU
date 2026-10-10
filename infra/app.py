#!/usr/bin/env python3
"""FieldWatch CDK app (docs/decisions.md D2).

Shared stacks (DataPlane, Access) plus one stack per subsystem, named FieldWatch-<Subsystem>.
Register a new stack by adding ONE import and ONE line in the matching section below.
Deploys run through the deploy workflow on main, or from Caleb's machine.

The agent itself runs on AgentCore Runtime from the AgentCore CLI project (agent/cloud/agentcore/), not here.
"""
import os

import aws_cdk as cdk

from stacks.access import Access
from stacks.api import Api
from stacks.agent import Agent
from stacks.context import Context
from stacks.data_plane import DataPlane
from stacks.feed import Feed
from stacks.frontend import Frontend
from stacks.hazards import Hazards
from stacks.scenes import Scenes
from stacks.tools import Tools

app = cdk.App()
# Region is fixed: the CDK CLI defaults to us-east-1 when no AWS config is present, and us-east-1 is off limits (D3).
env = cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region="us-west-2")

# --- shared ---
data = DataPlane(app, "FieldWatch-DataPlane", env=env)
Access(app, "FieldWatch-Access", data=data, env=env)

# --- pipelines ---
Scenes(app, "FieldWatch-Scenes", data=data, env=env)  # WS2 scene pipeline + replay
Hazards(app, "FieldWatch-Hazards", data=data, env=env)  # WS3 hail and drought
context = Context(app, "FieldWatch-Context", data=data, env=env)  # WS15; context.state_machine for the web API

# --- agents and tools ---
Tools(app, "FieldWatch-Tools", data=data, env=env)  # WS6 Gateway + tool Lambda
Agent(app, "FieldWatch-Agent", data=data, env=env)  # WS7 session starter (agent itself: AgentCore CLI)

# --- interaction ---
Feed(app, "FieldWatch-Feed", data=data, env=env)  # WS13 live feed
Api(app, "FieldWatch-Api", data=data, env=env)  # WS12 web API (HTTP API + Lambdas)
Frontend(app, "FieldWatch-Frontend", env=env)  # WS14 web/ on S3 + CloudFront

# Notifications (WS9) is integrated in its own workstream.

app.synth()
