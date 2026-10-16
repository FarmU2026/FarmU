"""Writes web/agents.json: the agents, their tools and the real data behind each tool, for the "How FieldWatch
thinks" page (web/agents.html).

Run from agent/:   python -m web_export.agents_page

Everything factual comes from the code: the agent rosters and tool lists come from fieldwatch.agents, the tool
descriptions from the Strands tool specs, and each data source names the file that calls it (`code`) plus a string
that file must contain (`check`). tests/test_agents_page.py fails if the committed JSON drifts from the code or if a
check string disappears. Nothing here calls AWS or the network.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from fieldwatch import agents as A
from fieldwatch import config
from fieldwatch import tools as T
from fieldwatch.agents import EvidenceLedger
from fieldwatch.hooks import ToolTimeline

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "web" / "agents.json"

MODEL_NAMES = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": "Claude Haiku 4.5",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "Claude Sonnet 4.5",
}
GATE = "Needs the farmer's YES (recorded by code, not the AI)"

# specialist name -> (title the app's activity feed uses, role)
SPECIALISTS = {
    "imagery": ("Satellite specialist", "Satellite / imagery"),
    "environment": ("Weather and soil specialist", "Weather & soil / environment"),
    "agronomy": ("Crop health specialist", "Crop health / agronomy"),
}

# Plain-words label per tool (the card line). The full description from the code is kept beside it.
LABELS = {
    "list_fields": "Your fields",
    "look_at_field": "Latest satellite photos",
    "get_scene_analysis": "Satellite check results",
    "analyze_field": "Problem areas from the satellite check",
    "get_index_history": "Crop health over the season",
    "compare_to_cohort": "Crop health vs. same-crop fields",
    "test_overlap": "Problem area vs. low spots, sand, edges",
    "get_weather": "Recent rain and heat",
    "get_hail": "Hail size and hail reports",
    "get_drought": "Drought level for the county",
    "get_soil": "Soil type and drainage",
    "get_soil_profile": "Soil map areas and texture",
    "get_field_context": "Field history, terrain, water, prices",
    "get_disease_reports": "County disease and pest reports",
    "ask_imagery": "Ask the satellite specialist",
    "ask_environment": "Ask the weather and soil specialist",
    "ask_agronomy": "Ask the crop health specialist",
    "precheck_evidence": "Check what we already know",
    "score_hypotheses": "Turn evidence into percents",
    "estimate_impact": "Dollar range at stake",
    "create_work_order": "Repair ticket with a map pin",
    "create_scout_pin": "Scouting pin for the field",
    "send_farmer_message": "Text the farmer (no YES needed)",
}

# Where each tool's data comes from (keys of SOURCES). Verified against the code by the test.
TOOL_SOURCES = {
    "list_fields": ["farm"],
    "look_at_field": ["sentinel2"],
    "get_scene_analysis": ["scene_store"],
    "analyze_field": ["sentinel2"],
    "get_index_history": ["sentinel2"],
    "compare_to_cohort": ["sentinel2", "farm"],
    "test_overlap": ["farm", "usgs_3dep", "nrcs"],
    "get_weather": ["open_meteo"],
    "get_hail": ["mrms", "spc"],
    "get_drought": ["usdm"],
    "get_soil": ["nrcs"],
    "get_soil_profile": ["nrcs"],
    "get_field_context": ["nrcs", "usgs_3dep", "csb", "fema", "nhd", "nwi", "nass"],
    "get_disease_reports": ["eddmaps"],
    "precheck_evidence": ["scene_store", "conditions"],
    "score_hypotheses": ["conditions"],
    "estimate_impact": ["loss_tables"],
    "create_work_order": ["notify"],
    "create_scout_pin": ["notify"],
    "send_farmer_message": ["notify"],
    "ask_imagery": [],
    "ask_environment": [],
    "ask_agronomy": [],
}

# Every data source. short = the badge text; name = the row title; gives/updates = plain words from the code.
# code = the file that calls it, check = a string that file must contain (the test asserts both).
# recall = True for AgentCore Memory, which feeds the supervisor's prompt rather than being a tool.
SOURCES = [
    {"id": "farm", "short": "FieldWatch farm records", "name": "FieldWatch farm records",
     "gives": "Fields, crops, acres and the people on the farm", "updates": None,
     "code": "agent/fieldwatch/farmdata.py", "check": "fw-fields"},
    {"id": "scene_store", "short": "FieldWatch satellite check", "name": "FieldWatch satellite check (stored)",
     "gives": "Latest pre-computed weak-spot analysis per field, from the scene pipeline", "updates": None,
     "code": "agent/fieldwatch/farmdata.py", "check": "fw-scene-analyses"},
    {"id": "sentinel2", "short": "Sentinel-2 satellite", "name": "Sentinel-2 satellite (Element 84 Earth Search)",
     "gives": "Cloud-free images and crop-health index (NDVI, NDRE, NDMI) at 10 m pixels",
     "updates": "Revisits every 2-5 days", "code": "agent/fieldwatch/analysis/loader.py",
     "check": "earth-search.aws.element84.com"},
    {"id": "open_meteo", "short": "Open-Meteo", "name": "Open-Meteo weather archive",
     "gives": "Daily rain and max temperature for the last 30 days at the field",
     "updates": "Archive lags about 2 days", "code": "agent/fieldwatch/openmeteo.py",
     "check": "archive-api.open-meteo.com"},
    {"id": "mrms", "short": "NOAA MRMS radar hail", "name": "NOAA MRMS radar hail",
     "gives": "Largest hail size (MESH, mm) over the field, per day, on a 0.01-degree grid",
     "updates": "One 24-hour product per day", "code": "agent/fieldwatch/hazards/__init__.py",
     "check": "noaa-mrms-pds"},
    {"id": "spc", "short": "NOAA SPC reports", "name": "NOAA SPC storm reports",
     "gives": "Hail reports within 10 km of the field", "updates": "One report file per day",
     "code": "agent/fieldwatch/hazards/__init__.py", "check": "spc.noaa.gov/climo/reports"},
    {"id": "usdm", "short": "U.S. Drought Monitor", "name": "U.S. Drought Monitor",
     "gives": "Drought category for the county (the worst one covering at least half of it)", "updates": None,
     "code": "agent/fieldwatch/hazards/__init__.py", "check": "usdmdataservices.unl.edu"},
    {"id": "nrcs", "short": "NRCS soil survey", "name": "USDA NRCS Soil Data Access (SSURGO)",
     "gives": "Soil map areas, texture, available water, sandy share and drainage", "updates": None,
     "code": "agent/fieldwatch/context/soil.py", "check": "sdmdataaccess.sc.egov.usda.gov"},
    {"id": "usgs_3dep", "short": "USGS 3DEP elevation", "name": "USGS 3DEP elevation",
     "gives": "1 m elevation: low spots and slope inside the field", "updates": None,
     "code": "agent/fieldwatch/context/terrain.py", "check": "tnmaccess.nationalmap.gov"},
    {"id": "fema", "short": "FEMA flood zones", "name": "FEMA flood maps (NFHL)",
     "gives": "Mapped flood zones that touch the field", "updates": None,
     "code": "agent/fieldwatch/context/water.py", "check": "hazards.fema.gov"},
    {"id": "nhd", "short": "USGS NHD streams", "name": "USGS National Hydrography Dataset",
     "gives": "Mapped streams near the field", "updates": None,
     "code": "agent/fieldwatch/context/water.py", "check": "hydro.nationalmap.gov"},
    {"id": "nwi", "short": "USFWS wetlands", "name": "USFWS National Wetlands Inventory",
     "gives": "Mapped wetlands on the field", "updates": None,
     "code": "agent/fieldwatch/context/water.py", "check": "fwspublicservices.wim.usgs.gov"},
    {"id": "csb", "short": "USDA crop history", "name": "USDA Crop Sequence Boundaries 2018-2025",
     "gives": "Crop history for the drawn field", "updates": None,
     "code": "agent/fieldwatch/context/boundaries.py", "check": "NationalCSB_2018-2025"},
    {"id": "nass", "short": "USDA NASS yields", "name": "USDA NASS Quick Stats",
     "gives": "County median yield and national price; FieldWatch planning defaults when missing",
     "updates": "Annual yields", "code": "agent/fieldwatch/context/economics.py",
     "check": "quickstats.nass.usda.gov"},
    {"id": "eddmaps", "short": "EDDMapS reports", "name": "EDDMapS disease and pest reports",
     "gives": "County disease and pest confirmations for the season", "updates": None,
     "code": "agent/fieldwatch/context/disease.py", "check": "maps.eddmaps.org"},
    {"id": "loss_tables", "short": "FieldWatch loss tables", "name": "FieldWatch loss tables",
     "gives": "Yield loss range by condition, crop, stage and severity, turned into dollars", "updates": None,
     "code": "agent/fieldwatch/scoring.py", "check": "loss_tables.json"},
    {"id": "conditions", "short": "FieldWatch condition catalog", "name": "FieldWatch condition catalog",
     "gives": "Prior odds and evidence weights per condition; the scorer uses these, never the AI",
     "updates": None, "code": "agent/fieldwatch/scoring.py", "check": "conditions.json"},
    {"id": "notify", "short": "Text message", "name": "Text messages (notify lane)",
     "gives": "Texts to people on the farm; quiet hours and the daily cap are enforced there", "updates": None,
     "code": "agent/fieldwatch/registry.py", "check": "quiet hours"},
    {"id": "memory", "short": "AgentCore Memory", "name": "AgentCore Memory (past investigations)",
     "gives": "What past investigations on this farm found; recalled into the supervisor's prompt, not a tool",
     "updates": None, "code": "agent/fieldwatch/memory.py", "check": "AgentCore Memory", "recall": True},
]

# AWS services the system runs on, grouped the way the footer reads. evidence = (file, string) pairs that prove
# each service is in the deployed design (infra/stacks or the AgentCore project).
SERVICES = [
    {"names": ["AgentCore Runtime"], "role": "agents", "evidence": [
        ("agent/cloud/agentcore/agentcore/agentcore.json", '"runtimes"')]},
    {"names": ["AgentCore Gateway"], "role": "tools, MCP", "evidence": [
        ("infra/stacks/tools.py", "CfnGateway")]},
    {"names": ["AgentCore Memory"], "role": None, "evidence": [
        ("agent/cloud/agentcore/agentcore/agentcore.json", '"memories"')]},
    {"names": ["Bedrock"], "role": "Claude", "evidence": [
        ("infra/stacks/agent.py", "bedrock")]},
    {"names": ["EventBridge", "Step Functions", "Lambda"], "role": "pipeline", "evidence": [
        ("infra/stacks/data_plane.py", "aws_events"), ("infra/stacks/scenes.py", "aws_stepfunctions"),
        ("infra/stacks/common.py", "aws_lambda")]},
    {"names": ["DynamoDB", "S3"], "role": None, "evidence": [
        ("infra/stacks/data_plane.py", "aws_dynamodb"), ("infra/stacks/data_plane.py", "aws_s3")]},
    {"names": ["AppSync"], "role": "live feed", "evidence": [
        ("infra/stacks/feed.py", "aws_appsync")]},
]


def model_name(model_id: str) -> str:
    return MODEL_NAMES.get(model_id, model_id)


def first_paragraph(text: str) -> str:
    return " ".join(re.split(r"\n\s*\n", text.strip())[0].split())


def tool_objects() -> dict:
    """Every Strands tool the agents can call, by name, as the agents build them."""
    ledger = EvidenceLedger()
    timeline = ToolTimeline()
    found = dict(A.local_tool_pool())
    found.update({t.tool_name: t for t in A.build_specialists(timeline, ledger)})
    found.update({t.tool_name: t for t in A.build_scoring_tools(ledger)})
    found.update({n: getattr(T, n) for n in A.SUPERVISOR_WRITE_TOOLS})
    found.update({t.tool_name: t for t in T.MESSAGE_TOOLS})
    return found


def tool_entry(name: str, objects: dict, gate: str | None = None) -> dict:
    entry = {
        "name": name,
        "label": LABELS[name],
        "description": first_paragraph(objects[name].tool_spec["description"]),
        "sources": TOOL_SOURCES[name],
    }
    if gate:
        entry["gate"] = gate
    return entry


def build() -> dict:
    objects = tool_objects()
    sup_model = A.SUPERVISOR_MODEL_ID or config.MODEL_ID
    specialists = []
    for name, tool_names in A.SPECIALIST_TOOLS.items():
        title, role = SPECIALISTS[name]
        specialists.append({
            "name": name, "title": title, "role": role, "model": model_name(config.MODEL_ID),
            "tools": [tool_entry(n, objects) for n in tool_names],
        })
    supervisor_tools = [tool_entry(n, objects) for n in ("ask_imagery", "ask_environment", "ask_agronomy",
                                                          *A.SUPERVISOR_READ_TOOLS,
                                                          *(t.tool_name for t in T.MESSAGE_TOOLS))]
    scorer_tools = [tool_entry(n, objects) for n in ("precheck_evidence", "score_hypotheses", "estimate_impact")]
    approval_tools = [tool_entry(n, objects, gate=GATE) for n in A.SUPERVISOR_WRITE_TOOLS]

    used: dict[str, list[str]] = {s["id"]: [] for s in SOURCES}
    for name, sources in TOOL_SOURCES.items():
        for sid in sources:
            used[sid].append(LABELS[name])
    sources = []
    for s in SOURCES:
        row = {k: v for k, v in s.items() if k not in ("code", "check")}
        row["used_by"] = used[s["id"]]
        row["code"] = s["code"]
        sources.append(row)

    return {
        "models": {"supervisor": model_name(sup_model), "specialists": model_name(config.MODEL_ID)},
        "supervisor": {"title": "Supervisor agent", "model": model_name(sup_model), "tools": supervisor_tools},
        "specialists": specialists,
        "scorer": {"title": "Scorer (rules, not AI)", "tools": scorer_tools},
        "approval": {"title": "Approval (your YES)", "tools": approval_tools},
        "sources": sources,
        "services": [{"name": " + ".join(s["names"]), "role": s["role"]} for s in SERVICES],
        "generated_from": ["agent/fieldwatch/agents.py", "agent/fieldwatch/registry.py", "agent/fieldwatch/tools.py",
                           "agent/fieldwatch/config.py"],
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> None:
    if "--stdout" in (argv if argv is not None else sys.argv[1:]):
        print(render(), end="")
        return
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
