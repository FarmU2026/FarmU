"""SMS plumbing with no network: outbound goes through sms.send, inbound texts route to the farm."""
import json

import pytest
from fastapi.testclient import TestClient

import fieldwatch.sms as sms
import web.server as srv


@pytest.fixture
def farm(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "FARMS", tmp_path / "farms")
    monkeypatch.setattr(srv, "IMAGES", tmp_path / "images")
    sent = []
    monkeypatch.setattr(sms, "send", lambda to, text: sent.append((to, text)) or {"provider": "fake", "ok": True, "detail": "x"})
    monkeypatch.setattr(srv.imagery, "look", lambda *a, **k: None)  # no satellite in tests
    client = TestClient(srv.app)
    r = client.post("/api/farms", json={"phone": "(402) 555-0100", "farmer": "Aaron",
                                        "fields": [{"name": "north pivot", "polygon": [[42.1, -96.5], [42.1, -96.49], [42.11, -96.49]]}]})
    assert r.status_code == 200, r.text
    return client, sent, r.json()


def test_e164_and_farm_id():
    assert sms.e164("402 555 0100") == "+14025550100"
    assert sms.e164("+1 (402) 555-0100") == "+14025550100"
    assert sms.farm_id_for("+14025550100") == "4025550100"


def test_welcome_is_texted_and_stop_mutes(farm):
    client, sent, f = farm
    assert f["farm_id"] == "4025550100"
    assert sent and sent[0][0] == "(402) 555-0100" and "FieldWatch here" in sent[0][1]
    assert f["thread"][0]["sms"]["ok"]

    r = client.post("/sms/inbound", data={"From": "+14025550100", "Body": "STOP"})
    assert r.status_code == 200 and "<Response>" in r.text
    import time
    time.sleep(0.5)  # inbound is handled on a thread
    f = client.get("/api/farms/4025550100").json()
    assert f["muted"] is True
    n = len(sent)
    client.post("/api/farms/4025550100/reply", json={"text": "MORE"})
    assert len(sent) == n  # muted: nothing texted


def test_more_without_diagnosis(farm):
    client, sent, f = farm
    r = client.post("/api/farms/4025550100/reply", json={"text": "MORE"})
    assert "No satellite pass yet" in r.json()["thread"][-1]["text"]
    assert sent[-1][1].startswith("No satellite pass yet")
