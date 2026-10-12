# FieldWatch

Strands agents that watch a farm's fields from satellite, diagnose what's wrong with
three specialists, and text the farmer in plain words. Every action waits for a YES.

## What's here

| Path | What |
| --- | --- |
| `fieldwatch/agents.py` | Supervisor + satellite, weather & soil, and crop-health specialists (agents-as-tools); scoring tools; `DiagnosisGuard` |
| `fieldwatch/tools.py` | Local read tools and the write tools that **interrupt** for the farmer's approval |
| `fieldwatch/toolset.py` | Gateway (MCP) tools for the agents in AWS |
| `fieldwatch/hooks.py` | `ToolTimeline` (live progress), `TokenBudget` |
| `fieldwatch/investigation.py` | Runs an investigation: `start()` pauses on the interrupt, `reply()` resumes (S3 sessions on Runtime, file sessions locally); question runs for chat |
| `fieldwatch/analysis/` | Satellite field analyzer (see `CALIBRATION.md`) |
| `fieldwatch/scoring.py`, `catalog/` | Deterministic hypothesis scorer, condition catalog, impact tables |
| `fieldwatch/context/`, `hazards/`, `notify/` | Field context data, hail and drought, notifications |
| `fieldwatch/contracts.py`, `interfaces.py` | Shared contracts and cross-workstream interfaces |
| `cloud/` | Lambda handlers, Gateway and the AgentCore Runtime project |
| `app.py` | AgentCore Runtime entrypoint |
| `web/server.py` | Local dev harness (FastAPI); the deployed UI is in `../web/` |
| `tests/` | Test suite; `tests/test_smoke.py` runs the whole loop with a fake model, no AWS needed |

Strands features on show: agents-as-tools, interrupts (human-in-the-loop) with session
persistence, hooks, structured output, MCP tools via AgentCore Gateway, per-invocation limits.

## Web UI (sign up a farm, get live updates)

```powershell
cd agent
copy .env.example .env      # then paste the Bedrock API key into .env
python -m uvicorn web.server:app --port 8000
start http://localhost:8000
```

1. Type a name and phone number.
2. Click the corners of a field on the satellite map, name it, press **Add field**. Repeat.
3. **Start watching**: the latest cloud-free Sentinel-2 pass is pulled, clipped to each boundary,
   and the first "new satellite pass" text lands in the phone panel.
4. **ASK** (or "Ask FieldWatch about this field") runs the Strands investigation live. The agent's
   steps stream into the phone as grey lines; its approval question arrives as a text; **YES** / **NO**
   resumes the paused agent and the diagnosis follows.
5. A background thread re-checks the catalog every 10 minutes; a new pass becomes a new text.
   **Check satellite now** forces a check.

State lives in `agent/data/` (gitignored), one JSON per farm keyed by phone number.

### Real texts

Every message FieldWatch posts to the phone panel is also sent as an SMS to the number the farmer
signed up with (`fieldwatch/sms.py`). Put Twilio credentials in `.env` and it is two-way:

1. https://console.twilio.com, free trial, get a trial number, **verify** each phone you will text
   (Phone Numbers > Verified Caller IDs). Trial texts carry a short "Sent from a Twilio trial" prefix.
2. In `.env`: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM=+1...`. Restart the server.
3. Reply from the phone with **ASK**, **ASK <field name>**, **YES**, **NO**, **MORE**, **STOP**.
   Replies are polled from Twilio every 5s, so no public URL is needed. With ngrok, point the
   number's inbound webhook at `/sms/inbound` instead.

One-way fallbacks: `TEXTBELT_KEY=textbelt` (one free text a day, no account) or IAM keys plus
`FIELDWATCH_SMS=sns`. The Bedrock API key cannot send SMS; SNS needs real IAM keys.

## Models and cost

No sponsor code, so the budget is the AWS free-tier credit: $100 at sign-up, plus $20 for each
of five onboarding tasks (one is "run a prompt in Bedrock"). Pick the **free plan** at sign-up:
it cannot bill you, it just stops when credits run out. Bedrock is eligible.

| Model | Price per 1M tokens in/out | One investigation (~40k tokens) |
| --- | --- | --- |
| Nova Lite | $0.06 / $0.24 | under a cent |
| Claude Haiku 4.5 (default) | $1 / $5 | about 5 cents |
| Claude Sonnet 4.5 (demo) | $3 / $15 | about 15 cents |

So $100 is hundreds of runs. The real risk is a runaway loop, which `LIMITS` in
`investigation.py` caps at 20 turns. Set the budget alarm anyway.

Switch models with env vars (see `.env.example`):

```powershell
$env:FIELDWATCH_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"   # demo quality
$env:FIELDWATCH_PROVIDER = "ollama"; $env:FIELDWATCH_OLLAMA_MODEL = "qwen3:8b"   # local, no AWS
```

Local Ollama works for wiring tests but this laptop has no discrete GPU, so an 8B model runs on
CPU at a few tokens per second, and small models are unreliable at the multi-step tool calling
the supervisor needs. Use Ollama to save credits while building UI and plumbing, and Bedrock
for anything that exercises the agents.

## 1. One-time AWS setup (browser, about 15 min)

1. **Account**: https://aws.amazon.com/free, Create account, choose the **free plan**.
   Needs a card and phone verification. The $100 credit appears under Billing > Credits.
2. **Extra $100**: complete the five onboarding tasks shown on the console home page.
3. **Model access**: Console > Amazon Bedrock > Model catalog > any Claude model > submit the
   one-time Anthropic use-case form (done for this account), then Bedrock > API keys > generate a
   short-term key (12 h, one region) and paste it into `agent/.env`.
4. **Access keys**: Console > IAM > Users > Create user `hackathon` > attach
   AdministratorAccess (hackathon only) > Security credentials > Create access key > CLI.
5. **Budget alarm**: Console > Billing > Budgets > create a $50 alert so a loop can't burn the credits.

## 2. Local setup

```powershell
cd FarmU/agent
aws configure                  # paste the access key, secret, region us-west-2, output json
aws sts get-caller-identity    # should print your account id
python -m pip install -r requirements.txt pytest
python -m pytest tests -q      # passes with no AWS
python -m fieldwatch "north pivot"   # real run against Bedrock
```

## 3. Deploy to AgentCore Runtime

```powershell
agentcore configure -e app.py -n fieldwatch
agentcore deploy
agentcore invoke '{"action":"start","investigation_id":"demo1","field":"north pivot"}'
agentcore invoke '{"action":"reply","investigation_id":"demo1","interrupt_id":"<from above>","text":"YES"}'
```

## Payloads

```json
{"action": "start", "investigation_id": "abc", "field": "north pivot"}
{"action": "reply", "investigation_id": "abc", "interrupt_id": "...", "text": "YES"}
```
