"""WS6: Gateway schemas, the tool Lambda's dispatch and errors, the toolset switch, and SigV4 signing.
No AWS and no network: the Gateway Lambda is called directly with a fake context."""
import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from botocore.credentials import Credentials
from pydantic import BaseModel, Field

from cloud.gateway import handler as H
from cloud.gateway import schemas as S
from fieldwatch import config, registry, toolset

FAKE_CREDS = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "FwoGZXIvYXdzEXAMPLETOKEN")


def ctx(tool: str, target: str = "fw") -> SimpleNamespace:
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"{target}___{tool}"}))


def call(tool: str, args: dict | None = None):
    return H.handler(args or {}, ctx(tool))


# ---------------------------------------------------------------- schemas

def _walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def test_generated_schemas_are_plain_json_schema():
    defs = S.tool_definitions()
    assert {d["name"] for d in defs} == {t.name for t in registry.TOOLS if t.access == "read"}
    for d in defs:
        assert d["description"]
        for path, node in _walk(d["inputSchema"], d["name"]):
            if isinstance(node, dict):
                assert not ({"$ref", "$defs", "anyOf", "allOf", "oneOf", "enum", "title", "default", "const"} & node.keys()), path


def test_every_schema_node_has_a_valid_type_and_only_gateway_keys():
    def check(node, path):
        assert node["type"] in S.VALID_TYPES, path
        assert set(node) <= S.ALLOWED_KEYS, (path, set(node) - S.ALLOWED_KEYS)
        if node["type"] == "object":
            assert set(node.get("required", [])) <= set(node["properties"]), path
            for name, sub in node["properties"].items():
                check(sub, f"{path}.{name}")
        if node["type"] == "array":
            check(node["items"], f"{path}[]")

    for d in S.tool_definitions():
        assert d["inputSchema"]["type"] == "object"
        check(d["inputSchema"], d["name"])


def test_flattening_nested_optional_enum_and_tuple():
    class Inner(BaseModel):
        kind: Literal["a", "b"]

    class Outer(BaseModel):
        inner: Inner
        maybe: Inner | None = None
        count: int | None = Field(default=None, description="how many")
        pair: tuple[float, float] | None = None
        ratio: float = Field(default=0.5, ge=0, le=1)
        anything: object = None

    out = S.gateway_schema(Outer.model_json_schema())
    props = out["properties"]
    assert out["required"] == ["inner"]
    assert props["inner"]["properties"]["kind"] == {"type": "string", "description": "One of: a, b."}
    assert props["maybe"]["type"] == "object" and "required" in props["maybe"]
    assert props["count"] == {"type": "integer", "description": "how many"}
    assert props["pair"]["type"] == "array" and props["pair"]["items"]["type"] == "number"
    assert "From 0 to 1" in props["ratio"]["description"] and "Default: 0.5" in props["ratio"]["description"]
    assert props["anything"]["type"] == "object"


def test_write_tools_are_not_exposed_until_the_switch_is_flipped(monkeypatch):
    assert "create_work_order" not in {d["name"] for d in S.tool_definitions()}
    monkeypatch.setattr(S, "EXPOSED_ACCESS", ("read", "write"))
    defs = {d["name"]: d for d in S.tool_definitions()}
    assert "approval_id" in defs["create_work_order"]["inputSchema"]["required"]  # finding 23


# ---------------------------------------------------------------- handler: dispatch

def test_dispatch_strips_target_prefix_and_returns_model_json():
    out = call("get_hail", {"field_id": "north-pivot", "start": "2026-07-01", "end": "2026-07-10"})
    assert out["source"] == "NOAA MRMS MESH"
    json.dumps(out)


def test_dict_results_pass_through_and_lists_are_wrapped():
    assert call("estimate_loss", {"field_id": "north-pivot", "affected_acres": 10, "yield_loss_fraction": 0.5})["low"] > 0
    listed = call("list_fields")
    assert {f["id"] for f in listed["result"]} >= {"north-pivot"}


def test_typed_model_input_reaches_fn_as_keywords(monkeypatch):
    seen = {}
    spec = registry.BY_NAME["score_hypotheses"]
    monkeypatch.setitem(H.BY_NAME, "score_hypotheses", replace(spec, fn=lambda **kw: seen.update(kw) or {"ok": True}))
    args = {"investigation_id": "inv-1", "crop": "corn", "evidence": []}
    assert call("score_hypotheses", args) == {"ok": True}
    assert seen["investigation_id"] == "inv-1" and seen["evidence"] == []


def test_tool_result_dicts_become_text_and_images_are_summarized(monkeypatch):
    result = {"status": "success", "content": [
        {"text": "Latest pass"}, {"image": {"format": "png", "source": {"bytes": b"x" * 10}}}]}
    monkeypatch.setitem(H.BY_NAME, "look_at_field", replace(registry.BY_NAME["look_at_field"], fn=lambda **kw: result))
    out = call("look_at_field", {"field_id": "north-pivot"})
    assert out == {"result": "Latest pass\n[image omitted: png, 10 bytes]"}


# ---------------------------------------------------------------- handler: errors

def _code(out):
    assert set(out) == {"error"} and set(out["error"]) == {"code", "message", "retryable"}
    return out["error"]["code"]


def test_invalid_input_missing_extra_and_out_of_range():
    assert _code(call("estimate_loss", {"field_id": "x"})) == "INVALID_INPUT"
    assert _code(call("get_soil", {"field_id": "x", "bogus": 1})) == "INVALID_INPUT"
    bad = call("estimate_loss", {"field_id": "x", "affected_acres": 1, "yield_loss_fraction": 2})
    assert _code(bad) == "INVALID_INPUT" and bad["error"]["retryable"] is False


def test_not_found_for_unknown_tool_unexposed_write_tool_and_unknown_field():
    assert _code(call("no_such_tool")) == "NOT_FOUND"
    assert _code(call("create_work_order", {})) == "NOT_FOUND"
    assert _code(call("get_soil", {"field_id": "nowhere"})) == "NOT_FOUND"


def test_missing_tool_name_is_invalid_input():
    assert _code(H.handler({}, SimpleNamespace(client_context=None))) == "INVALID_INPUT"


@pytest.mark.parametrize("exc", [TimeoutError("slow"), httpx.ReadTimeout("slow")])
def test_upstream_timeouts_are_retryable(monkeypatch, exc):
    def boom(**_kw):
        raise exc

    monkeypatch.setitem(H.BY_NAME, "get_hail", replace(registry.BY_NAME["get_hail"], fn=boom))
    out = call("get_hail", {"field_id": "f", "start": "2026-07-01", "end": "2026-07-02"})
    assert _code(out) == "UPSTREAM_TIMEOUT" and out["error"]["retryable"] is True


def test_slow_tool_hits_the_deadline(monkeypatch):
    monkeypatch.setattr(H, "TOOL_TIMEOUT_S", 0.05)
    monkeypatch.setitem(H.BY_NAME, "get_hail", replace(registry.BY_NAME["get_hail"], fn=lambda **kw: time.sleep(0.5)))
    out = call("get_hail", {"field_id": "f", "start": "2026-07-01", "end": "2026-07-02"})
    assert _code(out) == "UPSTREAM_TIMEOUT" and out["error"]["retryable"]


def test_exceptions_with_a_known_code_keep_it_and_bugs_become_internal(monkeypatch):
    def denied(**_kw):
        raise H.ToolError("POLICY_DENIED", "approval is not approved")

    monkeypatch.setitem(H.BY_NAME, "get_hail", replace(registry.BY_NAME["get_hail"], fn=denied))
    assert _code(call("get_hail", {"field_id": "f", "start": "a", "end": "b"})) == "POLICY_DENIED"
    monkeypatch.setitem(H.BY_NAME, "get_hail", replace(registry.BY_NAME["get_hail"], fn=lambda **kw: 1 / 0))
    assert _code(call("get_hail", {"field_id": "f", "start": "a", "end": "b"})) == "INTERNAL"


def test_tool_result_errors_are_upstream_errors(monkeypatch):
    err = {"status": "error", "content": [{"text": "could not fetch imagery"}]}
    monkeypatch.setitem(H.BY_NAME, "look_at_field", replace(registry.BY_NAME["look_at_field"], fn=lambda **kw: err))
    out = call("look_at_field", {"field_id": "north-pivot"})
    assert _code(out) == "UPSTREAM_ERROR" and "imagery" in out["error"]["message"]


# ---------------------------------------------------------------- toolset

def test_local_tools_are_the_registry_read_tools_by_name():
    tools = toolset.tools_for("local")
    assert [t.tool_name for t in tools] == [t.name for t in registry.TOOLS if t.access == "read"]
    assert "create_work_order" not in {t.tool_name for t in tools}
    only = toolset.tools_for("local", names=["list_fields", "get_hail"])
    assert [t.tool_name for t in only] == ["list_fields", "get_hail"]


def test_wrapped_stub_tool_runs_through_strands_stream():
    (tool,) = toolset.tools_for("local", names=["get_hail"])
    assert tool.tool_spec["inputSchema"]["json"]["properties"]["field_id"]

    async def run(payload):
        events = [e async for e in tool.stream({"toolUseId": "t1", "name": "get_hail", "input": payload}, {})]
        return events[-1]["tool_result"]

    ok = asyncio.run(run({"field_id": "f", "start": "2026-07-01", "end": "2026-07-02"}))
    assert ok["status"] == "success" and json.loads(ok["content"][0]["text"])["source"] == "NOAA MRMS MESH"
    bad = asyncio.run(run({"field_id": "f"}))
    assert bad["status"] == "error"


def test_default_backend_follows_config(monkeypatch):
    monkeypatch.setattr(config, "TOOLS_BACKEND", "local")
    assert toolset.tools_for()
    with pytest.raises(ValueError):
        toolset.tools_for("carrier-pigeon")


def test_gateway_unreachable_falls_back_to_local(monkeypatch, caplog):
    monkeypatch.setattr(config, "GATEWAY_URL", "")
    assert [t.tool_name for t in toolset.tools_for("gateway")] == [t.name for t in registry.TOOLS if t.access == "read"]
    assert toolset.tools_for("gateway", connect=lambda: None)


def test_gateway_unreachable_url_logs_and_falls_back(monkeypatch):
    # nothing listens on port 9; the MCP client fails to start and gateway_tools returns None
    monkeypatch.setattr(config, "GATEWAY_URL", "http://127.0.0.1:9")
    assert toolset.gateway_tools(auth=toolset.GatewaySigV4Auth(credentials=FAKE_CREDS), startup_timeout=3) is None


def test_gateway_backend_uses_remote_tools_and_filters_by_name():
    remote = [SimpleNamespace(tool_name="get_hail"), SimpleNamespace(tool_name="get_weather")]
    assert toolset.tools_for("gateway", connect=lambda: remote) == remote
    assert toolset.tools_for("gateway", names=["get_hail"], connect=lambda: remote) == remote[:1]


def test_prefix_and_url_helpers():
    assert toolset.strip_target_prefix("fw___get_weather") == "get_weather"
    assert toolset.strip_target_prefix("get_weather") == "get_weather"
    assert toolset.gateway_mcp_url("https://gw.example/") == "https://gw.example/mcp"
    assert toolset.gateway_mcp_url("https://gw.example/mcp") == "https://gw.example/mcp"


def test_gateway_client_shows_unprefixed_names(monkeypatch):
    from mcp.types import Tool
    from strands.tools.mcp import MCPClient
    from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
    from strands.types import PaginatedList

    client_cls = toolset._gateway_client_class()
    client = client_cls(url="https://gw.example/mcp", auth_provider=toolset.GatewaySigV4Auth(credentials=FAKE_CREDS))
    mcp_tool = Tool(name="fw___get_weather", description="d", inputSchema={"type": "object", "properties": {}})
    monkeypatch.setattr(MCPClient, "list_tools_sync",
                        lambda self, *a, **k: PaginatedList[MCPAgentTool]([MCPAgentTool(mcp_tool, self)], token=None))
    (tool,) = client.list_tools_sync()
    assert tool.tool_name == "get_weather" and tool.mcp_tool.name == "fw___get_weather"


# ---------------------------------------------------------------- SigV4

def test_sigv4_adds_authorization_and_date_headers():
    auth = toolset.GatewaySigV4Auth("us-west-2", credentials=FAKE_CREDS)
    request = httpx.Request("POST", "https://gw-123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    signed = next(auth.auth_flow(request))
    header = signed.headers["authorization"]
    assert header.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-west-2/bedrock-agentcore/aws4_request" in header
    assert "SignedHeaders=" in header and "host" in header and "x-amz-date" in header
    assert len(signed.headers["x-amz-date"]) == 16
    assert signed.headers["x-amz-security-token"] == "FwoGZXIvYXdzEXAMPLETOKEN"


def test_sigv4_signature_depends_on_body_and_works_through_httpx_client():
    seen = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(server), auth=toolset.GatewaySigV4Auth("us-west-2", credentials=FAKE_CREDS))
    url = "https://gw.example.amazonaws.com/mcp"
    client.post(url, json={"method": "tools/list"})
    client.post(url, json={"method": "tools/call"})
    a, b = seen
    assert a.headers["authorization"] and b.headers["authorization"]
    assert a.headers["authorization"].rsplit("Signature=", 1)[1] != b.headers["authorization"].rsplit("Signature=", 1)[1]


def test_sigv4_without_credentials_fails_clearly(monkeypatch):
    import boto3

    monkeypatch.setattr(boto3.Session, "get_credentials", lambda self: None)
    with pytest.raises(RuntimeError, match="credentials"):
        next(toolset.GatewaySigV4Auth("us-west-2").auth_flow(httpx.Request("POST", "https://gw.example/mcp", content=b"{}")))
