# agent/cloud/agentcore: the FieldWatch agent on AgentCore Runtime

An AgentCore CLI project (CLI 0.31, CodeZip, Strands, Bedrock, HTTP). It was made with

```bash
agentcore create --name fieldwatch_agent --project-name FieldWatch --framework Strands --model-provider Bedrock \
  --build CodeZip --protocol HTTP --memory none --language Python --skip-git --skip-python-setup --skip-install
```

then adapted: the template's `main.py`, model loader, skills and the third-party `mcp.exa.ai` client are gone, and the
deployed code is the real agent. See `docs/decisions.md` D5/D6 and spike findings 29-34.

```
agentcore/agentcore.json        runtime "fieldwatch_agent": entrypoint app.py, PYTHON_3_12, env vars, tags, extra IAM policy
agentcore/aws-targets.json      NOT in git: written by scripts/prepare_deploy.py (holds the account ID)
agentcore/cdk/                  generated CDK app the CLI deploys (untouched)
app/fieldwatch_agent/           the codeLocation
    pyproject.toml              runtime dependencies (kept light; no rasterio)
    policies/runtime-access.json  extra permissions for the Runtime role (see below)
    app.py, fieldwatch/, catalog/, fixtures/   STAGED from agent/ by scripts/stage.py (gitignored, never edit here)
scripts/stage.py                copy agent/app.py + agent/fieldwatch + catalog + contract samples into the codeLocation
scripts/prepare_deploy.py       stage, then write aws-targets.json from `aws sts get-caller-identity`
.cli-tools/package.json         where `@aws/agentcore` is installed (node_modules is gitignored)
```

There is one copy of the source (agent/); the staging step copies it, so the zip contains exactly what the tests ran.

## Local setup (no AWS)

```bash
cd agent/cloud/agentcore/.cli-tools && npm install --no-fund --no-audit        # the CLI, project-local
pip install uv                                                                   # CodeZip packing needs uv
export PATH="$PWD/node_modules/.bin:$PATH"; cd ..
python scripts/stage.py
agentcore validate -d . < /dev/null       # needs agentcore/aws-targets.json; any 12-digit placeholder works locally
agentcore package  -d . < /dev/null       # builds the Linux ARM64 zip (~41 MB); no AWS calls
```

Close stdin (`< /dev/null`) or `--help` can hang (finding 35). Delete `agentcore/fieldwatch_agent*` and the placeholder
`aws-targets.json` afterwards: they are gitignored, but `infra`'s Lambda bundler copies `agent/cloud` wholesale
(see "Known gaps").

## Deploy (Caleb only; docs/decisions.md D3)

Prerequisites: `FieldWatch-DataPlane` and `FieldWatch-Agent` deployed (`npx aws-cdk@2 deploy FieldWatch-DataPlane FieldWatch-Agent`
from `infra/`), the CDK bootstrap done (infra/README.md), Anthropic use-case form accepted (finding 28).

```bash
cd agent/cloud/agentcore
export PATH="$PWD/.cli-tools/node_modules/.bin:$PATH"
python scripts/prepare_deploy.py          # stage + aws-targets.json for the account of your current credentials
agentcore deploy -y < /dev/null           # creates stack AgentCore-FieldWatch-default (first deploy ~100 s)
agentcore status < /dev/null              # note the runtime ARN: arn:aws:bedrock-agentcore:us-west-2:<acct>:runtime/fieldwatch_agent-<id>
# first time (creates the parameter the session starter reads):
aws ssm put-parameter --name /fieldwatch/runtime-arn --type String --value <runtime ARN> --tags Key=project,Value=fieldwatch
# if the ARN ever changes: same command with --overwrite and without --tags
```

The session starter Lambda caches the parameter per container, so a new ARN is picked up on its next cold start
(or bump any env var on the function). Redeploying code keeps the same ARN.

Code-only redeploys: `python scripts/prepare_deploy.py && agentcore deploy -y` (~60 s).

### Checks after deploying

Invoke it directly (this is exactly what the session starter does; `agentcore invoke` also works, it wraps the text as
`{"prompt": "<json>"}` and the agent unwraps that):

```bash
echo '{"action":"start","investigation_id":"inv-smoke-test-0000000000001","farm_id":"demo","field":"north pivot"}' > start.json
aws bedrock-agentcore invoke-agent-runtime --region us-west-2 --agent-runtime-arn <runtime ARN> \
  --runtime-session-id fieldwatch-session-inv-smoke-test-0000000000001 --content-type application/json \
  --payload fileb://start.json out.json && cat out.json
# expect {"accepted": true, "investigation_id": "...", "action": "start"} within a few seconds
agentcore logs < /dev/null     # the agent working: "Async task started: investigation:..."
agentcore traces < /dev/null   # tracing is on (enableOtel)
aws dynamodb get-item --region us-west-2 --table-name fw-investigations \
  --key '{"investigation_id":{"S":"inv-smoke-test-0000000000001"}}'
# status running -> awaiting_reply with pending_interrupt_id; InvestigationProgress events appear on the `fieldwatch` bus
```

Resume it (same session id; `<id>` is `pending_interrupt_id`): payload
`{"action":"reply","investigation_id":"inv-smoke-test-0000000000001","interrupt_id":"<id>","text":"YES"}`.
Run it again after more than the idle timeout (30 min) to prove the S3 session restores the paused agent on a fresh microVM.

Session ids must be at least 33 characters (finding 32); the starter uses `fieldwatch-session-<investigation_id>`.

## Runtime request and response

`app.py` accepts `contracts.StartInvestigation` (`action: "start"`) and `contracts.ResumeInvestigation` (`action: "reply"`).
It acknowledges immediately (`{"accepted": true, ...}`), runs the investigation on a background thread registered with
`app.add_async_task` (so `/ping` says `HealthyBusy`), and reports through the bus and `fw-investigations`:

| Where | What |
| --- | --- |
| bus `fieldwatch`, source `fieldwatch.agent`, `InvestigationProgress` | one per tool start/end plus plan / waiting / done / error; `seq` is an atomic counter on the item |
| bus, `InvestigationConcluded` | when the diagnosis exists (after the farmer's answer in the current agent) |
| `fw-investigations` | `status` queued -> running -> awaiting_reply (`pending_interrupt_id`, `last_text`) -> resuming -> concluded / failed, `diagnosis_json` |

The farmer text for each pause is `last_text` (it is also in the `AgentStep`, but nothing returns it to a caller because
the call is a fire-and-acknowledge). `FIELDWATCH_RUNTIME_SYNC=1` returns the `AgentStep` instead (local `agentcore dev`).

## Configuration

Env vars are in `agentcore.json` (`FIELDWATCH_MODEL`, `FIELDWATCH_TOOLS_BACKEND`, `FIELDWATCH_EVENT_BUS`, ...). Change the
model by editing `FIELDWATCH_MODEL` and redeploying (D4: default Haiku 4.5; demo supervisor Sonnet 4.5
`us.anthropic.claude-sonnet-4-5-20250929-v1:0`; fallback `us.amazon.nova-pro-v1:0`).

Account-specific values are not in the project. With `FIELDWATCH_SSM_BOOTSTRAP=1` the agent reads, at startup:
`/fieldwatch/sessions-bucket` (written by `FieldWatch-Agent`), and optionally `/fieldwatch/bucket`, `/fieldwatch/gateway-url`
(the Gateway lane writes the URL), into the matching `FIELDWATCH_*` variables unless already set. With a sessions bucket
set, sessions go to S3 `sessions/`; otherwise to local files (lost when the microVM goes).

## Runtime role permissions

`agentcore.json` lists `policies/runtime-access.json` under `additionalPolicies`: the CLI attaches it as an inline policy
to the Runtime execution role it creates. It grants Bedrock invoke, `bedrock-agentcore:InvokeGateway`, `events:PutEvents`
on the `fieldwatch` bus, read/write on `fw-investigations`, the `sessions/` prefix of the data-plane bucket
(`fieldwatch-dataplane-bucket*`), and three SSM parameters. Account IDs are wildcards so the file is the same in every
account. To add more (Memory, other tables), edit that file and redeploy. The `fieldwatch-boundary` permits all of it.

## Memory (episodic) and tracing

Tracing is on (`instrumentation.enableOtel`, ADOT bundled via `aws-opentelemetry-distro`). The first invoke switches the
account to CloudWatch Transaction Search (finding 37): set span indexing to a deliberate percentage.

AgentCore Memory is **not added yet** (D9 cut order keeps S3 sessions). To add it:

1. `agentcore add memory --name FieldWatchMemory --strategies EPISODIC,USER_PREFERENCE,SEMANTIC --expiry 90 < /dev/null`
   (EPISODIC needs `reflectionNamespaceTemplates`, e.g. `/fieldwatch/farm/{actorId}/episodes`, in `agentcore.json`).
2. Add `bedrock-agentcore:CreateEvent`, `ListEvents`, `RetrieveMemoryRecords` on the memory ARN to `runtime-access.json`.
3. In `investigation.py`, write one event per finished investigation (`actorId = farm_id`, `sessionId = investigation_id`) and
   retrieve the top episodes for the same field at start; keep S3 for the interrupt state, since the session manager must
   stay S3 (Memory is not the system of record, specs "Memory mapping").

## Known gaps

- `infra/stacks/common.py` bundles all of `agent/cloud` into every Python Lambda; add `"agentcore"` to its `_IGNORE` list
  or the staged agent copy and the ~41 MB zip ride along whenever they exist on disk.
- `agents.py` hard-codes its tools, so `FIELDWATCH_TOOLS_BACKEND=gateway` only takes effect once `build_supervisor` accepts
  a `tools=` argument (investigation.py already passes `toolset.tools_for(...)` when it does).
- Nothing in the agent sends the farmer text yet (no `send_farmer_message` tool in `agents.py`); it is stored in
  `fw-investigations.last_text`.
