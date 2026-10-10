# FarmU / FieldWatch

Agents that watch a farm's fields from satellite, figure out what's wrong, and text the farmer in plain words. Every action waits for a YES.

| Folder | What |
| --- | --- |
| `agent/` | Strands agents, tools, contracts and the web app (sign-up map, dashboard, phone mirror). Start with `agent/README.md`. |
| `infra/` | CDK app: shared data plane and per-workstream stacks (`infra/README.md`). |
| `docs/` | The plan (system design, specs, data pipelines, features, build plan), build decisions, spike findings, workstreams. |

**Architecture as built:** the web app is the hub; the original plan's serverless backend (pipelines, AgentCore Runtime and Gateway, DynamoDB) plugs in behind switches, and the current local code is the fallback. See `docs/decisions.md`. Working in parallel? See `docs/workstreams.md` and `CLAUDE.md`.

Quick start:

```powershell
cd agent
python -m pip install -r requirements.txt pytest
python -m pytest tests -q -m "not live"   # no AWS needed
python -m uvicorn web.server:app --port 8000
```
