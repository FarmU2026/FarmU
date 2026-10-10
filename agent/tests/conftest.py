"""Shared test isolation.

Nothing a test does may touch agent/data or leak module state into the next test (or the next run):

* approvals: the local JSON backend points at a per-test tmp dir, any injected store / event sink / cached
  store is dropped before and after each test.
* the web harness's farm and image folders (agent/data/farms, agent/data/images) point at tmp too.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_agent_data(tmp_path, monkeypatch):
    from fieldwatch import approvals, config

    data = tmp_path / "agent-data"
    data.mkdir()
    monkeypatch.setenv("FIELDWATCH_APPROVALS_BACKEND", "local")
    monkeypatch.setenv("FIELDWATCH_APPROVALS_DIR", str(data))
    monkeypatch.setattr(config, "DATA", data)
    try:
        import web.server as srv
    except ImportError:  # the harness needs fastapi; the rest of the suite does not
        srv = None
    if srv is not None:
        monkeypatch.setattr(srv, "FARMS", data / "farms")
        monkeypatch.setattr(srv, "IMAGES", data / "images")

    def reset():
        from fieldwatch import interfaces

        approvals.use_store(None)
        approvals.set_event_sink(None)
        approvals._store_cache.clear()
        interfaces.use_notify(None)

    reset()
    yield data
    reset()
