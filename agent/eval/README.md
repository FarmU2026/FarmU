# eval/ (WS11, Caleb)

`scenarios/*.json` are `contracts.Scenario` files (hero, hail, healthy, end-of-season). The runner replays each one
through the system and prints pass/fail per scenario. "Demo ready" means **every scenario passes, three runs in a
row** (`--repeat 3`). The scenes themselves are chosen in `docs/hero-scenes.md`.

Run from `agent/`:

```
python -m eval.runner                                   # local mode, all scenarios, once
python -m eval.runner --scenario demo-fixture-pivot     # one scenario (repeatable flag)
python -m eval.runner --repeat 3 --json results.json    # the pitch rule, plus machine-readable output
```

Exit code: `0` only if every scenario passes on every repeat, `1` if any check failed, `2` for setup problems (bad
scenario file, unknown id, no scenarios, missing cloud settings).

## Flags and environment

| Flag | Default | Meaning |
| --- | --- | --- |
| `--mode local\|cloud` | `local` | where the scenario runs |
| `--scenario ID` | all | scenario id; repeat the flag for several |
| `--repeat N` | `1` | run the whole suite N times; the summary says whether the 3-green rule is met |
| `--api URL` | env `FIELDWATCH_API_URL` | cloud: API Gateway base URL |
| `--farm-id ID` | the fixture's `farm_id` | cloud: farm to reset and replay on (use the deployed demo farm's id) |
| `--person-id ID` | env `FIELDWATCH_EVAL_PERSON_ID` | cloud: person the scripted replies come from |
| `--timeout SECONDS` | `180` | cloud: per-scenario wait |
| `--json PATH` | off | also write results as JSON |

Cloud also needs env `FIELDWATCH_DASHBOARD_PASSCODE` (the value of SSM `/fieldwatch/dashboard-passcode`).

## What a scenario checks

Per run the runner records: did an investigation start (alert), the top condition, the action type, the seconds from
trigger to the first message to the farmer, and the investigation id. Criteria, each with a plain reason:

- `alert`: observed alert equals `expected.alert`.
- `top_condition`: only if `expected.top_condition` is set.
- `action_type`: only if `expected.action_type` is set (the proposed or created type: `work_order`, `scout_pin`,
  `photo_request`, `insurance_notice`, `none`).
- `trigger_to_message`: only when an alert is expected; must be `<= expected.max_trigger_to_message_s`.
- `run`: appears (failed) when the run itself errored or timed out.

## Local mode

No deployed system. Per scenario: load the farm fixture and field, build a `FieldAnalysisRequest` (the field's polygon
or bbox, crop, `as_of`), call the analyzer through `interfaces.analysis()`, apply the scene pipeline's own decision rule
(`cloud/lambdas/scenes/decide.py` `evaluate`, imported, not copied), and if it alerts run
`investigation.run_request(StartInvestigation)` with an in-memory Recorder (no DynamoDB or EventBridge). Each
`scripted_replies` item is then fed as a `ResumeInvestigation` while the agent is waiting for the farmer.
Trigger-to-message is the wall time from the start of analysis to the first outbound farmer message.

It is not free of side effects: the real analyzer reads imagery over the network and the agent calls Bedrock, so run it
from a configured dev machine (`agent/.env`). Unit tests use fakes for all of it.

## Cloud mode

Run against the deployed API (the lead runs this):

```
cd agent
export FIELDWATCH_API_URL=https://<api-id>.execute-api.us-west-2.amazonaws.com
export FIELDWATCH_DASHBOARD_PASSCODE=<passcode>
python -m eval.runner --mode cloud --farm-id <demo farm id> --repeat 3 --json cloud-results.json
```

Per scenario: `POST /v1/auth/dashboard` (once; again on a 401) -> `POST /v1/replay/reset {farm_id}` ->
`POST /v1/replay {field_ids, as_of, farm_id}` (one per 32 s; the API allows one per 30 s, so a suite of N scenarios takes
at least N x 32 s) -> poll `GET /v1/farms/{farm_id}/overview` and `GET /v1/investigations/{id}` every 3 s up to
`--timeout` -> `POST /v1/mirror/reply {person_id, text, farm_id}` for each scripted reply when the investigation is
`awaiting_reply` -> read the concluded row (`condition`, `proposed_action` or `diagnosis_json`, `message_sent_at`).

The investigation "started after the replay" is the first open investigation on the scenario's field that was not open
right after the reset. Trigger-to-message is the server's `message_sent_at` of the first message minus the client clock
when the replay was requested (large clock skew between the machines shows up as a note). A scenario that expects no
alert passes once the scene analysis shows up on the overview and 20 s pass with no investigation.

## `scripted_replies`

A list, used in order, one per time the agent pauses to ask the farmer:

```json
"scripted_replies": [
  {"text": "YES"},
  {"text": "MORE", "after": "alert", "person_id": "p-dad"}
]
```

- `text` (required): what the farmer texts back (`YES`, `NO`, `MORE`, free text).
- `after` (optional): only `"alert"` is supported (the reply comes once the question was sent); the default.
- `person_id` (optional, cloud): who the reply is from. Otherwise `--person-id`, then the investigation row's
  `person_id`, then the fixture farmer's id or name.

Replies the agent never asks for are not sent; the result notes them.

## Adding a scenario from a `docs/hero-scenes.md` row

1. Copy `scenarios/_template.json` to `scenarios/<scenario-id>.json` (files starting with `_` are skipped).
2. `scenario_id`: the row's short id. `farm_fixture`: a file under `agent/fixtures/` (for cloud runs the farm id and
   field ids must match the deployed demo farm). `field_id`: the field's `id` in that fixture.
3. `as_of`: a date on or after the "clear scene after" in the row (the analyzer uses the latest clear scene on or
   before it).
4. `expected`: from "How we know": `alert` (false for healthy and end-of-season), `top_condition` (a `ConditionId`),
   `action_type` for the action the farmer approves, `max_trigger_to_message_s`.
5. `scripted_replies`: `[{"text": "YES"}]` for scenarios whose action needs approval; `[]` otherwise.
6. `python -m eval.runner --scenario <scenario-id>`; a bad file is reported by name.

`demo-fixture-pivot.json` is the structural example. It uses the placeholder `demo_farm.json`, not a real scene.

## Finding the before/after scenes (`find_scenes`)

Given a field and an event date, list the clear Sentinel-2 dates and pick the nearest clear scene before and after.
"Clear" is the analyzer's own test (cloud/shadow mask inside the 10 m-buffered field), so a listed date is one
`analyze()` accepts. Same-pass tiles are deduplicated. Public data only, no AWS. Run from `agent/`:

```
python -m eval.find_scenes --point 42.164,-96.526 --acres 80 --year 2025          # all clear dates Apr-Oct
python -m eval.find_scenes --geojson field.json --event-date 2025-06-20 --window-days 45
python -m eval.find_scenes --bbox=-96.548,42.152,-96.532,42.168 --event-date 2025-06-20 \
    --analyze --out-dir scenes_out --field-id my-field --scenario-out draft.json
```

- Input: `--geojson` (Polygon, Feature or FeatureCollection), `--bbox W,S,E,N`, or `--point lat,lon --acres N`.
- Range: `--event-date` (brackets it, +/- `--window-days`, default 45), `--year` (Apr 1 to Oct 31), or `--start/--end`.
  `--min-clear` (default 0.8) sets the threshold; `--all` also lists cloudy dates.
- `--analyze` runs `analyze()` on the after-date (or the latest clear date) and prints regions with ring/wedge/edge/
  stripe scores and low-spot and sandy overlap. No layers are passed, so overlaps read 0.00 until layers are wired in.
  Overlay PNGs (true color, health, mask) land under `--out-dir/analysis/<field-id>/<date>/s2/`.
- `--scenario-out` writes a draft `contracts.Scenario` (validated) with `as_of` = the after-date. Set `field_id`,
  `farm_fixture` and `expected.top_condition` / `action_type` by hand, then move it into `scenarios/`.
- Negative longitudes: the CLI handles `--bbox -96.5,...` and `--point -...` itself; `--bbox=` also works.
- Code: `fieldwatch/analysis/scenes.py` (`find_clear_scenes`, `bracket_event`, `draft_scenario`).
