# Workstreams (parallel build, Oct 10–11)

How the remaining work is split so three people and their Claude Code agents can build at the same time. Read `docs/decisions.md` first; it overrides the plan docs.

## How this works

- **Contracts first.** Everything that crosses a workstream boundary is a model in `agent/fieldwatch/contracts.py`, with a sample in `agent/fixtures/contracts/`. Build against the samples; don't wait for other lanes.
- **The registry is the joint.** `agent/fieldwatch/registry.py` lists every tool. Stub entries return contract samples; your workstream replaces the stub's `fn` with the real implementation. The Gateway (workstream 6) generates its tools from the same list.
- **Own your folders.** Each workstream edits only the paths listed for it. Shared files have one owner; ask that person (or open a tiny PR) to change them.
- **One branch per ticket**, short-lived, merged to `main` often. CI must stay green with every switch on `local`.
- **One Claude Code session per workstream**, in its own git worktree:
  ```powershell
  git worktree add ..\FarmU-ws3 -b ws3-hail
  cd ..\FarmU-ws3; claude
  ```
  Paste the workstream's prompt below. Ask for a plan first, then let it implement.
- **Contract changes** go in their own small PR, announced in team chat. Never change a contract inside a feature PR.
- **Deploys:** Caleb (or the deploy workflow on main) deploys every stack. Kurt and Aaron write their stacks and open PRs, and use the `fieldwatch-dev` role for Bedrock, data and logs (D3).

## Shared files and their owners

| File | Owner | Others |
| --- | --- | --- |
| `agent/fieldwatch/contracts.py`, `fixtures/contracts/` | Caleb (WS0) | Small PRs only |
| `agent/fieldwatch/registry.py` | Caleb (WS0) | Swap your own entry's `fn`/`stub` in your PR |
| `agent/fieldwatch/config.py` | Caleb (WS0) | Add your env vars in a small PR |
| `agent/fieldwatch/agents.py` (prompts, roster) | Aaron (WS5) | |
| `agent/fieldwatch/tools.py` | Caleb (WS8 for write tools) | Read tools move behind the registry |
| `agent/web/server.py`, `agent/web/static/` | Caleb (WS10) | Expose functions; WS10 wires them in |
| `infra/app.py` | Caleb (WS0) | Add one line for your stack |

## Workstreams

| WS | Name | Owner | Paths | Depends on (samples unblock you) | Plan refs |
| --- | --- | --- | --- | --- | --- |
| 0 | Foundation + integration | Caleb | contracts, registry, config, infra/app.py, CI, merges | — | T01–T04, T06 |
| 1 | Field analyzer | Aaron | `agent/fieldwatch/analysis/`, `tests/test_analysis*.py` | contracts | T11, T13; pipelines B2, M1–M3 |
| 2 | Orchestration + replay + trigger | Caleb | `agent/fieldwatch/trigger.py`, `agent/cloud/lambdas/scene_check/`, `agent/cloud/lambdas/session_starter/`, `infra/stacks/analysis.py` | WS1 (stub ok) | T14, T34 |
| 3 | Hazards | Kurt | `agent/fieldwatch/hazards/`, `infra/stacks/hazards.py`, `tests/test_hazards.py` | contracts | T15, T16; pipelines C1–C4 |
| 4 | Field context | Kurt | `agent/fieldwatch/context/`, `tests/test_context.py` | contracts | T08–T10, T12, T26; pipelines A1–A10 |
| 5 | Scorer, catalog, impact, agent reasoning | Aaron | `agent/catalog/`, `agent/fieldwatch/scoring.py`, `agent/fieldwatch/agents.py`, `agent/fieldwatch/schemas.py`, `tests/test_scoring.py` | contracts | T20; pipelines M4–M5 |
| 6 | Gateway tools | Caleb | `agent/cloud/gateway/`, `agent/fieldwatch/toolset.py`, `infra/stacks/tools.py` | registry | T19, T21 |
| 7 | Agent on Runtime + sessions + memory | Caleb | `agent/cloud/agentcore/`, `agent/app.py`, `agent/fieldwatch/investigation.py`, `agent/fieldwatch/runner.py` | — | T23, T24 |
| 8 | Approvals + write tools | Caleb | `agent/fieldwatch/approvals.py`, write tools in `tools.py`, `tests/test_approvals.py` | WS9 send API | T22 |
| 9 | Notifications | Kurt | `agent/fieldwatch/notify/` (from `sms.py`), `infra/stacks/notifications.py`, `tests/test_notify*.py` | contracts | T17, T18 |
| 10 | Web app as client | Caleb | `agent/web/`, `agent/fieldwatch/store.py` | WS7 runner, WS9 | T27, T29, T30 |
| 11 | Evaluation + hero scenes | Caleb (Caleb picks the scenes) | `agent/eval/`, `docs/hero-scenes.md` | WS1, WS5 | P4, T32 |

## Goals per workstream

Write your own checks from the goal first (Build Plan "the loop for every ticket"), then implement.

1. **Field analyzer.** For a field polygon and an as-of date, analyze the latest clear Sentinel-2 scene on or before that date. Output a `FieldAnalysisResult`. It must:
   - mask clouds and shadow per pixel from the scene classification band, and apply a ~10 m interior buffer;
   - compute NDVI, NDRE and NDMI (apply scale/offset when using the C1 collection);
   - compute z-scores against the same weeks in 2–3 prior seasons, plus the change since the previous clear pass;
   - extract regions of at least 0.5 ha with pattern features (ring, wedge, edge band, stripes; overlaps filled when soil, low-spot or hail layers are given);
   - set the gates: senescence/harvest by date and crop, uniform whole-field drop, region-wide drop;
   - write overlays.

   It should flag a real summer problem and stay quiet on healthy and end-of-season fields. Replace `registry.analyze_field`.
2. **Orchestration + replay + trigger.** Local: `trigger.check_farm(farm, as_of)` runs analysis on every field and, when a region passes and no gate is set, starts an investigation. The web watcher calls it. Cloud: Scheduler → scene-check Lambda → `AnomalyDetected` on the bus → session-starter Lambda → AgentCore Runtime, with idempotency on field + date. Replay is any `as_of` in the past.
3. **Hazards.** `get_hail` (MRMS MESH max over the field for a date range, in mm; SPC reports within 10 km), `get_drought` (USDM county category with a written rule), and historical NWS warnings from the Iowa Environmental Mesonet archive. Optional: a daily Lambda that opens hail watches.
4. **Field context.** `get_soil_profile` (Soil Data Access: map units, horizons, sandy flag, soil-zone polygons), terrain low spots (3DEP 1 m), crop history and same-crop neighbors from USDA CSB 2018–2025 (EPSG:5070; cache per farm), `get_disease_reports` (EDDMapS), and economics (NASS key if available, else crop defaults). Fills `FieldContext`.
5. **Scorer, catalog, impact, agent reasoning.**
   - `catalog/conditions.json`: priors by crop, stage and irrigation; weights per `weight_key` with a cited reason. **Caleb reviews the agronomy.**
   - `score_hypotheses`: log-odds; marks `ruled_out` below 0.05 and `likely` above 0.6.
   - `estimate_impact` from `catalog/loss_tables.json`: hail by growth stage, water stress around silking.
   - Specialists return `contracts.SpecialistResponse` evidence items; the supervisor calls the scorer and never states its own confidence. Add an environment/agronomy split if time allows.
6. **Gateway tools.** One Lambda handler that dispatches on the tool name (strip the `target___` prefix) to `registry.BY_NAME[...]`. Tool schemas are generated from each entry's `input_model`. Gateway with IAM inbound auth (spike findings 20–24). `toolset.tools_for(backend)` returns local functions or a SigV4-signed MCP client (finding 34) behind `FIELDWATCH_TOOLS_BACKEND`, falling back to local if the Gateway is unreachable.
7. **Agent on Runtime.**
   - Create the AgentCore CLI project in `agent/cloud/agentcore/` (CodeZip, no `mcp.exa.ai`), deploying `app.py`'s start/reply entrypoint.
   - Sessions behind `FIELDWATCH_SESSIONS` (S3 session manager; AgentCore Memory if time allows).
   - `runner.start/reply` call Runtime or local behind `FIELDWATCH_AGENT_BACKEND`; on error they fall back to local and say so in the result's `backend`.
   - Turn on tracing.
8. **Approvals.**
   - Approval records (local file or `fw-approvals`) created when an interrupt asks.
   - Decisions are recorded only by the reply path and only from the number that was asked; they expire.
   - "Already decided by …" for a second answer.
   - Write tools act at most once per approval.
   - The work order is really delivered: a text to the assignee via WS9.
9. **Notifications.**
   - Turn `sms.py` into a `notify` package with the same `send()`/inbound interface: providers (Twilio, TextBelt, SNS, none; End User Messaging stub), message records and `MessageSent`/`MessageReceived` events, idempotency keys.
   - Rules:
     - quiet hours with deferral, daily cap, opt-in per person, STOP/START/HELP;
     - people with roles (farmer, family delegate, repairs) so work orders reach the repair person;
     - reply intents (YES/NO/MORE/ASK/PHOTO/free text) matched to the message they answer.
10. **Web app as client.** The watcher calls WS2's trigger. ASK and replies go through WS7's runner. State goes through `store.py` (local JSON or DynamoDB behind `FIELDWATCH_STORE`). The phone panel shows which backend ran each step. Add the plan's report view (reveal slider, checked list, dollar range, approve/decline) and work-order view. Keep the current map and phone UI.
11. **Evaluation + hero scenes.** Pick the hero and secondary scenes: a summer 2026 pivot or hail problem with clear passes before and after, a healthy field, and an end-of-season field. Record them in `docs/hero-scenes.md`. Then `agent/eval/` runs `contracts.Scenario` files end to end in local mode (cloud mode if ready) and reports pass/fail per scenario.

## Checkpoints

| When | Must be merged and shown |
| --- | --- |
| +0:45 | WS0 merged (this branch): contracts, samples, registry, switches, CDK skeleton, CI. Account/region and IAM access settled; DataPlane deployed |
| +3h | WS1 indices + replay on a hero date · WS5 scorer ranks the sample evidence correctly · WS7 today's agent on Runtime behind the switch · WS9 send/reply with rules and the phone mirror |
| +6h | WS1 regions + gates · WS3/WS4 hail, drought, soil live · WS6 Gateway serving registry tools · WS8 approval records + real work-order text · WS10 runner + store wired |
| +9h | Hero scenario end to end in local **and** cloud mode · WS11 scenarios passing in local mode · WS2 cloud trigger if green |
| ~6:30 | Feature freeze. Demo cloud mode if green, else local. Record a backup run |

## Prompt template (paste into the workstream's Claude Code session)

> You are working on FieldWatch (repo FarmU), workstream **N: <name>**. Read `CLAUDE.md`, `docs/decisions.md`, `docs/spike-findings.md`, and the plan sections listed for workstream N in `docs/workstreams.md` (plan docs are in `docs/`). Decisions and spike findings override the plan docs. Build against `agent/fieldwatch/contracts.py` and the samples in `agent/fixtures/contracts/`; do not change contracts. Edit only the paths listed for workstream N. Goal: <paste the goal>. First write the checks and tests for that goal (no network or AWS in unit tests; mark live tests `@pytest.mark.live`), then propose a plan, then implement. Run `python -m pytest tests -q -m "not live"` from `agent/` before finishing. If the goal needs AWS: region us-west-2; add your stack to `infra/` but do not deploy unless you are Caleb.
