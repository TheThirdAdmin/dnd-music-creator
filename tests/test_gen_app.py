import asyncio
import io
import os
import signal
import sys
import time
from pathlib import Path

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from generator.app import create_app
from generator.comfy import ComfyClient, ComfyProcess, ComfyRenderer
from generator.config import GenSettings
from generator.modelfiles import ModelError
from generator.service import GenService
from generator.supervisor import Supervisor
from genfakes import FakeRenderer
from helpers import free_port

FAKECOMFY = str(Path(__file__).with_name("fakecomfy.py"))


def wait_job(c, job_id, timeout_s=10):
    for _ in range(int(timeout_s / 0.02)):
        st = c.get(f"/jobs/{job_id}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.02)
    raise AssertionError(st)


@pytest.fixture
def api(tmp_path):
    svc = GenService(FakeRenderer(), gpu=False, work_dir=tmp_path)
    svc.set_status("ok", ready=True)
    with TestClient(create_app(svc)) as c:
        c.svc = svc
        yield c


def test_job_lifecycle(api):
    r = api.post("/jobs", json={"tags": "Calm.", "lyrics": "", "duration_s": 30, "seed": 5, "quality": 1})
    assert r.status_code == 202
    job_id = r.json()["id"]
    assert wait_job(api, job_id) == {"id": job_id, "status": "done", "seed": 5, "duration_s": 30.0, "error": None}
    audio = api.get(f"/jobs/{job_id}/audio")
    assert audio.headers["content-type"] == "audio/flac"
    x, rate = sf.read(io.BytesIO(audio.content))
    assert abs(len(x) / rate - 30) < 0.01


def test_errors(api):
    assert api.get("/jobs/nope").status_code == 404
    assert api.get("/jobs/nope/audio").status_code == 404
    r = api.post("/jobs", json={"tags": "x", "duration_s": 60, "quality": 2})
    assert r.status_code == 422 and "GPU" in r.json()["detail"]
    assert api.post("/jobs", json={"tags": "x", "duration_s": 400, "quality": 1}).status_code == 422
    assert api.post("/jobs", json={"tags": "", "duration_s": 60, "quality": 1}).status_code == 422


def test_audio_is_404_until_done(tmp_path):
    svc = GenService(FakeRenderer(delay_s=0.5), gpu=True, work_dir=tmp_path)
    svc.set_status("ok", ready=True)
    with TestClient(create_app(svc)) as c:
        job_id = c.post("/jobs", json={"tags": "x", "duration_s": 30}).json()["id"]
        assert c.get(f"/jobs/{job_id}/audio").status_code == 404
        assert svc.jobs[job_id].quality == 2
        wait_job(c, job_id)
        assert c.get(f"/jobs/{job_id}/audio").status_code == 200


def test_not_ready_is_503_with_the_status(tmp_path):
    svc = GenService(FakeRenderer(), gpu=True, work_dir=tmp_path)
    svc.set_status("downloading 43%")
    with TestClient(create_app(svc)) as c:
        r = c.post("/jobs", json={"tags": "x", "duration_s": 60})
        assert r.status_code == 503 and r.json()["detail"] == "not ready: downloading 43%"
        assert c.get("/health").json() == {"ok": False, "model": "stable-audio-3-medium", "busy": False,
                                           "gpu": True, "qualities": [1, 2, 3], "status": "downloading 43%"}


class FakeStore:
    def __init__(self, fail_times=0):
        self.fail_times, self.calls = fail_times, 0

    async def ensure(self, report):
        self.calls += 1
        report("downloading 50%")
        if self.calls <= self.fail_times:
            raise ModelError("network down")


async def wait_for(cond, timeout_s=20):
    for _ in range(int(timeout_s / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_supervisor_brings_comfyui_up_and_restarts_it(tmp_path):
    port = free_port()
    svc = GenService(FakeRenderer(), gpu=True, work_dir=tmp_path)
    statuses = []
    original = svc.set_status

    def spy(status, *, ready=False):
        statuses.append(status)
        original(status, ready=ready)

    svc.set_status = spy
    proc = ComfyProcess([sys.executable, FAKECOMFY, "--port", str(port), "--output-directory", str(tmp_path / "out")])
    sup = Supervisor(svc, FakeStore(fail_times=1), proc, ComfyClient(f"http://127.0.0.1:{port}"),
                     check_s=0.1, retry_s=0.1)
    task = asyncio.create_task(sup.run())
    try:
        await wait_for(lambda: svc.ready)
        assert statuses[:4] == ["downloading 50%", "error: network down", "downloading 50%", "loading"]
        os.kill(proc.pid, signal.SIGKILL)
        await wait_for(lambda: svc.status == "restarting ComfyUI")
        assert svc.health()["ok"] is False
        await wait_for(lambda: svc.ready)
        assert proc.alive()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sup.stop()
    assert not proc.alive()


def test_whole_generator_against_a_fake_comfyui_process(tmp_path):
    port = free_port()
    s = GenSettings(device="gpu", model_dir=tmp_path / "models", work_dir=tmp_path / "work", comfy_port=port)
    out_dir = s.work_dir / "comfy" / "output"
    comfy = ComfyClient(f"http://127.0.0.1:{port}", poll_s=0.05)
    svc = GenService(ComfyRenderer(comfy, s.steps, out_dir), gpu=True, work_dir=s.work_dir)
    proc = ComfyProcess([sys.executable, FAKECOMFY, "--port", str(port), "--output-directory", str(out_dir)])
    with TestClient(create_app(svc, Supervisor(svc, FakeStore(), proc, comfy, check_s=0.1))) as c:
        for _ in range(400):
            if c.get("/health").json()["ok"]:
                break
            time.sleep(0.05)
        else:
            pytest.fail("generator did not become ready")
        job_id = c.post("/jobs", json={"tags": "Epic.", "duration_s": 12, "seed": 3, "quality": 3}).json()["id"]
        st = wait_job(c, job_id, timeout_s=30)
        assert st["status"] == "done" and st["duration_s"] == 12.0 and st["seed"] in (3, 4)
        x, rate = sf.read(io.BytesIO(c.get(f"/jobs/{job_id}/audio").content))
        assert abs(len(x) / rate - 12) < 0.01
        assert list(out_dir.rglob("*.flac")) == []
    assert not proc.alive()


async def test_supervisor_reports_and_retries_a_comfyui_that_cannot_be_launched(tmp_path):
    svc = GenService(FakeRenderer(), gpu=True, work_dir=tmp_path)
    statuses = []
    original = svc.set_status

    def spy(status, *, ready=False):
        statuses.append(status)
        original(status, ready=ready)

    svc.set_status = spy
    proc = ComfyProcess(["/nonexistent/python"])
    sup = Supervisor(svc, FakeStore(), proc, ComfyClient(f"http://127.0.0.1:{free_port()}"), check_s=0.05, retry_s=0.05)
    task = asyncio.create_task(sup.run())
    try:
        await wait_for(lambda: statuses.count("loading") >= 2)
        assert any(s.startswith("error: ") for s in statuses)
        assert not svc.ready and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        sup.stop()
