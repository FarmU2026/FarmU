# FarmU

Agent to help farmers. See the two FieldWatch docs for the product and the build plan.

| Folder | What |
| --- | --- |
| `agent/` | Strands agents: supervisor + specialists, interrupt-based farmer approval, AgentCore entrypoint. Start with `agent/README.md`. |

Quick start:

```powershell
cd agent
python -m pip install -r requirements.txt pytest
python -m pytest tests -q          # no AWS needed
python -m fieldwatch "north pivot" # needs `aws configure` + Bedrock model access
```
