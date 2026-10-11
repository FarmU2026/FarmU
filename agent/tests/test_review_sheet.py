"""The weights review sheet regenerates and its dead/orphan key detection works."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "catalog" / "review_sheet.py"
_spec = importlib.util.spec_from_file_location("review_sheet", _PATH)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def test_sheet_builds_and_is_in_sync():
    text = rs.build()
    assert "## 2. Worked examples" in text and "## 3. Loss table sanity" in text
    assert rs.OUT.read_text(encoding="utf-8") == text, "REVIEW.md is stale: run python catalog/review_sheet.py"


def test_dead_and_orphan_detection():
    cat = {"hail": {"a", "dead_one"}, "ponding": {"b"}}
    wk = {"hail": ["a", "orphan_one"], "ponding": ["b"]}
    rules = [("f", 0.5, "ponding", "b", "why")]
    audit = rs.key_audit(cat, wk, rules, extra_code_keys={})
    assert audit["dead"] == [("hail", "dead_one")]
    assert audit["orphans"] == [("hail", "orphan_one")]
    assert ("hail", "a") in audit["llm_only"] and ("ponding", "b") not in audit["llm_only"]


def test_real_catalog_audit_has_no_orphans():
    wk, rules = rs._agents_literals()
    import json
    raw = json.loads(rs.CATALOG.read_text(encoding="utf-8"))
    audit = rs.key_audit({c: set(v["weights"]) for c, v in raw["conditions"].items()}, wk, rules)
    assert audit["orphans"] == []
