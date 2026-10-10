"""AgentCore Runtime entrypoint. Deploy with: agentcore configure -e app.py && agentcore deploy

Payloads:
  {"action": "start", "investigation_id": "abc", "field": "north pivot"}
  {"action": "reply", "investigation_id": "abc", "interrupt_id": "...", "text": "YES"}
"""
from dataclasses import asdict

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from fieldwatch.investigation import reply, start

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    action = payload.get("action", "start")
    inv = payload["investigation_id"]
    if action == "start":
        step = start(inv, payload["field"], emit=lambda e: None)
    elif action == "reply":
        step = reply(inv, payload["interrupt_id"], payload.get("text", ""), emit=lambda e: None)
    else:
        return {"error": f"unknown action {action}"}
    out = asdict(step)
    if step.diagnosis:
        out["diagnosis"] = step.diagnosis.model_dump()
    return out


if __name__ == "__main__":
    app.run()
