# FieldWatch

Strands agents that watch a farm's fields from satellite, diagnose what's wrong with
three specialists, and text the farmer in plain words. Every action waits for a YES.

## What's here

| Path | What |
| --- | --- |
| `fieldwatch/agents.py` | Supervisor + imagery/weather/soil specialists (agents-as-tools) |
| `fieldwatch/tools.py` | Read tools (fixture-backed today) and write tools that **interrupt** for approval |
| `fieldwatch/hooks.py` | `ToolTimeline` hook: evidence cards for the dashboard, write-tool guard |
| `fieldwatch/schemas.py` | `Diagnosis` structured output; `to_text()` is the SMS |
| `fieldwatch/investigation.py` | `start()` pauses on the interrupt, `reply()` resumes from a file session |
| `app.py` | AgentCore Runtime entrypoint |
| `fixtures/demo_farm.json` | The demo farm: north pivot, Johnson quarter, east 80 |
| `tests/test_smoke.py` | Whole loop with a fake model, no AWS needed |

Strands features on show: agents-as-tools, interrupts (human-in-the-loop), hooks,
structured output, session management, per-invocation limits. Next up: Graph for the
investigation, bidirectional streaming (Nova Sonic) for Grandpa's phone call.

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
3. **Model access**: Console > Amazon Bedrock > Model access > enable Claude Haiku 4.5 and
   Claude Sonnet 4.5 (and Nova 2 Sonic if doing voice). Region us-west-2.
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
