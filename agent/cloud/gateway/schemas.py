"""Gateway inline tool schemas, generated from the registry (the one list; docs/workstreams.md WS6).

AgentCore Gateway's SchemaDefinition accepts only `type`, `properties`, `required`, `items` and `description`
(botocore `SchemaDefinition`; no `enum`, no `$ref`, no `anyOf`), and every node needs a `type`. So Pydantic's
JSON Schema is flattened here: `$ref`/`$defs` inlined, Optional (`anyOf` with null) collapsed to the non-null
type, titles/defaults/formats dropped. Information the Gateway can't carry (enum values, defaults, ranges) moves
into the property's `description`, so the model still sees it; the Lambda re-validates everything with the
registry's Pydantic model anyway (INVALID_INPUT).

Which tools the Gateway exposes: EXPOSED_ACCESS. Write tools arrive with workstream 8; adding them is the one-line
change `EXPOSED_ACCESS = ("read", "write")` (the CDK stack and the handler both read it from here).
"""
from __future__ import annotations

from typing import Any

from fieldwatch.registry import TOOLS, ToolSpec

EXPOSED_ACCESS: tuple[str, ...] = ("read",)

VALID_TYPES = {"string", "number", "integer", "boolean", "object", "array"}
ALLOWED_KEYS = {"type", "properties", "required", "items", "description"}


def exposed_specs() -> list[ToolSpec]:
    return [t for t in TOOLS if t.access in EXPOSED_ACCESS]


def exposed_names() -> set[str]:
    return {t.name for t in exposed_specs()}


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _resolve(node: dict, defs: dict, seen: frozenset[str]) -> tuple[dict, frozenset[str]]:
    """Follow $ref / allOf / anyOf / oneOf / type lists until `node` is a concrete schema; sibling keys
    (description, default) of the wrapper win over the target's."""
    while True:
        extra = {k: v for k, v in node.items() if k not in ("$ref", "anyOf", "oneOf", "allOf")}
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name in seen or name not in defs:  # recursive or dangling: give up on the structure
                return {"type": "object", **extra}, seen
            target, seen = defs[name], seen | {name}
        elif "allOf" in node:
            parts = node["allOf"]
            target = parts[0] if len(parts) == 1 else _merge_all(parts, defs, seen)
        elif "anyOf" in node or "oneOf" in node:
            options = node.get("anyOf") or node.get("oneOf") or []
            non_null = [o for o in options if o.get("type") != "null" and o.get("const", 0) is not None]
            target = non_null[0] if non_null else {"type": "string"}
        else:
            return node, seen
        node = {**target, **extra}


def _merge_all(parts: list[dict], defs: dict, seen: frozenset[str]) -> dict:
    merged: dict = {"type": "object", "properties": {}, "required": []}
    for part in parts:
        part, _ = _resolve(part, defs, seen)
        merged["properties"].update(part.get("properties", {}))
        merged["required"] += part.get("required", [])
    return merged


def _describe(node: dict) -> str:
    bits = [node["description"].strip()] if node.get("description") else []
    if "const" in node:
        bits.append(f"Always {node['const']!r}.")
    elif node.get("enum"):
        bits.append("One of: " + ", ".join(str(v) for v in node["enum"] if v is not None) + ".")
    lo, hi = node.get("minimum", node.get("exclusiveMinimum")), node.get("maximum", node.get("exclusiveMaximum"))
    if lo is not None and hi is not None:
        bits.append(f"From {lo} to {hi}.")
    elif lo is not None:
        bits.append(f"At least {lo}.")
    elif hi is not None:
        bits.append(f"At most {hi}.")
    if node.get("minItems") is not None and node.get("minItems") == node.get("maxItems"):
        bits.append(f"Exactly {node['minItems']} items.")
    default = node.get("default")
    if isinstance(default, (str, int, float, bool)) and "const" not in node:
        bits.append(f"Default: {default}.")
    return " ".join(bits)


def _convert(node: dict, defs: dict, seen: frozenset[str] = frozenset()) -> dict:
    node, seen = _resolve(node, defs, seen)
    typ = node.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if "const" in node and typ is None:
        typ = _json_type(node["const"])
    if typ is None:
        if "properties" in node:
            typ = "object"
        elif "items" in node or "prefixItems" in node:
            typ = "array"
        elif node.get("enum"):
            typ = _json_type(next((v for v in node["enum"] if v is not None), ""))
        else:
            typ = "object"  # untyped "any" (e.g. EvidenceItem.value): the common case is a small object
    if typ not in VALID_TYPES:  # "null" alone
        typ = "string"

    out: dict[str, Any] = {"type": typ}
    description = _describe(node)
    if typ == "object":
        props = {name: _convert(sub, defs, seen) for name, sub in (node.get("properties") or {}).items()}
        out["properties"] = props
        required = [r for r in node.get("required", []) if r in props]
        if required:
            out["required"] = required
    elif typ == "array":
        items = node.get("items")
        if items is None and node.get("prefixItems"):
            items = node["prefixItems"][0]
        out["items"] = _convert(items if isinstance(items, dict) else {"type": "string"}, defs, seen)
    if description:
        out["description"] = description
    return out


def gateway_schema(model_schema: dict) -> dict:
    """Flatten one Pydantic `model_json_schema()` into a Gateway SchemaDefinition."""
    schema = _convert(model_schema, model_schema.get("$defs", {}))
    schema["type"] = "object"
    schema.setdefault("properties", {})
    schema.pop("description", None)  # the tool carries its own description
    return schema


def tool_definitions(specs: list[ToolSpec] | None = None) -> list[dict]:
    """The Gateway target's inline payload: [{name, description, inputSchema}] for every exposed registry entry."""
    return [
        {"name": s.name, "description": s.description, "inputSchema": gateway_schema(s.input_model.model_json_schema())}
        for s in (exposed_specs() if specs is None else specs)
    ]
