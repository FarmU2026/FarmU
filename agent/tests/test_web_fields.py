"""Field add / edit / delete on the local dev harness (web.server), no network."""
import pytest
from fastapi.testclient import TestClient

import web.server as srv

SQUARE = [[42.10, -96.60], [42.10, -96.59], [42.09, -96.59], [42.09, -96.60]]
TRI = [[42.1, -96.5], [42.1, -96.49], [42.11, -96.49]]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(srv.sms, "send", lambda to, text: {"provider": "fake", "ok": True, "detail": "x"})
    monkeypatch.setattr(srv.imagery, "look", lambda *a, **k: None)
    c = TestClient(srv.app)
    r = c.post("/api/farms", json={"phone": "(402) 555-0100", "farmer": "Aaron",
                                   "fields": [{"name": "North Pivot", "polygon": TRI}]})
    assert r.status_code == 200
    c.farm_id = r.json()["farm_id"]
    c.url = f"/api/farms/{c.farm_id}/fields"
    return c


def test_add_then_get_matches(client):
    r = client.post(client.url, json={"name": "South 40", "crop": "soybeans", "polygon": SQUARE})
    assert r.status_code == 200
    farm = r.json()
    assert [f["id"] for f in farm["fields"]] == ["north-pivot", "south-40"]
    assert farm["fields"][1]["crop"] == "soybeans" and farm["fields"][1]["polygon"][0] == [-96.60, 42.10]
    assert client.get(f"/api/farms/{client.farm_id}").json() == farm


def test_rename_crop_keep_id_and_redraw(client):
    r = client.patch(f"{client.url}/north-pivot", json={"name": "Home Quarter", "crop": "soy"})
    f = r.json()["fields"][0]
    assert r.status_code == 200 and f["id"] == "north-pivot" and f["name"] == "Home Quarter" and f["crop"] == "soybeans"
    old_polygon = f["polygon"]
    f = client.patch(f"{client.url}/north-pivot", json={"polygon": SQUARE}).json()["fields"][0]
    assert f["id"] == "north-pivot" and f["polygon"] != old_polygon and f["polygon"][0] == [-96.60, 42.10]


def test_redraw_pulls_a_fresh_look(client, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "pull_field", lambda farm, field, only_if_newer=True: calls.append((field["id"], only_if_newer)))
    client.patch(f"{client.url}/north-pivot", json={"polygon": SQUARE})
    client.patch(f"{client.url}/north-pivot", json={"name": "x"})
    assert calls == [("north-pivot", False)]


def test_delete(client):
    client.post(client.url, json={"name": "B", "polygon": SQUARE})
    r = client.delete(f"{client.url}/north-pivot")
    assert r.status_code == 200 and [f["id"] for f in r.json()["fields"]] == ["b"]
    assert client.delete(f"{client.url}/north-pivot").status_code == 404


@pytest.mark.parametrize("body", [
    {"name": "", "polygon": SQUARE}, {"name": "north pivot", "polygon": SQUARE}, {"name": "X", "polygon": SQUARE[:2]},
    {"name": "X", "crop": "kale", "polygon": SQUARE},
])
def test_add_invalid(client, body):
    r = client.post(client.url, json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_INPUT"


def test_patch_invalid_and_404s(client):
    assert client.patch(f"{client.url}/north-pivot", json={}).status_code == 400
    assert client.patch(f"{client.url}/north-pivot", json={"polygon": SQUARE[:2]}).status_code == 400
    r = client.patch(f"{client.url}/nope", json={"name": "x"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert client.post("/api/farms/0000000000/fields", json={"name": "x", "polygon": SQUARE}).status_code == 404
    assert client.delete("/api/farms/0000000000/fields/x").status_code == 404
