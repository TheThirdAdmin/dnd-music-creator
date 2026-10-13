import json
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from dndmusic.app import build_services, create_app
from dndmusic.config import Settings
from fakes import FakeBackend

KEY = "rpa_SECRETKEY123456"


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("RUNPOD_ENDPOINT_ID", "RUNPOD_API_KEY", "RUNPOD_API_BASE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def env(tmp_path):
    services = build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=FakeBackend())
    with TestClient(create_app(lambda: services, start_background=False)) as c:
        yield c, services, tmp_path


def mock_runpod(svc):
    def h(req):
        if req.url.path.endswith("/health"):
            return httpx.Response(200, json={"workers": {"idle": 1}, "jobs": {}})
        return httpx.Response(200, json={"data": {"myself": {"clientBalance": 9.5, "currentSpendPerHr": 0}}})
    svc.runpod.client._client = httpx.AsyncClient(base_url="https://x/v2/ep", transport=httpx.MockTransport(h))
    svc.runpod.reader.client = httpx.AsyncClient(transport=httpx.MockTransport(h))


def test_unconfigured(env):
    c, svc, _ = env
    assert c.get("/api/runpod").json() == {"configured": False}
    assert c.get("/api/state").json()["runpod"] == {"configured": False}


def test_put_saves_without_leaking_key(env):
    c, svc, d = env
    r = c.put("/api/runpod", json={"endpoint_id": "abc123", "api_key": KEY, "budget_usd": 20})
    assert r.status_code == 200 and r.json()["key_set"] is True and r.json()["budget_usd"] == 20
    f = d / "runpod.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and KEY in f.read_text()
    assert svc.runpod is not None and svc.runpod.cfg.endpoint_id == "abc123"
    assert svc.lib.get_state("runpod_budget_since", 0) > 0
    for resp in (r, c.get("/api/runpod"), c.get("/api/state"), c.post("/api/runpod/reset"), c.get("/api/settings")):
        assert KEY not in resp.text
    assert c.get("/api/state").json()["runpod"]["configured"] is True
    assert "runpod:abc123" in c.get("/api/state").json()["generators"]


def test_empty_key_keeps_old_and_swap_closes_client(env):
    c, svc, d = env
    c.put("/api/runpod", json={"endpoint_id": "abc123", "api_key": KEY})
    old = svc.runpod
    r = c.put("/api/runpod", json={"endpoint_id": "def456", "api_key": "", "floor": 2, "warn_below": 6})
    assert r.status_code == 200
    assert json.loads((d / "runpod.json").read_text())["api_key"] == KEY
    assert svc.runpod is not old and svc.runpod.cfg.endpoint_id == "def456"
    assert old.client._client.is_closed
    assert list(c.get("/api/state").json()["generators"]) == ["runpod:def456"]


def test_null_clears_limit(env):
    c, svc, _ = env
    c.put("/api/runpod", json={"endpoint_id": "abc123", "api_key": KEY, "daily_cap": 3})
    assert c.get("/api/runpod").json()["daily_cap"] == 3
    c.put("/api/runpod", json={"daily_cap": None})
    assert c.get("/api/runpod").json()["daily_cap"] is None


@pytest.mark.parametrize("body", [
    {"floor": -1}, {"daily_cap": 0}, {"warn_below": 0.5, "floor": 1}, {"endpoint_id": "a b"}, {"budget_usd": -2},
])
def test_thresholds_validate(env, body):
    c, svc, _ = env
    r = c.put("/api/runpod", json={"endpoint_id": "abc123", "api_key": KEY, **body})
    assert r.status_code == 422
    assert svc.runpod is None


def test_needs_key_and_endpoint(env):
    c, _, _ = env
    assert c.put("/api/runpod", json={"floor": 1}).status_code == 422


def test_test_route_and_reset(env):
    c, svc, _ = env
    assert c.post("/api/runpod/test").json()["ok"] is False
    c.put("/api/runpod", json={"endpoint_id": "abc123", "api_key": KEY})
    mock_runpod(svc)
    r = c.post("/api/runpod/test")
    assert r.json() == {"ok": True, "state": "ready", "balance": 9.5, "error": None}
    assert KEY not in r.text
    for _ in range(3):
        svc.runpod_guard.record(False)
    assert svc.runpod_guard.reason() is not None
    assert c.post("/api/runpod/reset").status_code == 200
    assert svc.runpod_guard.reason() is None


def test_api_base_env(tmp_path, monkeypatch):
    from dndmusic.runpod.config import load_config
    (tmp_path / "runpod.json").write_text(json.dumps({"endpoint_id": "abc", "api_key": "k"}))
    assert load_config(tmp_path, {}).api_base == "https://api.runpod.ai/v2"
    assert load_config(tmp_path, {"RUNPOD_API_BASE": "http://127.0.0.1:9"}).api_base == "http://127.0.0.1:9"


def test_endpoint_id_without_key_is_offered_and_completed_from_settings(env):
    c, svc, tmp = env
    (tmp / "runpod.json").write_text(json.dumps({"endpoint_id": "ep12345", "api_key": "", "rate_per_hr": 0.58,
                                                 "created": {"endpoint_id": "ep12345"}}))
    assert c.get("/api/runpod").json() == {"configured": False, "endpoint_id": "ep12345"}
    r = c.put("/api/runpod", json={"api_key": KEY})
    assert r.status_code == 200 and r.json()["configured"] is True and r.json()["endpoint_id"] == "ep12345"
    doc = json.loads((tmp / "runpod.json").read_text())
    assert doc["created"] == {"endpoint_id": "ep12345"}   # setup's ids survive a Settings save


def test_status_row_target_follows_runpod_client(env):
    c, svc, _ = env
    s = c.post("/api/scenes", json={"name": "A", "location": "tavern", "mood": "calm"}).json()
    lib = svc.lib
    lib.add_track(s["pool_id"], lib.new_track_path(), 30.0, 1, "g")
    c.put("/api/queue", json={"ids": [s["id"]]})
    c.post("/api/queue/next", json={})
    b = c.post("/api/scenes", json={"name": "B", "location": "battlefield", "mood": "combat"}).json()
    c.put("/api/queue", json={"ids": [s["id"], b["id"]]})
    row = lambda role: next(r for r in c.get("/api/state").json()["status"] if r["role"] == role)
    assert row("playing")["target_s"] == 600 and row("next")["target_s"] == 360
    c.put("/api/runpod", json={"endpoint_id": "ep12345", "api_key": KEY})
    assert row("playing")["target_s"] == 1080 and row("next")["target_s"] == 600


def test_activity_in_state_and_no_key_anywhere(env):
    import time
    c, svc, _ = env
    c.put("/api/runpod", json={"endpoint_id": "ep12345", "api_key": KEY})
    assert c.get("/api/state").json()["runpod"]["activity"] is None
    now = time.monotonic()
    svc.runpod.client.inflight["j1"] = {"t0": now - 70, "state": "queued", "t_state": now - 70}
    svc.scheduler.health[svc.runpod.client.base_url].update(
        ok=True, workers={"initializing": 1, "idle": 0, "ready": 0, "running": 0, "throttled": 0},
        jobs={"inQueue": 1, "inProgress": 0})
    body = c.get("/api/state")
    act = body.json()["runpod"]["activity"]
    assert set(act) == {"phase", "since_s", "detail"} and act["phase"] == "starting"
    assert "(1 starting) · 1m1" in act["detail"]
    assert KEY not in body.text
