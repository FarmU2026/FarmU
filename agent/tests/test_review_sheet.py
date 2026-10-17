"""The agronomy review sheet regenerates, is current, and its dead/orphan key detection works."""
import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "catalog" / "review_sheet.py"
_spec = importlib.util.spec_from_file_location("review_sheet", _PATH)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


def test_sheet_builds_and_is_current():
    text = rs.build()
    assert "## 3. Worked examples" in text and "## 5. Loss table sanity" in text and "## 1. Design values" in text
    assert rs.OUT.read_text(encoding="utf-8") == text, "REVIEW.md is stale: run `python catalog/review_sheet.py`"


def test_dead_and_orphan_detection():
    cat = {"hail": {"a", "dead_one"}, "ponding": {"b"}}
    audit = rs.key_audit(cat, {"a", "b", "orphan_one"}, {"hail": set(), "ponding": {"b"}})
    assert audit["dead"] == [("hail", "dead_one")]
    assert audit["orphans"] == ["orphan_one"]
    assert audit["photo_farmer"] == [("ponding", "b")]


def test_real_catalog_audit_has_no_dead_keys_or_orphans():
    from fieldwatch import evidence, scoring

    raw = json.loads(rs.CATALOG.read_text(encoding="utf-8"))
    audit = rs.key_audit({c: set(v["weights"]) for c, v in raw["conditions"].items()}, set(evidence.RESOLVERS),
                         scoring.llm_keys())
    assert audit["dead"] == [] and audit["orphans"] == []
