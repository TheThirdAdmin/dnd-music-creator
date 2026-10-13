import base64
import json
from pathlib import Path

import httpx
import pytest

from dndmusic.genclient import GeneratorError
from dndmusic.models import Prompt
from dndmusic.runpod.client import RunpodClient
from dndmusic.runpod.config import RunpodConfig

FIX = Path(__file__).parent / "fixtures" / "runpod"
KEY = "zq9-distinct-key"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


def make(handler):
    cfg = RunpodConfig("ep1", KEY, 0.5)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://rp.test/v2/ep1",
                             headers={"Authorization": f"Bearer {KEY}"})
    return RunpodClient(cfg, client=http)


async def test_submit_posts_run_with_policy_and_key():
    seen = []

    def h(r):
        seen.append(r)
        return httpx.Response(200, json=fixture("run_response"))
    c = make(h)
    assert c.base_url == "runpod:ep1"
    assert await c.submit(Prompt("tavern, calm", ""), 60, 42, 2) == "<job-id>-u1"
    [r] = seen
    assert r.method == "POST" and r.url.path == "/v2/ep1/run"
    assert r.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(r.content) == {
        "input": {"tags": "tavern, calm", "lyrics": "", "duration_s": 60, "seed": 42, "quality": 2},
        "policy": {"executionTimeout": 600000}}


@pytest.mark.parametrize("name,state", [("status_in_queue", "queued"), ("status_in_progress", "running")])
async def test_status_in_flight(name, state):
    c = make(lambda r: httpx.Response(200, json=fixture(name)))
    st = await c.status("j")
    assert st.status == state and st.error is None


async def test_completed_caches_audio_once():
    d = fixture("status_completed")
    raw = b"fLaC-bytes"
    d["output"]["audio_b64"] = base64.b64encode(raw).decode()
    c = make(lambda r: httpx.Response(200, json=d))
    st = await c.status("j")
    assert st.status == "done" and st.seed == 42 and st.duration_s == 60
    assert c.last_run["j"].exec_ms == 353912 and c.last_run["j"].delay_ms == 292267
    assert await c.audio("j") == raw
    with pytest.raises(GeneratorError):
        await c.audio("j")


@pytest.mark.parametrize("body,err", [
    ({"status": "FAILED", "error": "boom"}, "boom"),
    ({"status": "FAILED", "output": {"error": "oom"}}, "oom"),
    ({"status": "CANCELLED"}, "CANCELLED"),
    ({"status": "TIMED_OUT"}, "TIMED_OUT"),
    ({"status": "COMPLETED", "output": {"error": "HTTP 503"}}, "HTTP 503"),
])
async def test_failures_map_to_failed(body, err):
    c = make(lambda r: httpx.Response(200, json=body))
    st = await c.status("j")
    assert st.status == "failed" and err in st.error


@pytest.mark.parametrize("name,status,warming,busy", [
    ("health_cold", "warming up", True, True),
    ("health_after_jobs", "ready", False, False)])
async def test_health_from_fixtures(name, status, warming, busy):
    c = make(lambda r: httpx.Response(200, json=fixture(name)))
    h = await c.health()
    assert {k: v for k, v in h.items() if k not in ("workers", "jobs")} == {
        "ok": True, "busy": busy, "gpu": True, "qualities": [1, 2, 3], "status": status, "warming": warming}
    assert set(h["workers"]) == {"idle", "initializing", "ready", "running", "throttled"}
    assert set(h["jobs"]) == {"inQueue", "inProgress"}


async def test_health_idle():
    c = make(lambda r: httpx.Response(200, json={"jobs": {}, "workers": {"idle": 0, "initializing": 0}}))
    assert (await c.health())["status"] == "idle"


@pytest.mark.parametrize("code", [401, 403])
async def test_key_rejected(code):
    c = make(lambda r: httpx.Response(code, json={"error": "nope"}))
    with pytest.raises(GeneratorError, match="Runpod key rejected") as e:
        await c.health()
    assert KEY not in str(e.value)


async def test_http_error_and_transport_error_hide_key():
    c = make(lambda r: httpx.Response(500))
    with pytest.raises(GeneratorError, match="HTTP 500") as e:
        await c.status("j")
    assert KEY not in str(e.value)

    def boom(r):
        raise httpx.ConnectError(f"cannot reach with {KEY}")
    with pytest.raises(GeneratorError, match="unreachable") as e:
        await make(boom).health()
    assert KEY not in str(e.value) and KEY not in repr(e.value.__cause__)
