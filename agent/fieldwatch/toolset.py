"""One switch for where agent tools run (docs/decisions.md D1, workstream 6).

    tools = tools_for("local")     # registry tools, called in-process
    tools = tools_for("gateway")   # the same tools through AgentCore Gateway (MCP over SigV4)
    tools = tools_for()            # config.TOOLS_BACKEND (FIELDWATCH_TOOLS_BACKEND), default "local"

Both return a plain list of Strands tools, so they mix with the caller's own tools:
`Agent(tools=tools_for() + [my_tool])`. Gateway tool names come back WITHOUT the `<target>___` prefix, so
prompts say `get_weather` in both modes. If the Gateway is unreachable at startup we log and fall back to
local, so a demo never dies on a cold Gateway.

Shared by the Gateway Lambda (`cloud/gateway/handler.py`): `call_spec` and `render` are the one place a
registry entry is validated, called and turned into JSON, so local and cloud behave the same.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import httpx
from pydantic_core import to_jsonable_python

from . import config
from .registry import TOOLS, ToolSpec

log = logging.getLogger(__name__)

TARGET_SEPARATOR = "___"  # Gateway names tools `<target>___<tool>` (spike finding 22)
SIGV4_SERVICE = "bedrock-agentcore"  # spike finding 21


# ---------------------------------------------------------------- calling a registry entry

def strip_target_prefix(name: str) -> str:
    """`fw___get_weather` -> `get_weather`; names without the separator pass through."""
    return name.split(TARGET_SEPARATOR, 1)[1] if TARGET_SEPARATOR in name else name


def call_spec(spec: ToolSpec, arguments: dict | None) -> Any:
    """Validate `arguments` with the entry's input_model, then call its fn with the top-level fields as keywords.

    Nested models stay models (the same thing Strands hands a typed @tool parameter). Raises
    pydantic.ValidationError on bad input.
    """
    model = spec.input_model.model_validate(arguments or {})
    return spec.fn(**{name: getattr(model, name) for name in type(model).model_fields})


class ToolResultError(Exception):
    """A Strands ToolResult that reported status 'error'."""


def _is_tool_result(value: Any) -> bool:
    return (isinstance(value, dict) and value.get("status") in ("success", "error")
            and isinstance(value.get("content"), list))


def _result_text(result: dict) -> str:
    parts: list[str] = []
    for block in result["content"]:
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(to_jsonable_python(block["json"])))
        elif "image" in block:
            img = block["image"]
            size = len(img.get("source", {}).get("bytes", b"") or b"")
            parts.append(f"[image omitted: {img.get('format', 'png')}, {size} bytes]")
        else:
            parts.append(f"[{next(iter(block), 'content')} omitted]")
    return "\n".join(parts)


def render(value: Any) -> Any:
    """Turn a tool's return value into JSON-able data: pydantic -> model_dump(mode='json'); Strands ToolResult
    dicts -> their text (images summarized); everything else through pydantic's JSON conversion."""
    if _is_tool_result(value):
        text = _result_text(value)
        if value["status"] == "error":
            raise ToolResultError(text)
        return text
    return to_jsonable_python(value)


# ---------------------------------------------------------------- local backend

def _registry_tool_class():
    from strands.types._events import ToolResultEvent
    from strands.types.tools import AgentTool

    class RegistryTool(AgentTool):
        """A registry entry whose fn isn't already a Strands tool (the stubs), exposed by registry name."""

        def __init__(self, spec: ToolSpec) -> None:
            super().__init__()
            self.spec = spec

        @property
        def tool_name(self) -> str:
            return self.spec.name

        @property
        def tool_spec(self):
            return {"name": self.spec.name, "description": self.spec.description,
                    "inputSchema": {"json": self.spec.input_model.model_json_schema()}}

        @property
        def tool_type(self) -> str:
            return "python"

        async def stream(self, tool_use, invocation_state, **kwargs):
            try:
                out = render(call_spec(self.spec, tool_use.get("input")))
                status, text = "success", out if isinstance(out, str) else json.dumps(out)
            except Exception as e:  # tool errors go back to the model, not up the stack
                status, text = "error", f"{type(e).__name__}: {e}"
            yield ToolResultEvent({"toolUseId": tool_use["toolUseId"], "status": status, "content": [{"text": text}]})

    return RegistryTool


def _is_strands_tool(fn: Any) -> bool:
    from strands.types.tools import AgentTool

    return isinstance(fn, AgentTool)


def local_tools(*, access: Iterable[str] = ("read",), names: Iterable[str] | None = None) -> list:
    """Registry entries as Strands tools. Entries whose fn is already a Strands tool (tools.py) are returned
    as is; the rest (stubs, later real functions) are wrapped under the registry name and input schema."""
    wanted = set(names) if names is not None else None
    allowed = set(access)
    wrapper = None
    out = []
    for spec in TOOLS:
        if spec.access not in allowed or (wanted is not None and spec.name not in wanted):
            continue
        if _is_strands_tool(spec.fn):
            out.append(spec.fn)
        else:
            wrapper = wrapper or _registry_tool_class()
            out.append(wrapper(spec))
    return out


# ---------------------------------------------------------------- gateway backend

class GatewaySigV4Auth(httpx.Auth):
    """Signs each MCP request with SigV4 for `bedrock-agentcore` (spike findings 21 and 34).

    Credentials come from `credentials` (a botocore Credentials/Refreshable object, or anything with
    access_key/secret_key/token), else the default boto3 session, re-read on every request so refreshed or
    assumed-role credentials keep working. Needs `botocore[crt]` for `aws login` credentials (finding 25).
    """

    requires_request_body = True  # the payload hash is part of the signature

    def __init__(self, region: str | None = None, service: str = SIGV4_SERVICE, credentials: Any = None) -> None:
        self.region = region or config.REGION
        self.service = service
        self._credentials = credentials

    def _frozen(self):
        creds = self._credentials
        if creds is None:
            import boto3

            creds = boto3.Session().get_credentials()
        if creds is None:
            raise RuntimeError("no AWS credentials found to sign the Gateway request")
        return creds.get_frozen_credentials() if hasattr(creds, "get_frozen_credentials") else creds

    def auth_flow(self, request: httpx.Request):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers={"Content-Type": request.headers.get("content-type", "application/json")},
        )
        SigV4Auth(self._frozen(), self.service, self.region).add_auth(aws_request)
        for header in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token"):
            if header in aws_request.headers:
                request.headers[header] = aws_request.headers[header]
        yield request


def gateway_mcp_url(url: str) -> str:
    url = url.rstrip("/")
    return url if url.endswith("/mcp") else f"{url}/mcp"


def _gateway_client_class():
    from strands.tools.mcp import MCPClient
    from strands.tools.mcp.mcp_agent_tool import MCPAgentTool
    from strands.types import PaginatedList

    class GatewayMCPClient(MCPClient):
        """MCPClient that shows the agent `get_weather`, not `fw___get_weather` (calls still use the full name)."""

        def list_tools_sync(self, pagination_token=None, prefix=None, tool_filters=None):
            page = super().list_tools_sync(pagination_token, prefix, tool_filters)
            renamed = [MCPAgentTool(t.mcp_tool, self, name_override=strip_target_prefix(t.mcp_tool.name)) for t in page]
            return PaginatedList[MCPAgentTool](renamed, token=page.pagination_token)

    return GatewayMCPClient


def gateway_tools(url: str | None = None, *, auth: httpx.Auth | None = None, startup_timeout: int = 20) -> list | None:
    """Connect to the Gateway and return its tools, or None (with a log line) if it can't be reached.
    The client stays open for the life of the process; the returned tools hold it."""
    url = url or config.GATEWAY_URL
    if not url:
        log.warning("FIELDWATCH_GATEWAY_URL is not set; Gateway tools unavailable")
        return None
    client = None
    try:
        client = _gateway_client_class()(
            url=gateway_mcp_url(url), auth_provider=auth or GatewaySigV4Auth(), startup_timeout=startup_timeout
        )
        client.start()
        tools, token = [], None
        while True:
            page = client.list_tools_sync(token)
            tools.extend(page)
            token = page.pagination_token
            if token is None:
                break
        if not tools:
            raise RuntimeError("the Gateway lists no tools")
        return tools
    except Exception as e:
        log.warning("Gateway unreachable (%s: %s); falling back to local tools", type(e).__name__, e)
        if client is not None:
            try:
                client.stop(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        return None


def tools_for(backend: str | None = None, *, access: Iterable[str] = ("read",),
              names: Iterable[str] | None = None, connect: Callable[..., list | None] | None = None) -> Sequence:
    """The agent's registry tools for `backend` ("local" | "gateway"; default config.TOOLS_BACKEND).

    `access` picks registry entries by access ("read" by default; the Gateway decides for itself which it lists)
    and `names` restricts to those tool names, in either backend. `connect` is a test hook for the Gateway client.
    """
    backend = (backend or config.TOOLS_BACKEND).lower()
    if backend not in ("local", "gateway"):
        raise ValueError(f"unknown tools backend {backend!r}; use 'local' or 'gateway'")
    if backend == "gateway":
        remote = (connect or gateway_tools)()
        if remote is not None:
            wanted = set(names) if names is not None else None
            return [t for t in remote if wanted is None or t.tool_name in wanted]
    return local_tools(access=access, names=names)
