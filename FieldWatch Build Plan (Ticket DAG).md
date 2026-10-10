# FieldWatch: Build Plan (Ticket DAG)

Oct 7, 2026 · @Caleb

Every task from account setup to the pitch, as tickets in a dependency graph that three people can work through in parallel with Claude Code. Tickets marked P happen this week and contain no application code; tickets marked T happen during the 24 hours. Confirm with the organizers that writing these tickets in advance is fine, since the rules ban pre-written user stories and test cases.

## How to work this DAG with Claude Code

**Lanes.** Three owners, each with a lane they drive; anyone can pick up a ready ticket from another lane when theirs is blocked.

| Lane | Owner | Owns |
| --- | --- | --- |
| A: platform and agents | Caleb | Terraform, CI/CD, scene orchestration, Gateway tools, the Strands agent, evaluations |
| B: data and models | Teammate 2 | Static data, context builder, field analyzer, hazards, scorer, uploads |
| C: experience and messaging | Teammate 3 | SMS, web API, live feed, dashboard, phone pages, knowledge base, pitch |

**A ticket is ready** when every ticket it depends on is merged to `main`. Pick the ready ticket on your lane's critical path first.

**The loop for every ticket**

1. `git switch -c t13-field-analyzer` (ticket ID + short name). For a second ticket in parallel, use a separate worktree: `git worktree add ../fw-t16 -b t16-pollers`.
2. Open Claude Code in that directory and paste the ticket's **Claude Code prompt**. It points at the spec sections in `docs/`, which Claude Code reads itself.
3. Let it plan first (ask for a plan, check it against **Do**). If the ticket has a **Goal** instead of a checklist, write its concrete checks and tests first (inputs and expected results), then implement.
4. Run the ticket's **Done when** milestones or the checks you wrote from its **Goal** locally; anything touching AWS deploys to the shared environment from your branch with `make deploy-<module>` only if the ticket says so.
5. Open a PR titled `T13: Field analyzer`; CI must be green. Review: another teammate approves within 10 minutes, or the author self-merges if the change doesn't touch `schemas/` or `infra/core/`. Squash merge.
6. Post "T13 merged" in the team chat so dependents know they're unblocked.

**Contract first.** T04 (shared schemas, with fixture files for every event and tool response) merges by 10:45 Saturday. Until an upstream ticket merges, build against its fixtures: the dashboard against fixture feed messages, the agent against a fixture `FieldAnalysisResult`, and so on. This is what makes three lanes truly parallel.

**Repo layout (created in T01)**

```text
fieldwatch/
  CLAUDE.md          # instructions Claude Code reads automatically
  docs/              # the design docs, exported to Markdown at kickoff
  schemas/           # shared types (TypeScript + generated Pydantic) and fixtures
  infra/             # Terraform: core/, imagery/, hazards/, agents/, web/, messaging/
  services/          # Python Lambdas, one folder each
  agent/             # Strands app for AgentCore Runtime
  web/               # Vite + React + TypeScript: dashboard and phone pages
  data-prep/         # one-off scripts: static slices, seed farm, corpus ingest
  eval/              # scenario files and the evaluation runner
```

**Conventions for CLAUDE.md (T01 writes the file from these)**

- Point Claude Code at the docs in `docs/`: system design (architecture), implementation specs (every interface; the contract), data pipelines, core features, build plan (tickets).
- Names, payloads and events follow the implementation specs exactly; a needed change goes to `schemas/` first, in its own PR.
- Python 3.12 with uv, ruff and pytest; each Lambda in `services/<name>/` with a handler and its tests.
- TypeScript in strict mode with pnpm; `web/` uses Vite, React and MapLibre.
- Terraform per module in `infra/<module>/`; everything tagged `project=fieldwatch`.
- Region us-west-2 (only the MRMS forwarder in us-east-1). The agent image is ARM64 (AgentCore Runtime requires it), built in CodeBuild by the starter toolkit; Lambdas, including container Lambdas, are x86_64 so they build natively on team laptops.
- No secrets in code: API keys in AgentCore Identity, the link-signing key in SSM.
- Every PR: tests for new logic, `make lint test` passes, fixtures updated if schemas change.

## The DAG

The diagram shows the graph at the level of epics; the index below lists every ticket and its exact dependencies. Windows are targets, Saturday 9:30 a.m. to Sunday 8:00 a.m.

&#91;embedded content: epic-level DAG · critical path in color; Foundation also feeds every epic\]

Foundation (T01–T06) feeds every epic; only the edges that sequence the work are drawn. Tools and the scorer must both land before the agent, and the knowledge base (dashed) improves the agent but doesn't block it.

| ID | Ticket | Lane | Est. | Window | Depends on | Tier |
| --- | --- | --- | --- | --- | --- | --- |
| P1 | AWS account, access, model access, budgets | A | 1.5h | Wed–Thu | none | MVP |
| P2 | SMS registration and demo phones | C | 1h + wait | Wed | none | MVP |
| P3 | Data access verification | B | 2h | Wed–Thu | none | MVP |
| P4 | Hero events and scenario hunt | B + Caleb | 3h | Wed–Fri | P3 | MVP |
| P5 | Corpus source sheet and photo picks | C | 2h | Thu | none | MVP |
| P6 | Questions for the organizers | C | 0.5h | Wed | none | MVP |
| P7 | Learning spikes (throwaway) | All | 2–3h each | Wed–Fri | P1 | MVP |
| P8 | Repo and conventions plan (written) | A | 1h | Fri | P7 | MVP |
| T01 | Repo scaffold, CLAUDE.md, docs import | A | 45m | Sat 9:30 | P8 | MVP |
| T02 | Terraform bootstrap | A | 45m | Sat 9:45 | P1, T01 | MVP |
| T03 | Core data plane | A | 45m | Sat 10:15 | T02 | MVP |
| T04 | Shared schemas and fixtures | B | 1h | Sat 9:45 | T01 | MVP |
| T05 | Lambda platform and container base images | A | 1h | Sat 10:45 | T02 | MVP |
| T06 | CI/CD | A | 45m | Sat 11:45 | T02, T04 | MVP |
| T07 | Static data prep | B | 1.5h | Sat 10:45 | T03, P3 | MVP |
| T08 | Context builder skeleton and tile index | B | 1h | Sat 11:00 | T03, T04, T05 | MVP |
| T09 | Context stages: soil, terrain, water, wells | B | 1.5h | Sat 13:00 | T07, T08 | MVP |
| T10 | Context stages: crop, pivot, stage, economics | B | 1h | Sat 14:30 | T08 | MVP |
| T11 | Baseline stage | B | 1.5h | Sat 12:00 | T05, T08 | MVP, critical |
| T12 | Demo farm seed | B | 30m | Sat 13:30 | T08, P4 | MVP |
| T13 | Field analyzer | B | 3h | Sat 13:30 | T04, T05, T11 | MVP, critical |
| T14 | Scene orchestration and replay API | A | 2h | Sat 11:30 | T03, T04, T05 | MVP |
| T15 | Hail lane | B | 2h | Sat 20:00 | T03, T05 | MVP |
| T16 | Pollers (drought; NWS and storm reports should) | B | 1h | Sat 22:00 | T03 | MVP |
| T17 | Send message and phone mirror | C | 1.5h | Sat 10:30 | T03, T04 | MVP |
| T18 | Inbound replies and mirror reply | C | 1.5h | Sat 12:00 | T17 | MVP |
| T19 | Gateway and read tools | A | 2.5h | Sat 13:30 | T03, T04 | MVP, critical |
| T20 | Scorer, condition catalog, impact model | B | 2.5h | Sat 16:30 | T04, P5 | MVP, critical |
| T21 | Identity and OpenAPI targets | A | 1h | Sat 21:00 | T19 | MVP |
| T22 | Write tools, approvals and policy | A | 2h | Sat 19:00 | T19, T17 | MVP |
| T23 | Strands agent on Runtime and session starter | A | 3h | Sat 16:00 | T19, T20 | MVP, critical |
| T24 | Memory and observability | A | 1h | Sat 22:00 | T23 | MVP |
| T25 | Knowledge base and search tool | C | 2h | Sat 19:00 | P5, T03, T19 | MVP |
| T26 | Disease snapshot and reference photos | B | 30m | Sat 19:30 | P5, T03 | MVP |
| T27 | Web API | C | 2h | Sat 13:30 | T03, T04 | MVP |
| T28 | Live feed: AppSync Events and projector | C | 1.5h | Sat 15:30 | T03, T04 | MVP |
| T29 | Dashboard | C | 3.5h | Sat 17:00 | T27, T28 | MVP |
| T30 | Phone pages | C | 3h | Sat 21:00 | T27 | MVP |
| T31 | Upload intake | B | 1.5h | Sun 00:00 | T27, T25 | MVP |
| T32 | Evaluation suite | A | 2h | Sat 23:00 | P4, T14, T18, T23 | MVP, critical |
| T33 | Agents tab | C | 1h | Sun 01:00 | T29, T24 | Should |
| T34 | Live Sentinel-2 subscription | A | 30m | Sun 02:00 | T14 | Should |
| T35 | Demo operations and feature freeze | All | 1.5h | Sun 6:30 | T32, T29, T30 | MVP, critical |
| T36 | Pitch script and rehearsal | C | 1.5h | Sun 03:00 | P4, T29 | MVP |
| T37 | Voice agent | A | 3h | if ahead | T23, T22 | Stretch |
| T38 | Document upload (soil tests) | B | 2h | if ahead | T31 | Stretch |
| T39 | Code Interpreter for the imagery analyst | A | 1h | if ahead | T23 | Stretch |
| T40 | Extra sensors (Sentinel-1) | B | 2h | if ahead | T13 | Stretch |

## Pre-event tickets (this week, no application code)

### P1 · AWS account, access, model access, budgets

Lane A · 1.5h · Wed–Thu · depends on: none

1. One AWS account for the team (or an AWS-provided hackathon account if offered); enable IAM Identity Center and create three users with admin permission sets.
2. In us-west-2, request Bedrock model access for the models you'll try: a Claude Sonnet-class and Haiku-class model, Amazon Nova Lite/Pro, Nova 2 Sonic (stretch), and an embeddings model for the knowledge base.
3. Check quotas that could bite at 2 a.m.: Lambda concurrent executions, Bedrock tokens per minute for the chosen models, AgentCore Runtime limits. Request increases now.
4. AWS Budgets: alerts at two thresholds the team agrees on, emailed to all three.
5. GitHub: create an empty private repo `fieldwatch` (README only) and add teammates; confirm everyone has Claude Code working on their laptop.

Done when:

- [ ] All three can sign in to the console and the CLI (`aws sts get-caller-identity`)
- [ ] A test Bedrock call succeeds in us-west-2 for each chosen model
- [ ] Budget alerts exist; quota requests submitted

### P2 · SMS registration and demo phones

Lane C · 1h plus waiting · Wed · depends on: none

The plan is to stay in the End User Messaging **SMS sandbox** and text only the team's own phones. What the sandbox does and doesn't remove:

- **Removes:** the production-access request. In the sandbox you can text up to 10 **verified destination numbers**; each is verified by entering a code sent to that phone, which takes minutes.
- **Doesn't remove:** the US rule that sending needs an active **origination identity** (a toll-free number or 10DLC registration). Toll-free registration can take up to 15 business days. Without one, only AWS's **simulator numbers** work, and they can't reach a real phone or verify one.

Steps:

1. Check whether the account already has an active US origination identity. If it does, verify Caleb's and a teammate's phones as sandbox destinations and enable two-way messaging to an SNS topic.
2. If it doesn't, request a toll-free number and submit its registration now anyway (opt-in description from the Core Features doc), and plan the demo on the phone mirror (`SMS_ENABLED=false`, `mirror_only`). Simulator numbers can still exercise the real send API path during the build.
3. Decide which phones play the farmer (Caleb's) and the son (a teammate's), and how each is mirrored to the projector.
4. Ask the organizers whether requesting a phone number and registration before the event is fine (P6). It's account setup, not code, but it's worth a yes in writing.

Done when:

- [ ] Origination identity status known and written in the team notes; destination phones verified if an identity is active
- [ ] Demo phone roles and mirroring method decided

### P3 · Data access verification

Lane B · 2h · Wed–Thu · depends on: none

Work through "Verify this week" in the pipelines doc and the open items in the implementation specs, by hand only (browser, CLI, docs):

- [ ] The Nebraska slice of Crop Sequence Boundaries is reachable; the demo fields exist as separate polygons
- [ ] Earth Search returns cloud-free summer 2026 scenes over the farm
- [ ] MRMS archive depth (the `aws s3 ls` check)
- [ ] Soil Data Access, Open-Meteo history, Drought Monitor and 3DEP all answer for the county
- [ ] A NASS Quick Stats API key obtained

### P4 · Hero events and scenario hunt

Lane B with Caleb · 3h · Wed–Fri · depends on: P3

1. Ask the family what went wrong this summer (dates, fields, what they found).
2. In a STAC browser, look for clear scenes before and after each event; pick the hero field and date, two secondary flagged fields, one normal field and one late-season dry-down date.
3. Check storm reports for any hail date you'll use.

Done when:

- [ ] A one-page note listing five scenarios: field, scene ID(s), what happened, how you know

### P5 · Corpus source sheet and photo picks

Lane C · 2h · Thu · depends on: none

1. Build the source sheet: URL, title, publisher, year, crop, condition, stage, license terms (40–80 documents: Nebraska Extension, Crop Protection Network, USDA RMA loss adjustment handbooks).
2. Note where the loss tables live (hail by growth stage; water stress around silking).
3. Pick 15–30 licensed disease photos (Wikimedia Commons, Creative Commons datasets, extension images with permission); record license and attribution.

Done when:

- [ ] Source sheet complete with license notes; photo list with attributions

### P6 · Questions for the organizers

Lane C · 30m · Wed · depends on: none

Email: which awards a student team can win; whether downloading public datasets in advance is allowed; whether a written task plan like this one is fine; whether deploying to a cloud account created beforehand is fine (with no app code in it). Record answers in team notes.

### P7 · Learning spikes (throwaway)

All · 2–3h each · Wed–Fri · depends on: P1

Learn, build the official tutorial, tear down. A spike is worth running only where the service is new to the team, sits on the critical path, and a surprise at 2 a.m. would cost hours. Everything else is a docs check.

**Rules for every spike (fresh-code safe):**

- Official AWS/Strands tutorials and samples only, in a scratch folder outside this repo with generic names (no "fieldwatch").
- Nothing FieldWatch-specific: no farm logic, schemas, prompts or tools from the plan docs.
- Tag every resource `spike=true`; tear it all down the same day and confirm in Tag Editor and Billing.
- Keep notes in prose only: versions, timings, gotchas, links. No code snippets carried over; delete the scratch folder.

**Tier 1: run these (critical path, new to the team)**

| # | Spike | Learn | Protects |
| --- | --- | --- | --- |
| S1 | Strands quickstart agent deployed to AgentCore Runtime with the starter toolkit; invoke with a 33+ character session ID; try a background task with a `HealthyBusy` ping | Deploy time, cold start, the IAM role it creates, where logs and traces land, how sessions resume | T23 |
| S2 | Gateway tutorial: one Lambda target, IAM inbound auth; list and call the tool from a Strands agent; one agent calling another as a tool | The `target___tool` name prefix, the Lambda event shape, signing calls from Strands, agents-as-tools wiring | T19, T23 |
| S3 | Confirm the starter toolkit builds the ARM64 agent image in CodeBuild (no local ARM64 build); optionally build a tiny x86_64 rasterio container Lambda from a laptop and time it | That no laptop ever needs to build ARM64 (Lambdas are x86_64), and how long a geo image build takes | T05, T11, T13, T23 |
| S4 | Model access: one Bedrock call per chosen model in us-west-2; record the tokens-per-minute quotas | That each model answers and quotas fit three parallel specialists | T23 (part of P1, minutes) |

**Tier 2: run if time allows (fallback exists)**

| # | Spike | Learn | Fallback if it fails Saturday |
| --- | --- | --- | --- |
| S5 | AppSync Events: console-created API, publish from the CLI with IAM, subscribe from a browser sample page | Auth modes, message size, browser client setup | API-key subscribe for the demo |
| S6 | AgentCore Evaluations tutorial run over S1's session | Built-in evaluator names; how custom evaluators get per-scenario expected values (open item) | A plain script that scores scenarios |
| S7 | `terraform init` on the current AWS provider; confirm the `aws_bedrockagentcore_*` resources and attributes the plan needs; `plan` only | Provider coverage before committing T19/T23 to Terraform | Starter toolkit or CDK for AgentCore pieces |
| S8 | rasterio windowed read of one Earth Search COG, and DuckDB spatial on the CSB slice, locally | Read speed, auth-free access, query syntax | None needed; this overlaps P3 |

**Tier 3: docs check only (cut-safe or low novelty)**

- AgentCore Policy (Cedar): the handler approval check is the real enforcement.
- AgentCore Memory episodic strategy (cut line).
- Identity API-key provider on an OpenAPI target: header vs. query parameter (cut line: Lambda versions).
- cfgrib on one MRMS file: the hail lane is cuttable; a local install check is enough.
- End User Messaging: origination identity status (P2); optionally one simulator-number send.
- MapLibre satellite basemap: tile license terms.
- Earth Search SNS: confirm a subscription is accepted, then unsubscribe.
- Skip entirely: Amazon Connect and Nova Sonic (voice is cut first).

### P8 · Repo and conventions plan (written)

Lane A · 1h · Fri · depends on: P7

Confirm the repo layout, CLAUDE.md text and tooling choices in this doc against what the spikes taught; write down the Makefile targets each module needs. Export the design docs to Markdown so T01 can drop them into `docs/`.

## Foundation (Saturday 9:30–12:00)

### T01 · Repo scaffold, CLAUDE.md, docs import

Lane A · 45m · Sat 9:30 · depends on: P8 · unblocks: everything

Do:

1. Create the layout from "How to work this DAG"; write CLAUDE.md from the conventions listed there; drop the exported docs into `docs/`.
2. Tooling: `uv` workspace for Python (ruff, pytest), `pnpm` workspace for `web/` and `schemas/`, a root Makefile with `lint`, `test`, `deploy-<module>`.
3. PR template with: ticket ID, what changed, how it was tested, schema changes (yes/no).

Done when:

- [ ] `make lint test` passes on an empty project; teammates can clone and run it

Claude Code prompt:

> Scaffold the FieldWatch monorepo exactly as laid out in docs/build-plan.md ("How to work this DAG"), write a CLAUDE.md from the conventions listed in that section, and set up uv (ruff, pytest), pnpm, and a root Makefile with lint, test and deploy-\<module> targets. Keep it minimal; no application code.

### T02 · Terraform bootstrap

Lane A · 45m · Sat 9:45 · depends on: P1, T01 · unblocks: T03, T05, T06

Do:

1. State backend: S3 bucket with versioning and native S3 locking (or a DynamoDB lock table).
2. Providers: `aws` in us-west-2 plus an aliased us-east-1 provider; default tags `project=fieldwatch`.
3. GitHub OIDC provider and a deploy role scoped to this account for CI.
4. ECR repositories: `field-analyzer`, `hail-filter`, `intake`, `agent`, `baseline`.
5. Import the SNS topic and phone number created in P2.

Done when:

- [ ] `terraform plan` and `apply` run cleanly from a laptop and from the OIDC role

Claude Code prompt:

> In infra/bootstrap and infra/core, set up Terraform state, providers (us-west-2 default, us-east-1 alias), default tags, a GitHub OIDC deploy role for this repo, and the ECR repositories listed in docs/build-plan.md T02. Import the existing SNS topic for inbound SMS.

### T03 · Core data plane

Lane A · 45m · Sat 10:15 · depends on: T02 · unblocks: most of the graph

Do: S3 bucket with the prefixes and lifecycle rules; all DynamoDB tables with keys, GSIs and TTLs; the `fieldwatch` EventBridge bus with a schema registry; SSM parameter for the link-signing key (generated). All from the "Data model" section of the implementation specs.

Done when:

- [ ] Every table, GSI and TTL in the spec exists; outputs export names and ARNs for other modules

Claude Code prompt:

> Implement infra/core per docs/implementation-specs.md "Data model" and "Event catalog": the S3 bucket with lifecycle rules, every DynamoDB table (on-demand) with its GSIs and TTLs, the fieldwatch EventBridge bus and schema registry, and an SSM SecureString for the link-signing key. Export outputs for other modules.

### T04 · Shared schemas and fixtures

Lane B · 1h · Sat 9:45 · depends on: T01 · unblocks: almost every service ticket

Do:

1. Write the TypeScript types in `schemas/ts/` from the field tables in the implementation specs: basic formats and shared types, events, SceneAnalysisInput, FieldAnalysisRequest/Result, FieldContext, AgentRequest/ContextBundle/AgentResult, specialist and supervisor contracts, SendMessageRequest/Result, the Gateway tool inputs and outputs, ReportView, FeedMessage, PhotoResult/DocumentResult and the Scenario file. The specs hold field tables only; no type code exists before the event.
2. Generate JSON Schema from them, then Pydantic models into `schemas/py/` (a `make schemas` target).
3. Helpers: prefixed ULIDs, deterministic IDs (anomaly, investigation, session), an idempotency helper for `fw-idempotency`, an EventBridge publish helper.
4. Fixtures in `schemas/fixtures/`: one valid example of every event, tool response and API view, using the hero scenario's shape.

Done when:

- [ ] Every fixture validates against its schema in a test; Python and TS both import the types

Claude Code prompt:

> Create schemas/ from the field tables in docs/implementation-specs.md: write TypeScript types for every shared type, event, tool contract and API view; generate JSON Schema and Pydantic models via a make target; add ID, idempotency and event-publishing helpers; and write one realistic fixture per type (based on the north-pivot hero scenario) with tests that validate each fixture.

### T05 · Lambda platform and container base images

Lane A · 1h · Sat 10:45 · depends on: T02 · unblocks: T07–T16, T19

Do:

1. A Terraform module `infra/modules/lambda` for zip and container Lambdas: x86_64, log retention 7 days, reserved concurrency, DLQ, X-Ray, IAM role per function with least-privilege policy inputs.
2. Base images (x86_64): `geo` (GDAL, rasterio, rioxarray, numpy, shapely, scikit-image, exactextract) and `grib` (eccodes, cfgrib, xarray); push to ECR.

Done when:

- [ ] A hello-world zip Lambda and a hello-world `geo` container Lambda deploy and run through the module

Claude Code prompt:

> Build infra/modules/lambda (zip and container variants, x86_64, 7-day logs, reserved concurrency, DLQ, X-Ray, per-function role) and two x86_64 base images in services/\_images: geo (GDAL, rasterio, rioxarray, numpy, shapely, scikit-image, exactextract) and grib (eccodes, cfgrib, xarray). Prove both with hello-world functions.

### T06 · CI/CD

Lane A · 45m · Sat 11:45 · depends on: T02, T04 · unblocks: safe merging for everyone

Do: GitHub Actions on PR: lint, tests, `terraform fmt` and `validate`, and a `terraform plan` comment. On merge to `main`: build and push changed images, then `terraform apply` for changed modules. One environment only.

Done when:

- [ ] A trivial PR shows green checks and a plan comment; merging deploys

Claude Code prompt:

> Add GitHub Actions per docs/build-plan.md T06: PR checks (ruff, pytest, eslint, tsc, terraform fmt/validate, plan comment via the OIDC role) and a main-branch deploy that builds changed images and applies changed Terraform modules.

## Data and context (lane B, Saturday morning–afternoon)

### T07 · Static data prep

Lane B · 1.5h · Sat 10:45 · depends on: T03, P3 · unblocks: T09

Do: a `data-prep/static.py` script that writes Nebraska (or demo-county) slices to `static/` in S3: Crop Sequence Boundaries (DuckDB spatial filter to GeoParquet), Cropland Data Layer for the last five years, gNATSGO soils (or a Soil Data Access fallback), hydrography, wetlands, flood zones, DNR wells. Idempotent; logs sizes.

Done when:

- [ ] Every dataset is in `static/` and readable from a Lambda; the demo fields are present in the boundary slice

Claude Code prompt:

> Write data-prep/static.py per docs/data-pipelines.md section A and the S3 layout in docs/implementation-specs.md: download and slice each static dataset to the demo county (DuckDB spatial for GeoParquet, rasterio for rasters) and upload to static/. Make it rerunnable and print a summary.

### T08 · Context builder skeleton and tile index

Lane B · 1h · Sat 11:00 · depends on: T03, T04, T05 · unblocks: T09–T12

Do: the Standard state machine from the spec with the `LoadBoundary` stage implemented (normalize geometry, acres, centroid, interior buffer, Sentinel-2 grid tiles into `fw-tile-index`); every other stage a stub that writes an empty sub-document; partial-failure handling; `status` flips to `ready`.

Done when:

- [ ] Starting the machine for a fixture field produces a FieldContext item and tile-index rows

Claude Code prompt:

> Implement the context builder per docs/implementation-specs.md "Context builder": Terraform in infra/context, the Standard state machine (Map over fields, Parallel stages), LoadBoundary fully implemented (including grid tiles in fw-tile-index), and the other stages as stubs writing empty sub-documents. Partial failures must not fail the field.

### T09 · Context stages: soil, terrain, water, wells

Lane B · 1.5h · Sat 13:00 · depends on: T07, T08

Do: implement `Soil` (map units, horizon summaries, soil-zone raster), `Terrain` (slope, low-spot mask), `Water` (distances and shares) and `Wells` per the FieldContext schema.

Done when:

- [ ] The hero field's FieldContext has soil, terrain, water and wells populated; rasters exist in `context/`

Claude Code prompt:

> Implement the Soil, Terrain, Water and Wells stages of the context builder per docs/data-pipelines.md A4–A7 and the FieldContext schema in docs/implementation-specs.md, reading from static/ in S3. Add unit tests with a small fixture polygon.

### T10 · Context stages: crop, pivot, stage, economics

Lane B · 1h · Sat 14:30 · depends on: T08

Do: `Crop` (CDL history plus rotation prediction), `Pivot` (circularity and center/radius fit), `Stage` (Crop Progress history), `Economics` (county yields and price; call `get_county_stats` once T21 merges, using a fixed fallback value until then).

Done when:

- [ ] The hero field shows the right crop, pivot geometry and an economics block with sources

Claude Code prompt:

> Implement the Crop, Pivot, Stage and Economics stages per docs/data-pipelines.md A2, A3, A9, A10 and the FieldContext schema. Economics should call the get\_county\_stats Gateway tool when GATEWAY\_URL is set and otherwise use a configured fallback.

### T11 · Baseline stage

Lane B · 1.5h · Sat 12:00 · depends on: T05, T08 · critical path

Do: a container Lambda that searches past Sentinel-2 scenes for the field, masks clouds, computes NDVI/NDRE/NDMI per zone, fits seasonal curves from prior seasons only, stores the current season separately (the "as of" rule), and picks a same-crop cohort.

Done when:

- [ ] `baseline.parquet` exists for every demo field with prior-season curves and current-season points

Claude Code prompt:

> Implement the Baseline stage per docs/data-pipelines.md A8 and the Models section M1, as a geo-image container Lambda. Prior seasons build the expected curve per zone; the current season is stored separately so the analyzer can use only scenes before a given date (see "Replay correctness" in docs/implementation-specs.md).

### T12 · Demo farm seed

Lane B · 30m · Sat 13:30 · depends on: T08, P4

Do: `data-prep/seed_farm.py` creates the demo farm, its people (team phone numbers, roles, the grandfather preferring calls), and fields from the chosen boundary IDs with made-up landlord-style names, then starts the context builder. A `--reset` flag removes everything it created.

Goal: the demo farm can be seeded and reset repeatedly, and its fields end up ready for analysis.

## Imagery and hazards

### T13 · Field analyzer

Lane B · 3h · Sat 13:30 · depends on: T04, T05, T11 · unblocks: T19, T32 · critical path

Do: the container Lambda from "Field analyzer" in the implementation specs: windowed band reads, resampling, cloud and interior-buffer masks, NDVI/NDRE/NDMI, anomaly scores against baseline (scenes before the scene date only), cohort and change-since-last, region extraction, pattern features (ring, wedge, edge band, stripes, low-spot, sandy-soil and hail-swath overlap), the three gates, overlay PNGs with bounds, and `result.json`.

Goal: the analyzer picks out real stress patterns, stays quiet on healthy and naturally drying fields, and is fast enough for the 60-second latency budget.

Claude Code prompt:

> Implement services/field\_analyzer per the FieldAnalysisRequest/FieldAnalysisResult contract in docs/implementation-specs.md, the B2 steps in docs/data-pipelines.md, and models M2–M3. Use the geo base image. Add tests for the pattern features.

### T14 · Scene orchestration and replay API

Lane A · 2h · Sat 11:30 · depends on: T03, T04, T05 · unblocks: T32, T34

Do: the scene queue, scene filter, Express state machine (`LoadFieldInputs`, `AnalyzeField`, `Decide`), decision Lambda, and `POST /v1/replay` and `/v1/replay/reset` (including the hail backfill publish). Use a stub analyzer returning the fixture until T13 merges.

Goal: a replay drives the real pipeline end to end (with the stub analyzer), follows the idempotency and "as of" rules in the specs, and can be reset between demo runs.

Claude Code prompt:

> Implement the imagery pipeline orchestration per docs/implementation-specs.md "Imagery pipeline" and "Replay correctness": infra/imagery with the SQS queue, scene filter Lambda (grid:code tile lookup, cloud rule, idempotency), Express state machine, decision Lambda with thresholds, and the replay/reset endpoints. Wire AnalyzeField to a stub that returns schemas/fixtures until the real analyzer exists.

### T15 · Hail lane

Lane B · 2h · Sat 20:00 · depends on: T03, T05

Do: the MRMS forwarder in us-east-1 (prefix filter), the hail filter container Lambda (GRIB2 read, clip, per-field max size and share, swath raster and PNG, watches, `WatchOpened`), and handling of replay backfill events.

Goal: real radar hail data opens watches on the fields it hit.

Claude Code prompt:

> Implement the hazard pipeline's MRMS forwarder (us-east-1, prefix CONUS/MESH\_Max\_60min\_00.50/) and hail filter (grib image) per docs/implementation-specs.md "Hazard pipeline", with tests.

### T16 · Pollers

Lane B · 1h · Sat 22:00 · depends on: T03

Do: the weekly drought poller (MVP). If time allows: the NWS alert poller and the daily storm-report poller (should-have).

Done when:

- [ ] The demo county's drought category appears in FieldContext with its date

## Messaging (lane C)

### T17 · Send message and phone mirror

Lane C · 1.5h · Sat 10:30 · depends on: T03, T04 · unblocks: T18, T22

Do: the send Lambda from "Messaging" in the implementation specs, with the rules in order (opt-out, idempotency, signed link, quiet hours with Scheduler deferral, daily cap, send or mirror-only, `fw-messages`, `MessageSent`), plus pending-approval creation when `approval` is set. `SMS_ENABLED` defaults to false until P2's registration clears.

Goal: messages reach the phone mirror (and a real phone once an origination identity is active), following every send rule in the specs.

Claude Code prompt:

> Implement services/send\_message per docs/implementation-specs.md "Messaging → Send" (SendMessageRequest/SendMessageResult), including link signing (key from SSM), quiet hours in the farm's time zone with EventBridge Scheduler deferral, approval creation, idempotency via fw-idempotency, and the SMS\_ENABLED switch, with tests.

### T18 · Inbound replies and mirror reply

Lane C · 1.5h · Sat 12:00 · depends on: T17 · unblocks: T32

Do: the reply handler (SNS subscription; dedupe on `inboundMessageId`; person lookup by phone; keyword intents; STOP/HELP; approval matching by `previousPublishedMessageId` with fallback; `ApprovalRecorded` only for approval replies, `ReplyReceived` otherwise; `MessageReceived`), and `POST /v1/mirror/reply` routing through the same code.

Goal: replies from SMS or the mirror are classified and routed exactly as the reply-handler rules in the specs describe.

Claude Code prompt:

> Implement services/reply\_handler per docs/implementation-specs.md "Reply handler" using the two-way SMS payload fields listed there, and a /v1/mirror/reply entry point that calls the same function, with tests.

## Tools, scoring and the agent

### T19 · Gateway and read tools

Lane A · 2.5h · Sat 13:30 · depends on: T03, T04 · unblocks: T21–T23, T25 · critical path

Do: the AgentCore Gateway (IAM inbound auth) in Terraform; one Lambda target per tool group with inline tool schemas generated from `schemas/`; read tools `get_field_context`, `get_scene_analysis`, `get_index_history`, `compare_to_cohort`, `get_weather` (Open-Meteo, cached per field and day), `get_hazards`, `test_overlap`, `get_disease_reports`. Strip the `target___` prefix from tool names in handlers.

Done when:

- [ ] Listing tools through the Gateway shows every read tool with its schema
- [ ] Each tool returns its fixture-shaped response for the hero field, in under 3 seconds

Claude Code prompt:

> Implement infra/agents/gateway and services/tools\_read per docs/implementation-specs.md "Gateway tools" and the confirmed Gateway Lambda-target behavior noted there. Generate the inline tool schemas from schemas/. Include a small MCP client script (eval/tools\_smoke.py) that lists tools and calls each one.

### T20 · Scorer, condition catalog, impact model

Lane B · 2.5h · Sat 16:30 · depends on: T04, P5 · unblocks: T23 · critical path

Do:

1. Author `catalog/conditions.json` (versioned): for each condition, priors by crop, stage and irrigation; the evidence keys with weights and a cited rationale; actions. Caleb reviews the agronomy.
2. Author `catalog/loss_tables.json` from the RMA handbooks and extension guides (hail by stage; water stress around silking).
3. `score_hypotheses` (log-odds, with `include_analysis` turning pattern features and watches into evidence) and `estimate_impact` (range output) as Lambda tools.

Goal: the scorer ranks conditions sensibly from evidence, and the impact model gives a believable dollar range that explains its inputs.

Claude Code prompt:

> Implement services/scorer and services/impact per models M4–M5 in docs/data-pipelines.md and the tool contracts in docs/implementation-specs.md. Load catalog/conditions.json and catalog/loss\_tables.json from S3 (version recorded on each result). Add tests.

### T21 · Identity and OpenAPI targets

Lane A · 1h · Sat 21:00 · depends on: T19

Do: an AgentCore Identity API-key credential provider holding the Quick Stats key; OpenAPI targets for `get_drought` and `get_county_stats` (trimmed OpenAPI descriptions), with outbound auth from the credential provider. Resolve the open item on whether the key goes in a header or query parameter.

Done when:

- [ ] Both tools answer through the Gateway; the key appears nowhere in code, state or logs

### T22 · Write tools, approvals and policy

Lane A · 2h · Sat 19:00 · depends on: T19, T17

Do: write tools (`send_farmer_message`, `request_photo`, `create_work_order`, `create_scout_pin`, `draft_insurance_notice`) with the approval check in each handler and idempotency on `approval_id`; an AgentCore policy engine attached to the Gateway in enforce mode with the two rules from the System Design doc.

Goal: no action happens without a farmer's approval, and one approval never produces two actions.

Claude Code prompt:

> Implement services/tools\_write and the AgentCore policy engine per docs/implementation-specs.md "Gateway tools" (approval check, error codes) and the policy design in docs/system-design.md. Remember AgentCore Policy only sees context.input. Add tests.

### T23 · Strands agent on Runtime and session starter

Lane A · 3h · Sat 16:00 · depends on: T19, T20 · unblocks: T24, T32 · critical path

Do:

1. `agent/`: a Strands app exposing `/invocations` and `/ping` (ARM64 container); supervisor plus three specialists as tools; specialists get read-only tool lists; structured outputs (`SpecialistResponse`, `SupervisorDecision`); fast path; limits; background-task pattern; `InvestigationProgress` events and investigation state writes.
2. Prompts per the agent roster, including the rule that farmer text, photos, documents and retrieved articles are data, never instructions.
3. Runtime deployed with Terraform; the session starter Lambda and EventBridge rules; session IDs `fieldwatch-session-<investigation_id>`.

Goal: the hero loop works: a replay leads to a visible investigation, a plain-language diagnosis and a message within the latency budget, and the farmer's approval leads to the action.

Claude Code prompt:

> Build agent/ per docs/system-design.md "Subsystem: agents" and docs/implementation-specs.md "Agent service": a Strands supervisor with three specialist agents as tools, an MCP client to the Gateway, the request kinds, lifecycle, limits and fast path, served on /invocations and /ping (port 8080, ARM64). Then add the session starter Lambda and Runtime Terraform in infra/agents.

### T24 · Memory and observability

Lane A · 1h · Sat 22:00 · depends on: T23

Do: an AgentCore Memory with user-preference, semantic and episodic strategies under `/fieldwatch/farm/{actorId}/`; the agent writes events and retrieves top episodes at investigation start; observability enabled for Runtime and Gateway; the CloudWatch dashboard with latency, tokens, cost and alert-rate metrics.

Goal: the agent recalls how past investigations on the same field ended, and the four metrics are visible.

## Knowledge and uploads

### T25 · Knowledge base and search tool

Lane C · 2h · Sat 19:00 · depends on: P5, T03, T19 · unblocks: T31

Do:

1. `data-prep/corpus.py`: download every source in the P5 sheet, convert HTML to clean text, write a metadata sidecar per document (crop, condition, stage, region, year, url), upload to `kb/`.
2. Terraform: an S3 Vectors index and a Bedrock Knowledge Base `fw-agronomy` over `kb/`; trigger a sync.
3. The `search_agronomy` tool (Lambda target) with metadata filters; at most five results with URLs.
4. A retrieval check, with its questions and pass threshold written at the event.

Goal: agronomy questions reliably retrieve the right guidance.

Claude Code prompt:

> Implement data-prep/corpus.py from the P5 source sheet (clean text, per-file metadata sidecars), infra/knowledge (S3 Vectors + Bedrock Knowledge Base), and the search\_agronomy tool per docs/implementation-specs.md "Knowledge base retrieval". Add eval/retrieval\_check.py that reports hit rate on a question list.

### T26 · Disease snapshot and reference photos

Lane B · 30m · Sat 19:30 · depends on: P5, T03

Do: load the county disease-report snapshot (source and date per row) where `get_disease_reports` reads it; upload the licensed reference photos with an attribution file to `static/photos/`.

Done when:

- [ ] `get_disease_reports` returns rows for the demo county; every photo has a license line

### T31 · Upload intake

Lane B · 1.5h · Sun 00:00 · depends on: T27, T25

Do: the intake container Lambda from the implementation specs: validation, metadata stripping, HEIC conversion, downscale, sharpness check, distance to the anomaly region, a multimodal model call grounded with retrieved symptom guides returning `PhotoResult`, `fw-uploads` status, and `PhotoReceived`.

Goal: a farmer's photo becomes useful evidence that moves the investigation forward, and unusable photos get a retake request.

Claude Code prompt:

> Implement services/intake per docs/implementation-specs.md "Upload intake" (photos only; documents are stretch T38), with structured output validated against PhotoResult and retrieval from the fw-agronomy knowledge base filtered by crop and stage. Add tests.

## Web and live feed (lane C)

### T27 · Web API

Lane C · 2h · Sat 13:30 · depends on: T03, T04 · unblocks: T29–T31

Do: API Gateway HTTP API with every route in "Web API and live feed" except replay (T14) and mirror reply (T18); token verification middleware (scopes, expiry, error codes); dashboard passcode exchange; presigned overlay URLs in `ReportView`; presigned upload URLs; CORS for the CloudFront domain.

Goal: every route serves the seeded farm, and link tokens are enforced as the specs describe.

Claude Code prompt:

> Implement infra/web/api and services/web\_api per docs/implementation-specs.md "Web API and live feed" (routes table, ReportView, error codes, link-token scopes). One Lambda per route group, Pydantic validation from schemas/. Add tests.

### T28 · Live feed: AppSync Events and projector

Lane C · 1.5h · Sat 15:30 · depends on: T03, T04 · unblocks: T29

Do: an AppSync Events API with namespace `fieldwatch` (IAM publish; Lambda-authorizer subscribe checking the dashboard token); the projector Lambda with EventBridge rules for every event the feed shows, mapping each to a `FeedMessage` on the farm's feed or phone channel.

Done when:

- [ ] Publishing fixture events to the bus makes the matching feed messages appear in a test subscriber

Claude Code prompt:

> Implement infra/web/feed and services/projector per docs/implementation-specs.md "Live feed" and the event catalog. Include a small Node subscriber script for testing, and make the projector the only publisher.

### T29 · Dashboard

Lane C · 3.5h · Sat 17:00 · depends on: T27, T28 · unblocks: T33, T35, T36

Do: in `web/dashboard/`, the dashboard from the Core Features doc ("Web pages and dashboard" and the demo section): overview map (MapLibre, satellite basemap with licensing checked, field outlines and status badges), replay bar (scene picker, Replay, Reset), scan sweep, reveal slider on the hero field, investigation panel (hypotheses with bars, evidence cards streaming, struck-through ruled-out items), phone mirror (with a reply box calling `/mirror/reply`), and the empty, running and error states. Built for a projector: large type, high contrast. Develop against fixture feed messages until T28 merges.

Done when:

- [ ] A full hero replay plays on the dashboard end to end with no page reload; reset returns to the overview

Claude Code prompt:

> Build web/dashboard per the dashboard description in docs/core-features.md and the FeedMessage contract in docs/implementation-specs.md, using Vite, React, TypeScript and MapLibre. Add a dev mode that replays schemas/fixtures as a timed feed so the UI can be built before the backend is live.

### T30 · Phone pages

Lane C · 3h · Sat 21:00 · depends on: T27

Do: in `web/phone/`, four lightweight pages from the Core Features doc's pages table: field report (reveal slider, outline, summary, checked list, approve/decline, navigate link), photo upload (camera capture, location, progress, retake state), onboarding map (tap or pin to select, naming sheet), work order (pin, instructions, mark done). Follow the UI rules (18 px text, 48 px buttons, fast on weak signal).

Done when:

- [ ] On a real phone over cellular, each page loads in a few seconds and completes its flow against the seeded farm

### T33 · Agents tab (should-have)

Lane C · 1h · Sun 01:00 · depends on: T29, T24

Do: the agent view from the Core Features doc: agent badges on the map and one lane per running agent (field, current step, context thumbnails, last tool call), all from feed messages.

## Evaluation and demo

### T32 · Evaluation suite

Lane A · 2h · Sat 23:00 · depends on: P4, T14, T18, T23 · unblocks: T35 · critical path

Do: write the five scenario files from P4's note; the runner (reset, replay, scripted replies via mirror reply, wait for conclusion, collect session IDs, start the evaluation); the custom evaluators; the evaluator and online-evaluation Terraform resources; a `make eval` target that prints pass/fail per scenario.

Done when:

- [ ] `make eval` runs all five scenarios unattended and reports results; the hero scenario passes

Claude Code prompt:

> Implement eval/ per docs/implementation-specs.md "Evaluations": scenario files from the P4 notes, the runner using /v1/replay, /v1/replay/reset and /v1/mirror/reply, the custom evaluators, and Terraform for the evaluators and online evaluation config. Resolve the open item on how custom evaluators receive each scenario's expected values.

### T34 · Live Sentinel-2 subscription (should-have)

Lane A · 30m · Sun 02:00 · depends on: T14

Do: subscribe the scene queue to the Earth Search new-item SNS topic; confirm the filter drops scenes that don't cover a registered field and that cost stays flat.

### T35 · Demo operations and feature freeze

All · 1.5h · Sun 6:30 · depends on: T32, T29, T30 · critical path

Do:

1. 6:30 feature freeze: only fixes for failing scenarios merge after this.
2. `make eval` three times in a row, all green.
3. Record a clean full run (dashboard plus phone mirror) as the backup video.
4. `make prewarm`: a dry-run replay 10 minutes before the pitch so the Runtime and Lambdas are warm.
5. Check the hotspot, phone mirroring, projector resolution, and the judge-facing URL.
6. 8:00 submission per the organizers' instructions.

Done when:

- [ ] Three green eval runs, a saved backup video, and submission confirmed

### T36 · Pitch script and rehearsal

Lane C (with Caleb) · 1.5h · Sun 03:00 · depends on: P4, T29

Do: the 3-minute script from the Core Features doc's demo section, adapted to what's built; who clicks and who talks; the dollar figure and its basis; three rehearsals with a timer; prepared answers for the likely questions (accuracy, cost at scale, why agents, what's real vs. replayed, gap to production).

## Stretch (only when the MVP suite is green)

### T37 · Voice agent

Lane A · 3h · depends on: T23, T22 · Amazon Connect instance and number; a Nova Sonic AI agent using the same Gateway tools; the call flow from the Core Features doc; the transcript published to the phone mirror. Done when the grandfather persona receives the hero alert as a call on speaker.

### T38 · Document upload (soil tests)

Lane B · 2h · depends on: T31 · `DocumentResult` extraction, the confirmation text, and confirmed values becoming dated evidence. Goal: a farmer's soil test becomes confirmed, dated evidence.

### T39 · Code Interpreter for the imagery analyst

Lane A · 1h · depends on: T23 · give the imagery analyst an AgentCore Code Interpreter for ad hoc comparisons, kept off the hero path's 60-second budget. Done when the agent tab shows a code-run step on a secondary field.

### T40 · Extra sensors (Sentinel-1)

Lane B · 2h · depends on: T13 · a Sentinel-1 adapter for standing-water detection on cloudy days, behind the same analyzer interface. Goal: standing water can be detected even when clouds block the optical imagery.

## Critical path, cut order, and when you're behind

**Critical path:** two chains that meet at the evaluation suite. The data chain is P1 → T02 → T03 → T08 → T11 → T13; the agent chain is T19 and T20 → T23. Both must land before T32 → T35. Lane B carries most of the data chain in the afternoon, so A and C should take any lane-B ticket that unblocks it (for example, A can take T12 or C can take T26).

**7 p.m. checkpoint target:** a replay of the hero scene produces a diagnosis and a message in the phone mirror, however rough. If that isn't working by 7, everyone swarms on it before starting anything new.

**Cut order if behind** (drop from the top): T37 voice → T38 documents → T39 Code Interpreter → T40 radar → T33 agents tab → T34 live subscription → T16's should-have pollers → T21 OpenAPI targets (keep the Lambda versions) → T24's episodic memory → T31 photo intake.

**Never cut:** T13, T14, T19, T20, T23, T29 (reveal and investigation panel), T17–T18 (mirror path), T22 (approval to work order), T32 (at least the hero scenario), T35.

**Every two hours:** a five-minute stand-up: what merged, what's blocked, and whether the cut line moves.
