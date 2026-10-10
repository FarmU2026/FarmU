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

## 1. One-time AWS setup (browser, about 15 min)

1. **Account**: https://aws.amazon.com/free, Create account. Needs a card and phone verification.
2. **Credits**: get the promo code from the AWS booth. Console > Billing > Credits > Redeem.
3. **Model access**: Console > Amazon Bedrock > Model access > enable Claude Sonnet 4.5
   (and Nova 2 Sonic if doing voice). Region us-west-2.
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
