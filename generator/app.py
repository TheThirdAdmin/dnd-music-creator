"""The real generator's HTTP job API: fakegen's contract plus quality/gpu/qualities/status."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .service import GenService, NotReady, QualityUnavailable

MAX_DURATION_S = 380


class JobIn(BaseModel):
    tags: str = Field(min_length=1, max_length=4000)
    lyrics: str = ""
    duration_s: int = Field(ge=10, le=MAX_DURATION_S)
    seed: int | None = Field(None, ge=0, lt=2**31)
    quality: int = Field(2, ge=1, le=3)


def create_app(service: GenService, supervisor=None, *, housekeeping_s: float = 5) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(service.worker()), asyncio.create_task(service.housekeeping(housekeeping_s))]
        if supervisor is not None:
            tasks.append(asyncio.create_task(supervisor.run()))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if supervisor is not None:
                supervisor.stop()

    app = FastAPI(title="dnd-music generator", lifespan=lifespan)

    @app.post("/jobs", status_code=202)
    async def submit(body: JobIn):
        try:
            job = service.submit(body.tags, body.duration_s, body.seed, body.quality)
        except QualityUnavailable as e:
            raise HTTPException(422, str(e))
        except NotReady as e:
            raise HTTPException(503, f"not ready: {e}")
        return {"id": job.id}

    @app.get("/jobs/{job_id}")
    async def status(job_id: str):
        job = service.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return job.public()

    @app.get("/jobs/{job_id}/audio")
    async def audio(job_id: str):
        path = service.audio_path(job_id)
        if path is None:
            raise HTTPException(404, "not ready")
        return FileResponse(path, media_type="audio/flac")

    @app.get("/health")
    async def health():
        return service.health()

    return app
