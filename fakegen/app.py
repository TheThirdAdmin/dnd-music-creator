"""Fake generator: same HTTP API as the real one, returns test tones. No GPU needed."""
import asyncio
import hashlib
import io
import os
import uuid
import wave
from contextlib import asynccontextmanager

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

RATE = 22050
FAKEGEN_DELAY_S = float(os.environ.get("FAKEGEN_DELAY_S", "1"))
FAKEGEN_MAX_DURATION = float(os.environ.get("FAKEGEN_MAX_DURATION", "600"))


class JobIn(BaseModel):
    tags: str
    lyrics: str = ""
    duration_s: int
    seed: int | None = None
    quality: int = Field(2, ge=1, le=3)


def pitch_for(tags: str) -> float:
    """A pitch per prompt, so tests and ears can tell pools apart."""
    h = int(hashlib.sha256(tags.encode()).hexdigest()[:8], 16)
    return 220.0 * 2 ** ((h % 24) / 12)


def render_wav(tags: str, duration_s: float, seed: int) -> bytes:
    n = int(RATE * duration_s)
    t = np.arange(n) / RATE
    rng = np.random.default_rng(seed)
    signal = 0.3 * np.sin(2 * np.pi * pitch_for(tags) * t) + 0.02 * rng.standard_normal(n)
    pcm = (np.clip(signal, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def create_app(delay_s: float = FAKEGEN_DELAY_S, max_duration_s: float = FAKEGEN_MAX_DURATION) -> FastAPI:
    jobs: dict[str, dict] = {}
    tasks: set[asyncio.Task] = set()
    running = {"n": 0}

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="fake generator", lifespan=lifespan)

    async def run(job: dict) -> None:
        job["status"] = "running"
        running["n"] += 1
        try:
            await asyncio.sleep(delay_s)
            duration = min(job["duration_s"], max_duration_s)
            job["audio"] = await asyncio.to_thread(render_wav, job["tags"], duration, job["seed"])
            job["status"] = "done"
        except Exception as e:  # report, don't crash the server
            job["status"], job["error"] = "failed", str(e)
        finally:
            running["n"] -= 1

    def public(job: dict) -> dict:
        return {k: job.get(k) for k in ("id", "status", "seed", "duration_s", "error")}

    @app.post("/jobs", status_code=202)
    async def submit(body: JobIn):
        job_id = uuid.uuid4().hex
        seed = body.seed if body.seed is not None else int.from_bytes(os.urandom(4), "big") >> 1
        jobs[job_id] = {"id": job_id, "status": "queued", "tags": body.tags, "duration_s": body.duration_s,
                        "seed": seed, "error": None, "quality": body.quality}
        task = asyncio.create_task(run(jobs[job_id]))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"id": job_id}

    @app.get("/jobs/{job_id}")
    async def status(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "no such job")
        return public(jobs[job_id])

    @app.get("/jobs/{job_id}/audio")
    async def audio(job_id: str):
        job = jobs.get(job_id)
        if job is None or job["status"] != "done":
            raise HTTPException(404, "not ready")
        return Response(job["audio"], media_type="audio/wav")

    @app.get("/health")
    async def health():
        return {"ok": True, "model": "fake", "busy": running["n"] > 0, "gpu": False, "qualities": [1, 2, 3],
                "status": "ok"}

    return app


app = create_app()
