"""Every sample in fixtures/contracts/ must validate against its contract, and every contract needs a sample."""
import json
from pathlib import Path

import pytest

from fieldwatch import contracts as C
from fieldwatch import config, registry

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "contracts"


@pytest.mark.parametrize("name", sorted(C.FIXTURE_MODELS))
def test_fixture_validates(name):
    path = FIXTURES / f"{name}.json"
    assert path.exists(), f"missing sample {path.name}"
    C.FIXTURE_MODELS[name].model_validate(json.loads(path.read_text()))


def test_no_orphan_fixtures():
    orphans = {p.stem for p in FIXTURES.glob("*.json")} - set(C.FIXTURE_MODELS)
    assert not orphans, f"samples without a contract: {orphans}"


def test_switches_default_to_local():
    """main must run with every capability local (docs/decisions.md D1)."""
    assert config.SWITCHES == {k: "local" for k in config.SWITCHES}


def test_registry_entries_are_well_formed():
    names = [t.name for t in registry.TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names"
    for t in registry.TOOLS:
        assert t.access in ("read", "write")
        assert t.input_model is not None and t.description
        t.input_model.model_json_schema()  # must be expressible as a Gateway tool schema


@pytest.mark.parametrize("name", sorted(t.name for t in registry.TOOLS if t.stub))
def test_stubs_return_their_contract(name):
    spec = registry.BY_NAME[name]
    assert isinstance(spec.fn(), spec.output_model)
