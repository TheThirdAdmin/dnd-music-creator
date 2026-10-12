"""Opt-in: needs a real generator with a GPU at GENERATOR_URL (default http://localhost:8001).
Run: GENERATOR_URL=http://localhost:8001 .venv/bin/pytest -m gpu -v"""
import io
import os
import time

import httpx
import numpy as np
import pytest
import soundfile as sf

pytestmark = pytest.mark.gpu
URL = os.environ.get("GENERATOR_URL", "http://localhost:8001").rstrip("/")


def test_renders_30_s_at_quality_2():
    deadline = time.monotonic() + 1800
    h = httpx.get(f"{URL}/health", timeout=10).json()
    assert h["gpu"] is True and 2 in h["qualities"]
    while not h["ok"]:
        assert time.monotonic() < deadline, f"generator not ready: {h['status']}"
        time.sleep(10)
        h = httpx.get(f"{URL}/health", timeout=10).json()
    r = httpx.post(f"{URL}/jobs", json={"tags": "Epic orchestral fantasy battle music, pounding drums and brass. "
                                                 "Instrumental.", "duration_s": 30, "seed": 42, "quality": 2}, timeout=30)
    assert r.status_code == 202
    job_id = r.json()["id"]
    while (st := httpx.get(f"{URL}/jobs/{job_id}", timeout=10).json())["status"] in ("queued", "running"):
        assert time.monotonic() < deadline, "render took too long"
        time.sleep(2)
    assert st["status"] == "done", st
    audio = httpx.get(f"{URL}/jobs/{job_id}/audio", timeout=60)
    assert audio.headers["content-type"] == "audio/flac"
    x, rate = sf.read(io.BytesIO(audio.content))
    assert len(x) / rate >= 30
    assert float(np.sqrt(np.mean(np.square(x)))) > 0.003
