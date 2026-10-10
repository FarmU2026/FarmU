#!/usr/bin/env python3
"""FieldWatch CDK app (docs/decisions.md D2).

One shared DataPlane stack (integrator deploys it) plus one stack per workstream, named
FieldWatch-<Workstream>. Add your workstream's stack below; deploy only your own:

    cdk deploy FieldWatch-Hazards

The agent, Gateway and AgentCore Memory live in the AgentCore CLI project (agent/cloud/agentcore/),
not here.
"""
import os

import aws_cdk as cdk

from stacks.access import Access
from stacks.context import Context
from stacks.data_plane import DataPlane

app = cdk.App()
env = cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region=os.getenv("CDK_DEFAULT_REGION", "us-west-2"))

data = DataPlane(app, "FieldWatch-DataPlane", env=env)
Access(app, "FieldWatch-Access", data=data, env=env)
context = Context(app, "FieldWatch-Context", data=data, env=env)  # workstream 15; context.state_machine for the web API

# Workstream stacks go here, each receiving `data` for the shared bucket, tables and bus, e.g.:
# from stacks.hazards import Hazards
# Hazards(app, "FieldWatch-Hazards", data=data, env=env)          # workstream 3 (Kurt)
# from stacks.notifications import Notifications
# Notifications(app, "FieldWatch-Notifications", data=data, env=env)  # workstream 9 (Kurt)
# from stacks.analysis import Analysis
# Analysis(app, "FieldWatch-Analysis", data=data, env=env)        # workstreams 1-2
# from stacks.tools import Tools
# Tools(app, "FieldWatch-Tools", data=data, env=env)              # workstream 6 (Lambda targets for the Gateway)

app.synth()
