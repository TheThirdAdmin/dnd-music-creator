import base64
import httpx
import pytest
from generator import runpod_worker as rw


def mock_generator(statuses):
    calls = iter(statuses)
    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/jobs":
            return httpx.Response(202, json={"id": "j1"})
        if req.url.path == "/jobs/j1":
            return httpx.Response(200, json=next(calls))
        if req.url.path == "/jobs/j1/audio":
            return httpx.Response(200, content=b"fLaC-bytes")
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="http://gen")


async def test_render_returns_base64_audio(monkeypatch):
    monkeypatch.setattr(rw, "POLL_S", 0)
    c = mock_generator([{"id": "j1", "status": "running"},
                        {"id": "j1", "status": "done", "seed": 7, "duration_s": 60.5, "error": None}])
    out = await rw.render(c, {"tags": "epic", "lyrics": "", "duration_s": 60, "seed": 7, "quality": 2})
    assert base64.b64decode(out["audio_b64"]) == b"fLaC-bytes"
    assert out["mime"] == "audio/flac" and out["seed"] == 7 and out["duration_s"] == 60.5
    assert out["render_seconds"] >= 0


async def test_failed_job_raises(monkeypatch):
    monkeypatch.setattr(rw, "POLL_S", 0)
    c = mock_generator([{"id": "j1", "status": "failed", "error": "boom", "seed": 1, "duration_s": 0}])
    with pytest.raises(RuntimeError, match="boom"):
        await rw.render(c, {"tags": "x", "duration_s": 30, "quality": 1})


def test_fit_output_transcodes_only_when_too_big():
    small, mime = rw.fit_output(b"a" * 10, "audio/flac", 100, lambda b: b"x")
    assert (small, mime) == (b"a" * 10, "audio/flac")
    big, mime = rw.fit_output(b"a" * 1000, "audio/flac", 100, lambda b: b"ogg")
    assert (big, mime) == (b"ogg", "audio/ogg")


def test_fit_output_gives_up_if_still_too_big():
    with pytest.raises(RuntimeError, match="too large"):
        rw.fit_output(b"a" * 1000, "audio/flac", 100, lambda b: b"b" * 500)


def health_client(bodies):
    it = iter(bodies)
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=next(it))),
                             base_url="http://gen")


async def test_wait_ready_fails_fast_when_comfyui_is_dead():
    bad = {"ok": False, "status": "error: ComfyUI exited (code 1)"}
    with pytest.raises(RuntimeError, match="not starting: error: ComfyUI exited"):
        await rw.wait_ready(health_client([bad] * 10), timeout_s=60, poll_s=0)


async def test_wait_ready_survives_a_transient_error_and_downloads():
    bodies = [{"ok": False, "status": "downloading 40%"}, {"ok": False, "status": "error: x"},
              {"ok": False, "status": "restarting ComfyUI"}, {"ok": False, "status": "error: x"},
              {"ok": True, "status": "ready"}]
    await rw.wait_ready(health_client(bodies), timeout_s=60, poll_s=0)
