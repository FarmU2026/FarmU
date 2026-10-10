# Workstreams (parallel build of the full architecture, Oct 10–11)

How the remaining work is split so three people and many Claude Code agents can build at once. Read `docs/decisions.md` first; it overrides the plan docs. Plan ticket numbers (T##) refer to `docs/build-plan.md`.

## How this works

- **Contracts first.** Everything that crosses a workstream boundary is a model in `agent/fieldwatch/contracts.py`, with a sample in `agent/fixtures/contracts/`. Build against the samples; don't wait for other lanes.
- **The registry is the joint.** `agent/fieldwatch/registry.py` lists every agent tool. Stub entries return contract samples; a workstream replaces its stub's `fn`. The Gateway (WS6) generates its Lambda tools from the same list.
- **Own your folders** (table below and `.github/CODEOWNERS`). Shared files have one owner; ask, or open a tiny PR.
- **Data access:** use boto3 against the `fw-` tables, bucket and `fieldwatch` bus directly, with names from `fieldwatch/config.py`. Keys and indexes: `infra/stacks/data_plane.py`.
- **One branch per workstream**, short-lived, merged to `main` often. CI (offline tests, `cdk synth`, secret scan) must stay green.
- **One Claude Code session per workstream** in its own git worktree:
  ```powershell
  git worktree add ..\FarmU-ws3 -b ws3-hazards origin/main
  cd ..\FarmU-ws3; claude
  ```
  Paste the prompt at the bottom with your workstream's goal. Ask for a plan first.
- **Contract changes** go in their own small PR, announced in team chat.
- **Deploys:** Caleb (or the deploy workflow on main) deploys every stack. Everyone else writes their stack in `infra/stacks/` and opens a PR. Kurt and Aaron use the `fieldwatch-dev` role for Bedrock, data and logs (D3).

## Workstreams

| WS | Name | Owner | Paths | Plan refs |
| --- | --- | --- | --- | --- |
| 1 | Field analyzer | Aaron | `agent/fieldwatch/analysis/`, `tests/test_analysis*.py` | T11, T13; pipelines B2, M1–M3 |
| 2 | Scene pipeline + replay | Claude → Caleb | `agent/cloud/lambdas/scenes/`, `infra/stacks/scenes.py`, `tests/test_scenes.py` | T14, T34 |
| 3 | Hazards | Kurt | `agent/fieldwatch/hazards/`, `agent/cloud/lambdas/hazards/`, `infra/stacks/hazards.py`, `tests/test_hazards.py` | T15, T16 |
| 4 | Field context functions | Kurt | `agent/fieldwatch/context/`, `tests/test_context.py` | T09, T10, T12, T26 |
| 5 | Scorer, catalog, impact, agent reasoning | Aaron | `agent/catalog/`, `agent/fieldwatch/scoring.py`, `agent/fieldwatch/agents.py`, `agent/fieldwatch/schemas.py`, `tests/test_scoring.py` | T20 |
| 6 | Gateway + tools | Claude → Caleb | `agent/cloud/gateway/`, `agent/fieldwatch/toolset.py`, `infra/stacks/tools.py`, `tests/test_gateway.py` | T19, T21 |
| 7 | Agent on Runtime + session starter + memory | Claude → Caleb | `agent/cloud/agentcore/`, `agent/app.py`, `agent/fieldwatch/investigation.py`, `agent/cloud/lambdas/session_starter/`, `infra/stacks/agent.py`, `tests/test_runtime*.py` | T23, T24 |
| 8 | Approvals + write tools + policy | Claude → Caleb | `agent/fieldwatch/approvals.py`, `agent/fieldwatch/actions.py`, `infra/policies/cedar/`, `tests/test_approvals.py` | T22 |
| 9 | Notifications | Kurt | `agent/fieldwatch/notify/` (from `sms.py`), `agent/cloud/lambdas/notify/`, `infra/stacks/notifications.py`, `tests/test_notify*.py` | T17, T18 |
| 11 | Evaluations + hero scenes | Caleb (+ Claude) | `agent/eval/`, `docs/hero-scenes.md` | P4, T32 |
| 12 | Web API | Claude → Caleb | `agent/cloud/api/`, `infra/stacks/api.py`, `tests/test_api*.py` | T27 |
| 13 | Live feed | Claude → Caleb | `agent/cloud/feed/`, `infra/stacks/feed.py`, `tests/test_feed.py` | T28 |
| 14 | Front end on CloudFront | Claude → Caleb | `web/` (new top-level static site), `infra/stacks/frontend.py` | T29, T30, T33 |
| 15 | Context builder workflow | Claude → Caleb | `agent/cloud/lambdas/context/`, `infra/stacks/context.py`, `tests/test_context_workflow.py` | T08 |
| 16 | Knowledge base | later | `data-prep/corpus.py`, `infra/stacks/knowledge.py` | T25 |
| 17 | Photo intake | later | `agent/cloud/lambdas/intake/`, `infra/stacks/intake.py` | T31 |

Shared files: `contracts.py`, `fixtures/contracts/`, `registry.py`, `config.py`, `infra/app.py`, `.github/` are Caleb's (small PRs welcome). `agent/web/` (the FastAPI dev harness) is frozen; the UI moves to `web/` in WS14.

## Goals

Write your own checks from the goal first (Build Plan "the loop for every ticket"), then implement.

1. **Field analyzer:** `analyze(FieldAnalysisRequest) -> FieldAnalysisResult`. For a field polygon and an as-of date, use the latest clear Sentinel-2 scene on or before that date. Steps:
   - per-pixel cloud and shadow mask from the scene classification band, ~10 m interior buffer;
   - NDVI, NDRE and NDMI (scale/offset for the C1 collection);
   - z-scores against the same weeks in 2–3 prior seasons, plus change since the previous clear pass;
   - regions of at least 0.5 ha with pattern features (ring, wedge, edge band, stripes; overlaps when soil, low-spot or hail layers are given);
   - gates: senescence and harvest by date and crop, uniform whole-field drop, region-wide drop;
   - overlay PNGs written to S3 under `analysis/`.

   It should flag a real summer problem and stay quiet on healthy and end-of-season fields. No web or AWS-SDK imports in the core (S3 writes go through a small injected writer). Replace `registry.analyze_field`.
2. **Scene pipeline + replay:** SQS `fw-scene-queue` → scene filter Lambda (tile index lookup, cloud rule, idempotency) → Step Functions Express (Map over fields: load inputs → analyze, calling WS1's `analyze` → decide) → `SceneAnalyzed` / `AnomalyDetected` on the bus. Plus a replay Lambda that sends a historical STAC item into the same queue (with hail backfill events), and a reset Lambda. Specs: "Imagery pipeline", "Replay correctness". The scheduled Earth Search check uses EventBridge Scheduler.
3. **Hazards:** `get_hail` (MRMS MESH in mm via GDAL, SPC reports within 10 km), `get_drought` (Drought Monitor county percentages + a written rule), historical warnings from the Iowa Environmental Mesonet. Plus the hail-watch pipeline: SQS queue in us-west-2 subscribed to the MRMS topic in us-east-1 (no us-east-1 resources) → hail filter Lambda → watches in `fw-watches` + `WatchOpened`. Plus the drought poller.
4. **Field context functions:**
   - `get_soil_profile` (Soil Data Access: map units, horizons, sandy flag, zone polygons);
   - terrain low spots (3DEP 1 m);
   - crop history and same-crop neighbors from USDA CSB 2018–2025 (EPSG:5070; cache per farm);
   - `get_disease_reports` (EDDMapS);
   - economics (NASS if a key is set, else crop defaults).

   Pure functions returning contract models; WS15 calls them from the workflow.
5. **Scorer, catalog, impact, agent reasoning:**
   - `catalog/conditions.json`: priors and weights with cited reasons; **Caleb reviews the agronomy**.
   - `score_hypotheses`: log-odds; ruled out below 0.05, likely above 0.6.
   - `estimate_impact` from `catalog/loss_tables.json`.
   - Specialists return `contracts.SpecialistResponse` evidence items; the supervisor calls the scorer and never states its own confidence; the plan's roster (imagery, environment, agronomy). Includes the fast path (R1).
6. **Gateway + tools:** one Lambda handler that dispatches on the tool name (strip `target___`) to `registry.BY_NAME`. A script generates the Gateway tool schemas from each entry's `input_model`. CDK for the Lambda, Gateway (IAM inbound auth) and target (spike findings 20–24). The two OpenAPI targets (drought, NASS) if time allows. `toolset.py` gives the agent a SigV4-signed MCP client for the Gateway (finding 34).
7. **Agent on Runtime:**
   - AgentCore CLI project in `agent/cloud/agentcore/` (CodeZip; remove `mcp.exa.ai`) deploying `agent/app.py` with start/resume per the specs' "Agent service"; tools from `toolset.py`.
   - S3 session manager so interrupts survive between invocations; AgentCore Memory (episodic) if time allows.
   - `InvestigationProgress`/`InvestigationConcluded` events.
   - Session-starter Lambda + EventBridge rules (`AnomalyDetected`, `ApprovalRecorded`, `ReplyReceived`, `PhotoReceived`, `ActionCompleted`).
   - Tracing on.
8. **Approvals + write tools + policy:**
   - `approvals.py`: create a pending record when an interrupt asks; `record_decision()` for the reply handler and report page (only from the person asked, unexpired, conditional on pending, "already decided by …").
   - `actions.py`: work order, scout pin, insurance draft. At most one action per approval; the work order is delivered as a text to the assignee via WS9's send.
   - Write-tool registry entries with required `approval_id`/`investigation_id`; Cedar policy for the Gateway if time allows.
9. **Notifications:** turn `sms.py` into `fieldwatch/notify/`.
   - **Send Lambda** (T17): opt-out, idempotency, signed link, quiet hours with Scheduler deferral, daily cap, providers (Twilio, mirror-only, End User Messaging stub), `fw-messages`, `MessageSent`.
   - **Reply handler** (T18): the **Twilio webhook arrives through the API** (verify the signature), dedupe, person lookup, intents, STOP/HELP, approval matching via WS8's `record_decision`, `ReplyReceived` / `ApprovalRecorded` / `MessageReceived`.
   - People with roles, so work orders reach the repairs person.
11. **Evaluations + hero scenes:** pick the scenes in `docs/hero-scenes.md`; `agent/eval/` runs `contracts.Scenario` files through replay end to end and reports pass/fail; AgentCore Evaluations if time allows.
12. **Web API (T27):** API Gateway HTTP API + Lambda route groups. It serves:
    - the specs' `/v1` routes (onboarding fields, farms/fields, people, reports → `ReportView`, approval decisions, uploads, work orders, overview, investigations, replay/reset, dashboard auth, mirror reply);
    - **the same JSON shapes as today's `agent/web/server.py` `/api/...` routes** (create farm, get farm with thread, check now, ask, reply, field images, status), backed by DynamoDB/S3;
    - `POST /sms/inbound` (calls WS9's reply handler).

    It also handles link-token verification (HMAC key from SSM `/fieldwatch/link-signing-key`) and the error codes from the specs.
13. **Live feed (T28):** AppSync Events API (namespace `fieldwatch`, channels `/farms/{farm_id}/feed` and `/farms/{farm_id}/phone`; IAM publish; subscribe with an API key or a Lambda authorizer on the dashboard token). The **projector** Lambda maps bus events to `FeedMessage`s and is the only publisher. Include a small Node or browser test subscriber.
14. **Front end on CloudFront (T29, T30, T33):** move the UI to `web/` as static files on S3 + CloudFront (origin access control).
    - `config.js` with the API URL and AppSync endpoint, generated at deploy.
    - Keep today's map, drawing and phone-panel UX; the phone panel subscribes to AppSync instead of polling.
    - Add the dashboard pieces (scan sweep, investigation panel with hypotheses and evidence cards), the **report page** (reveal slider, checked list, dollar range, approve/decline) and the **work-order page**, following the UI rules in `docs/core-features.md`.
15. **Context builder workflow (T08):** Step Functions Standard, started by the API after fields are saved. A Map over fields runs LoadBoundary (tile index into `fw-tile-index`), then parallel stages calling WS4 functions (stubs until they land) and WS1's baseline, each writing its own part of `fw-field-context`. Partial failures are allowed; field `status` → `ready`.

## Checkpoints

| When | Must be merged and shown |
| --- | --- |
| +0:45 | PR #1 merged; Kurt and Aaron have AWS access; repo variables set |
| +3h | API + front end serving today's map from CloudFront with farms in DynamoDB · analyzer indices on a summer date · scorer ranks the sample · notify send Lambda with rules · agent on Runtime answering a start request |
| +6h | Replay → scene pipeline → `AnomalyDetected` → session starter → agent → message on the phone mirror, all in AWS · Gateway serving registry tools · approvals + work order delivered · hazards and soil live |
| +9h | Hero scenario end to end in AWS with the report page · evaluation scenarios run · feed drives the dashboard |
| ~6:30 | Feature freeze. If the full path isn't green, demo the fallback branch. Record a backup run either way |

## Prompt template (paste into the workstream's Claude Code session)

> You are working on FieldWatch (repo FarmU), workstream **N: <name>**. Read `CLAUDE.md`, `docs/decisions.md`, `docs/spike-findings.md`, and the plan sections listed for workstream N in `docs/workstreams.md` (plan docs are in `docs/`). Decisions and spike findings override the plan docs. Build against `agent/fieldwatch/contracts.py` and the samples in `agent/fixtures/contracts/`; do not change contracts. Edit only the paths listed for workstream N. Goal: <paste the goal>. First write the checks and tests for that goal (no network or AWS in unit tests: mock boto3 with `moto` or fakes; mark live tests `@pytest.mark.live`), then propose a plan, then implement. Before finishing, run `python -m pytest tests -q -m "not live"` from `agent/` and, if you touched `infra/`, an offline `cdk synth`. Do not deploy anything; Caleb deploys.
