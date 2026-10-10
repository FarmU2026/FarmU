# cloud/ (Caleb)

The cloud side of each switch in config.SWITCHES (docs/decisions.md D1).

| Folder | Workstream | What |
| --- | --- | --- |
| `agentcore/` | WS7 | AgentCore CLI project (`npx agentcore create`, CodeZip) deploying `agent/app.py`; S3 sessions, tracing |
| `gateway/` | WS6 | One Lambda handler dispatching Gateway tool calls to `registry.BY_NAME` (strip the `target___` prefix) |
| `lambdas/scene_check/` | WS2 | Scheduled: analyze each field, emit `AnomalyDetected` (idempotent on field + date) |
| `lambdas/session_starter/` | WS2 | On `AnomalyDetected` / `ApprovalRecorded`: invoke the agent on Runtime |

Their CDK stacks live in `infra/stacks/`; Lambdas are x86_64 Python 3.12 and import from `agent/fieldwatch/`.
