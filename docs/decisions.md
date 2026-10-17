# Build decisions (Oct 10)

The plan docs describe the target, and we are building **the full original architecture** (`docs/system-design.md`). This file records the few places the build deliberately differs, and it **overrides every other doc**. Change it only by team agreement.

The pre-pivot local demo is preserved on its own branch; that branch is the fallback if the full build isn't demo-ready at freeze.

## D1. Shape: the plan's serverless system, with today's UI as its front end

- **Front end:** the current Leaflet UI (`agent/web/static/`: sign-up map, field drawing, phone panel) is ported to static pages on **S3 + CloudFront**. It calls the **API Gateway HTTP API** and subscribes to **AppSync Events** for live updates. It keeps today's look, plus the plan's report page (before/after slider, checked list, dollar range, approve/decline) and work-order page.
- **API:** API Gateway HTTP API + Lambdas (T27). It serves the plan's `/v1/...` routes **and** keeps the JSON shapes of today's `/api/farms...` routes from `agent/web/server.py`, so the ported UI needs minimal changes.
- **Live feed:** AppSync Events, one channel per farm, published only by the projector Lambda (T28).
- **Pipelines:** Earth Search SNS / Scheduler → SQS → scene filter → **Step Functions Express** (load inputs → analyze → decide) → EventBridge (T14); context builder as **Step Functions Standard** (T08–T12); hazards (T15, T16).
- **Agents:** Strands supervisor + specialists on **AgentCore Runtime**; tools via **AgentCore Gateway** (Lambda targets generated from `registry.py`); AgentCore Memory, Observability, Evaluations (T19–T24, T32).
- **State:** DynamoDB `fw-` tables and the S3 bucket only.
- `agent/web/server.py` (FastAPI) remains a **local development harness** for agent and UI work until the port lands; it is not part of the deployed system.

## D2. Infrastructure as code: CDK, not Terraform

The AgentCore CLI deploys with CDK (spike finding 31), and the data plane is already deployed with CDK, so everything uses CDK.
- `infra/` is a **Python CDK app**: shared stacks `FieldWatch-DataPlane` (tables, bus, bucket) and `FieldWatch-Access` (roles, users), plus one stack per subsystem, named `FieldWatch-<Subsystem>`.
- The agent on Runtime is managed by the **AgentCore CLI** project in `agent/cloud/agentcore/` (its own generated CDK).
- Lambdas: Python 3.12, **x86_64**, code from `agent/` packaged as-is. Everything tagged `project=fieldwatch`.
- Only Caleb or the deploy workflow deploys.

## D3. Account, region and access

- **Caleb's AWS account, region us-west-2**: where the spikes validated everything, next to the Sentinel-2 files. Budget alert $50/month.
- **No resources in us-east-1** (the account's other project uses it). The MRMS hail topic lives in us-east-1, so we subscribe an **SQS queue in us-west-2** to it directly instead of the plan's us-east-1 forwarder Lambda.
- The account also hosts another project. Never create `preflight-*` or `pr-*` names, never use the tag `preflight-derive`, never create or modify the account's GitHub OIDC provider (we reference it). The `fieldwatch-boundary` permissions boundary (`infra/policies/`) enforces this on every FieldWatch role, user and the CDK execution role.
- **Deploys:** Caleb from his machine, and GitHub Actions (`.github/workflows/deploy.yml`) through role `fieldwatch-github-deploy` (existing GitHub OIDC provider, `FarmU2026/FarmU` main or the `deploy` environment only). Role ARNs and the account ID live in GitHub variables, never in code.
- **Kurt and Aaron** use IAM users `fieldwatch-kurt` / `fieldwatch-aaron` → role `fieldwatch-dev` (Bedrock, FieldWatch tables and bucket, Lambda invoke, logs, events, AgentCore invoke; no deploys).
- Local agent runs may use a Bedrock API key in `agent/.env`; API keys can't sign Gateway/Runtime calls.

## D4. Models

- Default `us.anthropic.claude-haiku-4-5-20251001-v1:0`; demo supervisor `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (or Sonnet 4.6); fallback for any role `us.amazon.nova-pro-v1:0`. Claude needs the account's Anthropic use-case form.
- The imagery specialist needs a vision-capable model. Unit tests use the scripted fake model.

## D5. Agent build

- AgentCore CLI (`npx agentcore …`), **CodeZip**, no container (findings 29–32). The Build Plan's `agentcore configure` and ARM64-container wording is obsolete.

## D6. Approvals

- **Strands interrupts** pause the agent in-session; the session persists to S3 (or AgentCore Memory) so a reply hours later resumes it on Runtime.
- Around them, the plan's design (T22): approval records in `fw-approvals` written only by the reply handler or the report page; expiry; replies accepted only from the number asked; "already decided by …"; one approval → at most one action; `approval_id`/`investigation_id` required in write-tool schemas (the Gateway validates them, finding 23); Cedar policy on the Gateway if time allows.

## D7. Analysis

- Deterministic code computes; agents gather, interpret and explain. The LLM never sets a confidence number: the scorer does.
- Baseline: the **same weeks in 2–3 prior seasons** from the Sentinel-2 archive instead of fitted seasonal curves (fitted curves are post-hackathon).
- Every analysis takes an **as-of date** (replay), and end-of-season gates apply.
- Field boundaries: drawn by the farmer in the UI (no CSB picker); USDA CSB 2018–2025 supplies crop history and same-crop neighbors (findings 1–2).

## D8. Messaging

- **Twilio** behind the plan's send and reply-handler Lambdas (T17, T18). Inbound texts arrive by **Twilio webhook → API Gateway** (verify the Twilio signature), so no polling.
- The phone mirror (AppSync channel) is always on. AWS End User Messaging stays a stub provider until a number is registered (finding 39).

## D9. Cut order if behind (drop from the top)

Voice → document upload → Bedrock Knowledge Base (keep a curated excerpt file) → photo intake pipeline → agents tab → live Sentinel-2 SNS subscription (replay stays) → NWS and storm-report pollers → AgentCore Evaluations (keep the script runner) → AgentCore Memory (keep S3 sessions) → Cedar policy (keep schema-required fields + Lambda checks).

**Never cut:** replay, field analyzer, supervisor + specialists, scorer, report page with the reveal, phone mirror, approval → work order.

## D10. Owners

| Who | Workstreams (see `docs/workstreams.md`) |
| --- | --- |
| Kurt | 9 Notifications (send + reply-handler Lambdas), 3 Hazards, 4 Field context functions |
| Aaron | 1 Field analyzer, 5 Scorer + condition catalog + impact + agent reasoning |
| Caleb | Integration, reviews, deploys, agronomy weights, hero scenes (11) |
| Claude agents (Caleb reviews) | 12 Web API, 13 Live feed, 14 Front end on CloudFront, 2 Scene pipeline + replay, 15 Context builder workflow, 6 Gateway + tools, 7 Agent on Runtime, 8 Approvals + policy; then 16 Knowledge base, 17 Photo intake, 11 Evaluations |

## D11. Diagnosis: evidence in code, "not determined" is an outcome (Oct 16)

Source: `docs/research/grounding.md`. This refines D7.
- `agent/fieldwatch/evidence.py` evaluates every catalog evidence key (`present` / `absent` / `not_evaluable`) from the field's inputs on every run. LLMs never choose keys; they may only add `photo` and `farmer` evidence as fixed fields.
- Keys are `field`, `static` or `regional`. Regional evidence is capped at +-0.7 log-odds per condition, static at +-1.5; correlated items (a `cluster`) count once.
- A condition is named only with a required field-evidence key, inside its stage window, with no rule-out, at 0.65 probability and 0.15 ahead of the runner-up. Otherwise the outcome is `not_determined` with "possible" candidates and a scouting pin. Farmer text uses likely / possible / not determined, never percentages.
- Nitrogen is "possible nitrogen variation, please confirm" (red-edge sufficiency index, corn V6-V12) until the farmer reports N short. Disease and herbicide drift are never named from imagery alone.
- On-demand checks (no anomaly) read the latest analysis' `crop_state` first; a non-diagnosable season is answered in code without agents. Pin location, acres and dollars come from code (`runfacts.py`), not the model. Chat replies are a structured `FarmerReply`, at most 320 characters.
