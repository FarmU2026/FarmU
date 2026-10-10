# Build decisions (Oct 10)

The plan docs describe the target. This file records where the build deliberately differs, and it **overrides every other doc**. Change it only by team agreement.

## D1. Shape: the web app is the hub, the serverless plan is the backend

- `agent/web/` (FastAPI + the Leaflet page) stays the farmer-facing pages, dashboard and phone mirror. It replaces the plan's API Gateway pages and AppSync feed.
- Behind it we build the plan's backend: scene and hazard pipelines, field analyzer, event bus, agents on AgentCore Runtime, tools behind AgentCore Gateway, DynamoDB/S3 state.
- **Every cloud capability sits behind a switch** in `agent/fieldwatch/config.py`, and `local` (today's in-process code) is the default and the demo fallback:

| Switch | `local` (default) | cloud value |
| --- | --- | --- |
| `FIELDWATCH_AGENT_BACKEND` | Strands agents in the web process | `runtime`: AgentCore Runtime |
| `FIELDWATCH_TOOLS_BACKEND` | Python functions in-process | `gateway`: AgentCore Gateway → Lambda |
| `FIELDWATCH_STORE` | JSON files in `agent/data/` | `dynamo`: the `fw-` tables |
| `FIELDWATCH_SESSIONS` | `FileSessionManager` | `s3` (or `memory` for AgentCore Memory) |
| `FIELDWATCH_TRIGGER` | watcher thread + ASK | `cloud`: Scheduler → scene check → EventBridge → session starter |

- The laptop has no public URL, so the cloud side never calls the web app: it writes to DynamoDB and the web app polls.
- `main` must always run and pass CI with every switch on `local`.

## D2. Infrastructure as code: CDK, not Terraform

The AgentCore CLI already deploys with CDK (spike finding 31), so everything uses CDK.
- `infra/` is a **Python CDK app** for the data plane and pipelines: one shared `DataPlane` stack (bucket, tables, bus) plus **one stack per workstream**, named `FieldWatch-<Workstream>`.
- The agent, Gateway and AgentCore Memory are managed by the **AgentCore CLI** project in `agent/cloud/agentcore/`.
- Only the integrator deploys `DataPlane`. Each workstream deploys only its own stack.
- Everything is tagged `project=fieldwatch`.

## D3. Account and region

- One shared account (the team's), region **us-east-2**, matching the team's Bedrock setup. Sentinel-2 files live in us-west-2; cross-region reads of a few MB per field cost fractions of a cent, so we accept them.
- Bedrock API keys work for model calls only. **Gateway and Runtime calls need IAM credentials** (SigV4); each person gets IAM access keys.
- CDK bootstrap the account once, deliberately (finding 36).

## D4. Models

- Default: `us.anthropic.claude-haiku-4-5-20251001-v1:0`; demo supervisor `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (or Sonnet 4.6 if enabled); fallback for any role `us.amazon.nova-pro-v1:0` (no Anthropic form needed).
- The imagery specialist needs a vision-capable model (Claude 4.x or Nova Pro).
- Keep agent tests on the scripted fake model; spend Bedrock credits only on integration.

## D5. Agent build

- AgentCore CLI (`npx agentcore …`), **CodeZip** build, no container (findings 29–32). The Build Plan's `agentcore configure` commands and ARM64 container wording are obsolete.

## D6. Approvals

- Keep **Strands interrupts** as the human-in-the-loop mechanism (they are better than the plan's design for the demo).
- Add the plan's safeguards around them (workstream 8): approval records in `fw-approvals`, expiry, "already decided by …", replies accepted only from the number asked, one approval → at most one action, and required `approval_id`/`investigation_id` in write-tool schemas at the Gateway.

## D7. Analysis

- Deterministic code computes; agents gather, interpret and explain (pipelines doc principle). The LLM never sets a confidence number: the scorer does.
- Baseline: the **same weeks in 2–3 prior seasons** from the Sentinel-2 archive instead of fitted seasonal curves; fitted curves are post-hackathon.
- Every analysis takes an **as-of date** (replay), and end-of-season gates apply (it's October; live passes show harvest).
- Field boundaries: farmers draw them in the web app; USDA CSB 2018–2025 supplies rotation history and same-crop neighbors where available (findings 1–2).

## D8. Messaging

- Primary: **Twilio** (trial: verified numbers only; replies polled). The **phone mirror** is always on.
- AWS End User Messaging is another provider behind `sms.py`, used once a number is registered (finding 39). Not this weekend.

## D9. Cut for the hackathon (roadmap)

Fitted multi-season curves, Bedrock Knowledge Base (a small curated excerpt file replaces it), photo intake pipeline, document upload, voice, wells/water layers, Cedar policy beyond required schema fields, AppSync, Step Functions (a Lambda loop with per-field error handling replaces the Map state).

## D10. Owners

| Person | Workstreams (see `docs/workstreams.md`) |
| --- | --- |
| Kurt | 9 Notifications, 3 Hazards, 4 Context |
| Aaron | 1 Field analyzer, 5 Scorer + condition catalog + impact |
| Caleb | 0 Foundation and integration, 2 Orchestration + replay, 6 Gateway tools, 7 Agent on Runtime, 8 Approvals, 10 Web app as client, 11 Evaluation |
