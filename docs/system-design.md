# FieldWatch: System Design

Oct 6, 2026 · @Caleb

FieldWatch is an event-driven system on AWS: deterministic pipelines turn satellite and weather data into per-field facts, and a Strands multi-agent investigator on Amazon Bedrock AgentCore runs only when something looks wrong, then reaches the farmer by text.

## Summary, goals and constraints

The design optimizes for one thing above all: a live, under-60-second loop from a satellite pass to a farmer's phone that works every time on stage, built in 24 hours, at hackathon-scale cost.

**Goals**

1. Detect field stress from free public imagery with no farmer install.
2. Diagnose it by gathering and weighing evidence from several sources, visibly.
3. Reach the farmer in plain language by text (voice as a stretch) and act only with their approval.
4. Make AgentCore and Strands load-bearing: agents run the investigation, tools are governed, actions are policy-gated, outcomes are remembered, and agent quality is measured.
5. Keep cost proportional to anomalies, not acreage.

**Non-goals for the hackathon:** accounts and passwords, billing, multi-tenant admin, a native app, commercial imagery as a dependency, model training or fine-tuning.

**Constraints**

| Constraint | Value | Design consequence |
| --- | --- | --- |
| Build time | 24 hours, 3–4 people | Managed services only; one Strands app; scripted deploy |
| Fresh-code rule | No app code before the event | This doc, data access checks and tutorials only |
| Demo latency | Under 60 s, trigger to text | Precompute context; parallel specialists; prewarm |
| Cost | Hackathon budget | Scale to zero everywhere; no always-on capacity |
| Data region | Sentinel-2 files in us-west-2 | Primary region us-west-2 |
| Messaging | US SMS needs a registered sender | In-app phone mirror as a guaranteed fallback |

## System overview

Four layers, top to bottom: external data, ingestion and analysis, shared data and events, and the people-facing channels, with the agent layer reading from the middle and writing to the bottom.

&#91;embedded content: system overview · every pipeline, the agent layer and the AWS services behind each\]

- **Ingestion and analysis** is plain, deterministic AWS compute (Lambda, Step Functions). It runs on every satellite pass and storm, and it's cheap.
- **The agent layer** runs only when the analysis flags an anomaly. It is one Strands application on AgentCore Runtime, reaching data only through AgentCore Gateway tools, with AgentCore Memory, Policy, Identity, Observability and Evaluations around it.
- **Interaction** turns agent decisions into texts, pages and (stretch) calls, and turns replies back into events.

## C4 level 1: system context

FieldWatch sits between three kinds of people and six external systems it depends on but doesn't control. The diagrams below follow the C4 model (context, containers, components) plus one dynamic view for the hero scenario.

&#91;embedded content: C4 level 1 · system context: 3 kinds of people, 6 external systems (dashed)\]

The two dependencies with the most demo risk are the carrier network (SMS registration) and Bedrock model availability in the chosen region; both have fallbacks (the phone mirror, and a second model per agent role).

## C4 level 2: containers

Eleven deployable containers inside the system boundary, all serverless or managed, so nothing runs (or bills) while idle.

&#91;embedded content: C4 level 2 · containers and main relationships; the table below lists every link\]

| Container | Technology | Responsibility | Talks to |
| --- | --- | --- | --- |
| Ingestion and analysis | Lambda (container images), Step Functions Express, SQS | Scene and hazard pipelines; per-field anomaly analysis | Event bus, data store |
| Context builder | Step Functions Standard, Lambda | Per-field context at onboarding | Data store |
| Event bus | Amazon EventBridge | `AnomalyDetected`, `WatchOpened`, `ReplyReceived`, `PhotoReceived`, investigation progress | Agent service, live feed |
| Agent service | AgentCore Runtime running one Strands app | Investigations: supervisor + specialists | Tool gateway, memory and policy, Bedrock |
| Memory and policy | AgentCore Memory, Policy (Cedar), Identity | Preferences and past outcomes; rules on write tools; credentials for external APIs | Agent service, tool gateway |
| Tool gateway | AgentCore Gateway (MCP) with Lambda and OpenAPI targets | Every read and write the agents can do | Data store, knowledge base, messaging |
| Knowledge base | Bedrock Knowledge Bases + S3 Vectors | Agronomy retrieval | Tool gateway |
| Data store | S3, DynamoDB (on-demand) | Rasters, overlays, photos, records | Everything |
| Messaging | AWS End User Messaging SMS, SNS, Lambda | Outbound texts, inbound replies | Farmer, event bus |
| Web API and live feed | API Gateway HTTP API, Lambda, AppSync Events, CloudFront + S3 | Phone pages, uploads, approvals, dashboard feed | Data store, event bus |
| Voice agent (stretch) | Amazon Connect + Nova 2 Sonic | Calls for people who prefer them | Tool gateway |

## Subsystem: ingestion and analysis

Two lanes share one rule: deterministic code decides whether anything is unusual, and only then does an agent get involved. Dataset-by-dataset details are in the pipelines doc (linked at the end).

&#91;embedded content: C4 level 3 · ingestion and analysis components; dashed boxes are outside this subsystem\]

Open watches make the field analyzer more sensitive for the fields under them; that read is not drawn to keep the lanes legible.

Key decisions:

- **Express Step Functions for scene analysis:** each run is short and runs per scene, so Express (billed per request and duration) is cheaper than Standard; onboarding uses Standard because it runs longer and benefits from step history.
- **No copying imagery:** the analyzer reads only the field's window of the public cloud-optimized files with HTTP range requests, in the same region, so there's no storage and no transfer charge.
- **Idempotent by key:** SNS and SQS deliver at least once, so each analysis is keyed on scene ID + field ID and each anomaly on field + date; duplicates are dropped.
- **Hazards computed once per region:** each MRMS file is clipped against all registered farms in one pass, so cost grows with storms, not farms.
- **Cross-region hop:** the MRMS SNS topic lives in us-east-1, so a small forwarder Lambda there sends events to the us-west-2 event bus.
- **Replay is a first-class entry point:** `POST /replay` writes the same message shape into the scene queue, so the demo exercises the real path.

## Subsystem: agents (AgentCore + Strands)

One Strands application runs on AgentCore Runtime: a supervisor agent that plans and decides, and three specialist agents it calls as tools. Every data access and action goes through AgentCore Gateway, every action is checked by AgentCore Policy, and every step is traced and evaluated.

&#91;embedded content: C4 level 3 · agent components: one Runtime, four agents, governed tools\]

The policy check also reads the Approvals table written by the reply handler; that link is described below rather than drawn.

**Orchestration.** Strands' agents-as-tools pattern: the supervisor sees each specialist as a tool with a one-line contract ("given a field and an anomaly, return evidence items"). It calls the three in parallel, gets back structured evidence items, sends them to the deterministic hypothesis scorer, and writes the diagnosis and message. One Runtime keeps cold starts and cost down; the specialists can be split into their own runtimes later if they need to scale separately.

**Fast path (revision R1).** Before calling any specialist, the supervisor runs the scorer on the precomputed evidence in the context bundle. If one hypothesis is already above a high threshold, it skips the specialists and goes straight to impact and the message; specialists run only when hypotheses are close.

**Agent roster**

| Agent | Model tier | Gateway tools it may call | Returns |
| --- | --- | --- | --- |
| Supervisor | Larger reasoning model (a Claude Sonnet-class or Amazon Nova Pro-class model) | `get_field_context`, `get_scene_analysis`, `score_hypotheses`, `estimate_impact`, all write tools | Diagnosis, message draft, proposed action |
| Imagery analyst | Small multimodal model (Claude Haiku-class or Nova Lite-class) | `get_scene_analysis`, `get_index_history`, `compare_to_cohort`, `test_overlap` | Evidence items about pattern, timing and extent; can look at the overlay PNGs |
| Environment analyst | Small model | `get_weather`, `get_hazards`, `get_drought`, `get_field_context`, `test_overlap` | Evidence items about weather, hail, drought, soil, terrain |
| Agronomy and economics analyst | Small model | `search_agronomy`, `get_disease_reports`, `estimate_impact`, photo and document results | Evidence items from guidance and photos; dollar range |
| Voice agent (stretch) | Nova 2 Sonic | Same read tools; write tools via the same policy | A spoken conversation, summarized into the investigation |

Specialists get **read-only** tools; only the supervisor can call write tools. Pick the exact models by running the scenario suite (Observability and evaluation) and keep a second model configured per role as a fallback.

**Evidence items** are the contract between agents and the scorer: `{hypothesis, direction (supports / refutes), source tool, value, plain-language reason, weight key}`. The LLM never invents a confidence number; the scorer computes it.

**Memory design (AgentCore Memory)**

| Scope | Kind | Holds |
| --- | --- | --- |
| Investigation session | Short-term | Tool results and messages for this case; lets a reply or photo resume the session |
| `farm/{id}/people` | Long-term | Who's on the farm, roles, call or text preference, quiet hours |
| Investigation episodes (actor: the farm) | Long-term, episodic strategy | How past investigations unfolded and ended, including the farmer's verdict (ring pattern on the north pivot, July 2026: nozzle, confirmed by Jake). Exact records stay in DynamoDB; episodes are what the agent recalls. |
| `farm/{id}/feedback` | Long-term | Farmer corrections ("that wasn't hail, it was drift") |

**Identity and Gateway targets.** Most tools are Lambda targets because they cache and summarize per field. Two small, single-call lookups are OpenAPI targets with no Lambda at all: `get_drought` (Drought Monitor) and `get_county_stats` (USDA NASS Quick Stats). The Quick Stats API key lives in an AgentCore Identity API-key credential provider, and Gateway injects it on each call, so no function or agent ever holds the key. The context builder's economics stage calls the same Gateway tool rather than keeping its own copy.

**Policy design (AgentCore Policy, Cedar).** AgentCore Policy evaluates each tool call against Cedar policies whose schema is generated from the Gateway's tools; the only context it can see is the tool's own arguments (`context.input`), and the principal is the caller's IAM identity. So the policy enforces shape, and code enforces facts:

- **At the Gateway (Cedar):** read tools are permitted; write tools are permitted only when the call includes a well-formed `approval_id` and `investigation_id`.
- **In each write-tool Lambda:** the approval must exist, be `approved`, belong to this investigation, match the action type, and be unexpired; each approval can produce at most one action.
- **In the Strands app:** specialists only receive read tools, because policy can't distinguish supervisor calls from specialist calls (they share the Runtime's identity).

Approval records are written only by the reply handler or the report page (deterministic code that matched the farmer's reply to the message it answered), never by the agent. Exact tool-input shapes are in the implementation specs.

**Prompts.** Each agent's system prompt states its role, its tools, the evidence-item format, plain-language rules, and that text from farmers, photos, documents and retrieved articles is data to evaluate, never instructions to follow.

## Subsystem: interaction

Five channels, one rule: everything a person does becomes an event on the bus, and the agent picks it up from there. No channel talks to the agent directly except voice.

&#91;embedded content: C4 level 3 · interaction components; shaded boxes belong to other subsystems, dashed outlines are stretch\]

- **SMS out:** the supervisor calls `send_farmer_message`; the tool Lambda checks quiet hours and rate limits, then sends through End User Messaging with an idempotency key.
- **SMS in:** replies arrive on an SNS topic; the reply handler normalizes them (YES/NO/MORE/PHOTO/free text), dedupes by message ID, records approvals, and emits `ReplyReceived`, which resumes the investigation's Runtime session.
- **Phone pages:** static pages on CloudFront call an HTTP API; every link carries a signed, expiring token scoped to one farm and one task, verified in the API Lambda.
- **Uploads:** the API returns a presigned S3 URL; the upload event triggers the intake Lambda (blur check, metadata strip, document extraction), which emits `PhotoReceived` or `DocumentReceived`.
- **Live dashboard:** a projector Lambda turns bus events and investigation progress into small messages on an AppSync Events channel per farm; the dashboard subscribes over WebSockets. The phone mirror reads the same channel, which is also the SMS fallback.
- **Voice (stretch):** Amazon Connect hosts the number; its Nova Sonic AI agent uses the same Gateway tools, so text and voice give the same answers.

## Hero scenario: sequence and latency budget

From pressing Replay to the alert text should take under 60 seconds; the targets below leave about 15 seconds of slack.

&#91;embedded content: dynamic view · hero scenario sequence with time targets\]

The fast path (revision R1) would skip the three specialist steps when the first score is already decisive; the hero case is chosen so they run.

| Step | Target | How it stays fast |
| --- | --- | --- |
| Replay to analyzer start (queue, filter, Step Functions) | 2 s | Warm Lambdas; tile-to-field lookup in memory |
| Field analysis (reads, indices, anomaly, overlays) | 8–10 s | Windowed range reads in us-west-2; one field |
| Decision + event to Runtime session start | 2 s | Runtime prewarmed by a dry run before the pitch |
| Supervisor plan | 3 s | Short prompt; context bundle passed in the event |
| Three specialists in parallel | 12–15 s | Small models; precomputed context; cached weather |
| Score + impact | 2 s | Deterministic Lambdas |
| Diagnosis and message | 4 s | Template-guided output |
| SMS delivery | 2–5 s | Carrier-dependent; phone mirror shows it instantly |
| **Total** | **35–43 s** |  |

## Data and storage

One S3 bucket and twelve `fw-` DynamoDB tables (farms, fields, tile index, field context, scene analyses, watches, investigations, approvals, actions, messages, uploads, idempotency); keys, indexes and TTLs are in the implementation specs ("Data model").

- **S3:** Nebraska slices of static datasets (GeoParquet, COG), per-field context rasters, analysis outputs and overlay PNGs, photos and documents. Lifecycle rules: analysis and hazard outputs expire after 180 days, uploads after 365 days.
- **DynamoDB, on-demand capacity:** no provisioned throughput to pay for while idle. Investigations hold the hypothesis list and evidence items, so the dashboard and the report page read one item.
- **AgentCore Memory** holds what agents should recall across investigations; DynamoDB holds the system of record. Anything a page needs to display lives in DynamoDB, not only in Memory.
- **Bedrock Knowledge Base on S3 Vectors** for retrieval, with metadata filters by crop, stage and condition.
- **Secrets:** the link-signing key lives in SSM Parameter Store (not Secrets Manager, which charges per secret); third-party API keys live in AgentCore Identity.

## Cost design

Cost scales with anomalies and messages, not with acreage or time: every component bills per request or per second of use, and the only model calls happen inside investigations. The real risk is not steady-state cost but a few well-known traps that bill while idle.

**Cost drivers and decisions**

| Component | What drives cost | Decision |
| --- | --- | --- |
| Bedrock model calls | Tokens per investigation | Large model only for the supervisor; small models for specialists; prompt caching for long system prompts; token cap per investigation; fast path (see critique) |
| AgentCore Runtime | Active session time | One Runtime; sessions end when the investigation pauses for a reply and resume on the event |
| AgentCore Gateway, Memory, Observability, Evaluations | Per call, per event, per trace | Few tools per investigation; sample traces outside demos; online evaluation scores only a small share of live investigations |
| Lambda, Step Functions | Invocations and duration | Express workflows for scene analysis; container images only where GDAL or GRIB2 is needed |
| Imagery reads | Requests; transfer if cross-region | Range reads in us-west-2 only; no copies |
| Knowledge base vectors | Store type | S3 Vectors, not a vector database with minimum capacity |
| DynamoDB | Requests | On-demand mode |
| CloudWatch Logs | Ingested volume | Log retention 7 days; info level by default |
| SMS, voice | Per message, per minute, registration fees | Daily cap per farm; voice is opt-in and stretch |

**Traps avoided**

- **NAT gateways:** they bill hourly even when idle. No Lambda runs in a VPC, so none are needed.
- **Vector databases with minimum capacity:** a managed vector search cluster can cost more in a month than everything else combined; S3 Vectors avoids it.
- **Always-on compute:** no EC2, no provisioned concurrency, no provisioned DynamoDB.
- **Cross-region imagery transfer:** compute stays next to the Sentinel-2 files.
- **Forgotten resources after the event:** everything is tagged `project=fieldwatch` and created by one Terraform stack, so teardown is one command.

**Guardrails:** AWS Budgets alerts at two thresholds the team picks before Saturday; reserved concurrency caps on every Lambda; a per-investigation token budget enforced in the Strands app; a rate limit on `/replay`.

**Unit economics to measure, not guess:** tokens and dollars per investigation (from Observability), Lambda seconds per field per pass, and messages per alert. Put those three numbers on the dashboard's debug view by Sunday morning; they make a strong Q&A answer on cost at scale.

## Security, privacy and safety

The main threats are an agent taking an action nobody approved, untrusted content steering an agent, and leaking farmers' phone numbers or locations.

| Threat | Control |
| --- | --- |
| Agent acts without approval | Write tools need an approval record written by deterministic code (reply handler or report page), verified in the tool Lambda, with the Gateway's Cedar policy requiring a well-formed approval ID on every write tool; specialists have no write tools |
| Prompt injection from farmer texts, photos with text, uploaded documents, retrieved articles | All of it is passed as data in a separate field, never appended to instructions; Bedrock Guardrails prompt-attack filter on inputs; the worst an injected instruction can do is draft a message, because actions still need a human yes |
| Forged or shared report links | HMAC-signed tokens scoped to farm + task + expiry; key in Parameter Store; tokens single-purpose (viewing a report can't approve a different action) |
| Spoofed SMS replies | Replies are only accepted from the phone number the approval was sent to, within its expiry |
| Phone numbers and field locations | Encrypted at rest (S3 and DynamoDB defaults); never sent to third parties; photo metadata stripped except location and time |
| Over-broad IAM | One role per Lambda with only the tables and prefixes it needs; Runtime role can only reach the Gateway and Bedrock |
| Someone claims another farm's fields | A field can belong to one farm; a second claim needs the first farm's confirmation |

**Safety of advice:** findings are phrased as "consistent with," confidence comes from the scorer, disease calls point to the UNL Plant & Pest Diagnostic Clinic, and the system never recommends a pesticide product or rate; it points to the label and Extension guidance instead.

**Third-party API keys** (for example the NASS Quick Stats key) live in an AgentCore Identity credential provider and are injected by the Gateway, so no Lambda or agent holds them. Only the link-signing key lives in Parameter Store.

## Observability, evaluation and deployment

**Observability.** Strands emits OpenTelemetry traces; AgentCore Observability collects Runtime and Gateway traces into CloudWatch. The same spans feed the dashboard's investigation panel and agent view through the projector, so what the audience sees is the real trace, not a separate animation. Four metrics on a CloudWatch dashboard: trigger-to-text latency, tokens per investigation, cost per investigation, and alerts per field per month.

**Evaluation: a scenario suite.** A handful of recorded historical scenes covering the situations the system must handle: a pivot problem (the hero), a hail swath, water stress on sandy soil, a healthy field, and late-season dry-down. The team finds the scenes this week (P4); the expected outcome for each and the pass criteria are written at the event (T32).

Run it as an AgentCore Evaluations job (built-in evaluators for answer quality plus custom evaluators for the expected outcomes above) after every agent or prompt change on Saturday, and three times in a row before the pitch. Online evaluation also samples live investigations, so quality is tracked after the demo too. A green suite is the definition of "demo ready."

**Deployment.**

- **One AWS account, one environment, us-west-2,** plus the small MRMS forwarder in us-east-1.
- **Terraform** for the AWS resources the team already knows how to manage. For AgentCore resources, use Terraform if the AWS provider covers them by Saturday; otherwise the AgentCore starter toolkit or CDK for just those. Confirm which this week.
- **The Strands app** ships as a container image to AgentCore Runtime; Lambdas ship as zips except the GDAL and GRIB2 ones (container images).
- **Demo hygiene:** pin model versions, temperature 0 for specialists, prewarm with a dry run 10 minutes before the pitch, and keep a recorded clean run as a backup.

## Design critique and revisions

Verdict: the architecture is sound and the AWS story is strong, but as first drafted it spends LLM calls where code would do, has more moving parts than 24 hours comfortably allow, and leaves demo determinism to luck. Eight revisions fix that; they're already reflected where noted.

**Evaluation against the goals**

| Criterion | Assessment | Verdict |
| --- | --- | --- |
| Demo impact | Reveal, visible reasoning, phone moment and concurrency are all on one dashboard | Strong |
| Completeness risk | 11 containers and \~20 AWS services for 3–4 people in 24 hours | Risky; needs cut lines (R7) |
| AgentCore and Strands essential? | Runtime, Gateway, Policy, Identity, Memory, Observability and Evaluations each do real work; multi-agent parallelism earns its place in the latency budget | Strong, if the agents don't run when they aren't needed (R1) |
| Cost | Scale-to-zero throughout; traps avoided | Good; model tokens are the one variable to watch (R1, R2) |
| Latency | 35–43 s planned against a 60 s budget | OK, but cold starts and LLM variance could eat the slack (R5) |
| Security | Approvals bound in code and policy; untrusted input treated as data | Good after R3 |
| Correctness of diagnoses | Rules plus a scorer with hand-set weights; no measured accuracy yet | Weakest area; R4 and R6 address it |

**Problems found and revisions**

1. **R1: agents run even when the answer is already obvious.** If precomputed evidence (pattern features, open hail watch, drought status) already puts one hypothesis above a high threshold, the supervisor skips the specialists and goes straight to scoring and the message. This fast path cuts tokens and latency for clear-cut cases; specialists run only when hypotheses are close, which is exactly where gathering evidence adds value. For the demo, the hero case is chosen to be ambiguous at first so the specialists visibly run, and a second field shows the fast path.
2. **R2: one Runtime, not four.** Separate runtimes per specialist add cold starts and cost for no hackathon benefit. One Strands app with agents-as-tools (Agent subsystem).
3. **R3: approvals must never be inferred by the model.** Approval records are written only by the reply handler or the report page, and checked in two places (Agent subsystem, Security).
4. **R4: no way to know if diagnoses are right.** Add the five-scenario suite and run it as the definition of demo-ready (Observability and evaluation).
5. **R5: demo determinism.** Pinned models, temperature 0 for specialists, prewarm dry run, recorded backup (Deployment). The phone mirror removes the carrier from the critical path.
6. **R6: no learning loop.** After a repair is marked done or a week passes, ask "Was this right? YES / NO / it was something else." Answers go to the episodic memory strategy (so the agent recalls how past cases turned out) and a labels table; they become the training data for the optional pattern classifier and for tuning scorer weights. Should-have for the hackathon, but it's the strongest answer to "how does this get better?"
7. **R7: too much to build.** Cut lines in the build plan: the system works end to end with only the MVP column, and every later item is additive.
8. **R8: hazards shared across farms.** One hail swath affects every farm under it; computing it once per radar file and fanning out to watches (already in the ingestion design) also enables a future regional early-warning feature at no extra cost.

**Expansions after the hackathon**

- **Regional early warning:** when several farms' fields show the same new pattern (a disease moving through a county), alert nearby farms before it reaches them.
- **Agronomist portal:** consultants see all their clients' investigations, add notes, and approve on their behalf when allowed.
- **Insurance claim packet:** for hail, a dated bundle of before/after imagery, radar swath and affected acres the farmer can send to their agent.
- **Premium imagery tier:** PlanetScope adapter for near-daily detection.
- **Learned pattern classifier:** trained on R6 feedback labels once there are enough.

## Build plan and cut lines

Goal for the 7 p.m. checkpoint: the hero loop works end to end once, however rough. Everything after that is reliability, polish and additions.

**Roles** (combine if the team is smaller): A: AWS infrastructure and agents; B: geospatial pipeline and scorer; C: dashboard and phone pages; D: messaging, scenario suite and pitch.

| Window (Sat–Sun) | A: infra + agents | B: geospatial | C: frontend | D: messaging + pitch |
| --- | --- | --- | --- | --- |
| 9:30–11:30 | Terraform base: bucket, tables, bus; hello-world Strands agent on Runtime with one Gateway tool | Demo farm onboarded from CSB; context for its fields | Dashboard shell with map and field outlines | SMS sender and phone mirror channel |
| 11:30–15:00 | Read tools; supervisor + specialists | Field analyzer on the hero scene; overlays | Reveal slider; investigation panel wired to events | Reply handler; approvals |
| 15:00–19:00 | End-to-end hero loop | Scorer and impact model | Report page | Scenario suite skeleton |
| 19:00–01:00 | Write tools + policy; fast path | Hail lane; second scenario fields | Photo upload page | Photo flow; suite green on 3 scenarios |
| 01:00–06:30 | Should-haves by priority | Gates for dry-down and harvest | Agent view tab | Pitch script; rehearsals |
| 06:30–8:00 | **Feature freeze.** Three green suite runs, record the backup video, final rehearsal |  |  |  |

**Cut lines, dropped in this order if behind:** voice → document upload → agent view tab → live Sentinel-2 subscription (replay stays) → NWS and storm-report pollers → extra sensors → free-text questions → photo flow.

**Never cut:** replay, field analyzer, supervisor + specialists, scorer, report page with the reveal, phone mirror (SMS if registered), approval to work order.

## Related docs and sources

- FieldWatch: Data & ML Pipelines: dataset-by-dataset pipelines, storage layout, models
- FieldWatch: Core Features: users, features, messaging, voice, escalation, pages and UI rules

Sources:

- [Element 84 Earth Search](https://github.com/Element84/earth-search)
- [Sentinel-2 Cloud-Optimized GeoTIFFs on the AWS Registry of Open Data](https://registry.opendata.aws/sentinel-2-l2a-cogs/)
- [NOAA MRMS on the AWS Registry of Open Data](https://registry.opendata.aws/noaa-mrms-pds/)
- [USDA Crop Sequence Boundaries on Source Cooperative](https://source.coop/fiboa/us-usda-cropland)
- [Introducing Amazon Nova 2 Sonic (AWS News Blog)](https://aws.amazon.com/blogs/aws/introducing-amazon-nova-2-sonic-next-generation-speech-to-speech-model-for-conversational-ai)
- [Amazon Connect agentic self-service with Nova Sonic (AWS What's New)](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-connect-agentic-self-service)
- [AWS Guidance for Farm Management AI Agent](https://aws.amazon.com/solutions/guidance/farm-management-ai-agent-on-aws/)
