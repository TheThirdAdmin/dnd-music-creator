import httpx
import pytest

from dndmusic.library import Library
from dndmusic.runpod.balance import Balance, BalanceReader
from dndmusic.runpod.client import JobMeta, RunpodClient
from dndmusic.runpod.config import RunpodConfig
from dndmusic.runpod.cost import CostLedger
from dndmusic.runpod.guard import Guard
from dndmusic.runpod.service import RunpodService


def make(tmp_path, **kw):
    cfg = RunpodConfig(endpoint_id="ep", api_key="k", rate_per_hr=0.58, **kw)
    lib = Library(tmp_path)
    led = CostLedger(lib)
    client = RunpodClient(cfg)
    ref = []
    guard = Guard(cfg, lambda: ref[0].balance_usd, lambda: led.spent_today(__import__("time").time()))
    reader = BalanceReader(cfg, httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(401))))
    svc = RunpodService(cfg, client, guard, led, reader, lib)
    ref.append(svc)
    return svc


KEYS = {"configured", "endpoint_id", "state", "warming", "cold_start_s", "cost", "balance", "blocked", "activity",
        "rate_per_hr"}


def test_unknown(tmp_path):
    s = make(tmp_path).snapshot()
    assert set(s) == KEYS and s["state"] == "idle"
    assert s["balance"] == {"usd": None, "source": "unknown", "tracks_left": None, "low": False, "error": None}
    assert s["cost"]["last_track"] is None and s["cost"]["estimated"] is True


def test_runpod_balance_and_tracks(tmp_path):
    svc = make(tmp_path)
    svc.client.last_run["j1"] = JobMeta(30000, 60000)
    svc.on_job_done("j1")
    assert "j1" not in svc.client.last_run and svc.cold_start_s() == 60.0
    svc.last = Balance(1.0, 0.0)
    s = svc.snapshot()
    avg = s["cost"]["avg_track"]
    assert s["balance"]["source"] == "runpod" and s["balance"]["tracks_left"] == int(1.0 / avg)
    assert s["balance"]["low"] is True   # warn_below 5
    assert s["cost"]["today"] == pytest.approx(avg)


def test_budget_only(tmp_path):
    svc = make(tmp_path, budget_usd=10.0)
    svc.lib.set_state("runpod_budget_since", 0.0)
    svc.ledger.add("a", 1.5)
    s = svc.snapshot()
    assert s["balance"]["source"] == "budget" and s["balance"]["usd"] == pytest.approx(8.5)


def test_guard_blocks_on_real_balance(tmp_path):
    svc = make(tmp_path)
    svc.last = Balance(0.5, 0.0)
    assert "floor" in svc.guard.reason()
    s = svc.snapshot()
    assert s["state"] == "blocked" and s["blocked"]


def test_cold_starts_persist_and_cap(tmp_path):
    svc = make(tmp_path)
    for i in range(25):
        svc.client.last_run[str(i)] = JobMeta(1000, 6000 + i)
        svc.on_job_done(str(i))
    assert len(svc.cold_starts) == 20
    assert make(tmp_path).cold_starts == svc.cold_starts
    svc.on_job_done(None)   # no-op
    svc.on_job_done("missing")


async def test_balance_loop_reads(tmp_path):
    import asyncio
    svc = make(tmp_path)
    t = asyncio.create_task(svc.balance_loop(0.01))
    await asyncio.sleep(0.1)
    t.cancel()
    assert svc.last.error == "key cannot read the balance"


def test_scheduler_hook_books_cost(tmp_path, monkeypatch):
    from dndmusic.app import Settings, build_services
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "ep9")
    monkeypatch.setenv("RUNPOD_RATE", "0.58")
    svc = build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=object())
    svc.runpod.client.last_run["j"] = JobMeta(10000, 100)
    svc.scheduler.on_job_result("runpod:ep9", False, "j")
    assert svc.runpod.ledger.last() is not None and svc.runpod_guard.fails == 1


def test_last_run_none_then_recorded_and_persisted(tmp_path):
    svc = make(tmp_path)
    assert svc.snapshot()["cost"]["last_run"] is None
    svc.client.last_run["j1"] = JobMeta(55000, 290000)
    svc.on_job_done("j1")
    assert svc.snapshot()["cost"]["last_run"] == {"exec_s": 55.0, "delay_s": 290.0}
    assert make(tmp_path).snapshot()["cost"]["last_run"] == {"exec_s": 55.0, "delay_s": 290.0}   # new instance, same library


# ---- activity line

def _health(**kw):
    w = {"idle": 0, "initializing": 0, "ready": 0, "running": 0, "throttled": 0}
    w.update(kw.pop("workers", {}))
    return {"ok": True, "workers": w, "jobs": {"inQueue": kw.pop("in_queue", 1), "inProgress": 0}, **kw}


def _job(svc, state="queued", age=10.0, jid="j1"):
    import time
    now = time.monotonic()
    svc.client.inflight[jid] = {"t0": now - age, "state": state, "t_state": now - age}


def _act(svc, health):
    svc.health_fn = lambda: health
    return svc.snapshot()["activity"]


def test_activity_none_when_idle(tmp_path):
    assert make(tmp_path).snapshot()["activity"] is None


def test_activity_phases_from_health(tmp_path):
    import json
    from pathlib import Path
    fx = Path(__file__).parent / "fixtures/runpod"
    cold = json.loads((fx / "health_cold.json").read_text())
    svc = make(tmp_path)
    _job(svc, age=130)
    a = _act(svc, _health(workers={"initializing": 2}))
    assert a["phase"] == "starting" and a["since_s"] == 130
    assert a["detail"].startswith("Waiting for a worker to start (2 starting) · 2m10s. A start after idle takes 1-3 min;")
    assert "5-10 min (image pull, model download)." in a["detail"]
    # the recorded cold-start health (1 queued, 1 initializing) maps to the same phase
    assert cold["workers"]["initializing"] == 1
    a = _act(svc, _health(workers={"throttled": 3}))
    assert a["phase"] == "throttled"
    assert a["detail"] == "No GPU available right now (3 throttled) · 2m10s. Runpod is waiting for capacity."
    _job(svc, age=12)
    a = _act(svc, _health(workers={"idle": 1}))
    assert a["phase"] == "worker_ready" and a["detail"].startswith("Worker ready, starting the job · 0m")


def test_activity_rendering_and_long_first_run(tmp_path):
    svc = make(tmp_path)
    _job(svc, "running", age=41)
    a = _act(svc, _health(workers={"running": 1}, in_queue=0))
    assert a["phase"] == "rendering"
    assert a["detail"] == "Rendering · 0m41s (a track normally takes 20-80 s after the worker is up)"
    _job(svc, "running", age=400)
    assert _act(svc, _health(workers={"running": 1}, in_queue=0))["detail"] == \
        "Rendering · 6m40s · longer than usual: first run downloads the models"
    svc.ledger.add("old", 0.06)
    assert _act(svc, _health(workers={"running": 1}, in_queue=0))["detail"] == "Rendering · 6m40s · longer than usual"


def test_activity_more_queued_and_unreachable(tmp_path):
    svc = make(tmp_path)
    _job(svc, age=5)
    assert _act(svc, _health(workers={"initializing": 1}, in_queue=3))["detail"].endswith(" · +2 more queued")
    a = _act(svc, {"ok": False, "error": "Runpod key rejected (check Settings)"})
    assert a["phase"] == "unreachable" and a["detail"] == "Can't read Runpod health right now"


def test_job_done_clears_inflight_and_client_tracks_status(tmp_path):
    svc = make(tmp_path)
    _job(svc)
    svc.client._track("j1", "IN_PROGRESS")
    assert svc.client.inflight["j1"]["state"] == "running"
    svc.on_job_done("j1")
    assert svc.client.inflight == {}
    _job(svc, jid="j2")
    svc.client._track("j2", "COMPLETED")
    assert svc.client.inflight == {} and svc.client.active()   # lingers briefly for fast polling
