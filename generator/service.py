"""The generator's job queue: FIFO, one job at a time, quality levels, idle VRAM release, retention."""
import asyncio
import logging
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .modelfiles import MEDIUM, SMALL
from .score import duration_of, score

log = logging.getLogger(__name__)
SMALL_MAX_S = 120


class QualityUnavailable(Exception):
    """This generator can't render that quality level (2 and 3 need a GPU)."""


class NotReady(Exception):
    """Still downloading models, starting or restarting ComfyUI."""


@dataclass(frozen=True)
class Level:
    ckpt: str
    renders: int
    max_s: int | None


LEVELS = {1: Level(SMALL.name, 1, SMALL_MAX_S), 2: Level(MEDIUM.name, 1, None), 3: Level(MEDIUM.name, 2, None)}


class Renderer(Protocol):
    async def render(self, ckpt: str, prompt: str, seconds: int, seed: int) -> bytes: ...

    async def free(self) -> None: ...


@dataclass
class Job:
    id: str
    prompt: str
    duration_s: int
    seed: int
    quality: int
    status: str = "queued"
    error: str | None = None
    rendered_s: float | None = None
    audio_path: Path | None = None
    finished_at: float | None = None
    fetched_at: float | None = None

    def public(self) -> dict:
        return {"id": self.id, "status": self.status, "seed": self.seed,
                "duration_s": self.rendered_s if self.rendered_s is not None else self.duration_s, "error": self.error}


class GenService:
    def __init__(self, renderer: Renderer, *, gpu: bool, work_dir: Path, idle_free_s: float = 60,
                 keep_s: float = 3600, fetched_keep_s: float = 600, clock: Callable[[], float] = time.monotonic,
                 scorer: Callable[[bytes], float] = score):
        self.renderer = renderer
        self.gpu = gpu
        self.jobs_dir = Path(work_dir) / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.idle_free_s = idle_free_s
        self.keep_s = keep_s
        self.fetched_keep_s = fetched_keep_s
        self.clock = clock
        self.scorer = scorer
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[Job] = asyncio.Queue()
        self.current: Job | None = None
        self.status = "starting"
        self._ready = asyncio.Event()
        self.last_active = clock()
        self.freed = True

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def set_status(self, status: str, *, ready: bool = False) -> None:
        self.status = status
        if ready:
            self._ready.set()
        else:
            self._ready.clear()

    @property
    def qualities(self) -> list[int]:
        return [1, 2, 3] if self.gpu else [1]

    def health(self) -> dict:
        return {"ok": self.ready, "model": "stable-audio-3-medium" if self.gpu else "stable-audio-3-small-music",
                "busy": self.current is not None or not self.queue.empty(), "gpu": self.gpu,
                "qualities": self.qualities, "status": self.status}

    def submit(self, prompt: str, duration_s: int, seed: int | None, quality: int) -> Job:
        if quality not in self.qualities:
            raise QualityUnavailable(f"quality {quality} needs a GPU generator; this one offers {self.qualities}")
        if not self.ready:
            raise NotReady(self.status)
        job = Job(uuid.uuid4().hex, prompt, duration_s, random.getrandbits(31) if seed is None else seed, quality)
        self.jobs[job.id] = job
        self.queue.put_nowait(job)
        return job

    def audio_path(self, job_id: str) -> Path | None:
        job = self.jobs.get(job_id)
        if job is None or job.status != "done" or job.audio_path is None:
            return None
        if job.fetched_at is None:
            job.fetched_at = self.clock()
        return job.audio_path

    async def worker(self) -> None:
        while True:
            job = await self.queue.get()
            self.current = job
            await self._ready.wait()
            job.status, self.freed = "running", False
            try:
                await self._run(job)
                job.status = "done"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                job.status, job.error = "failed", str(e) or type(e).__name__
                log.warning("job %s failed: %s", job.id, job.error)
            finally:
                job.finished_at = self.last_active = self.clock()
                self.current = None

    async def _run(self, job: Job) -> None:
        level = LEVELS[job.quality]
        seconds = min(job.duration_s, level.max_s) if level.max_s else job.duration_s
        best: bytes | None = None
        best_score: float | None = None
        best_seed = job.seed
        for i in range(level.renders):
            seed = (job.seed + i) % (1 << 31)
            audio = await self.renderer.render(level.ckpt, job.prompt, seconds, seed)
            if level.renders == 1:
                best, best_seed = audio, seed
                break
            s = await asyncio.to_thread(self.scorer, audio)
            log.info("job %s render %d (seed %d) scored %.3f", job.id, i + 1, seed, s)
            if best_score is None or s > best_score:
                best, best_score, best_seed = audio, s, seed
        path = self.jobs_dir / f"{job.id}.flac"
        path.write_bytes(best)
        job.audio_path, job.seed, job.rendered_s = path, best_seed, round(duration_of(best), 2)

    async def idle_tick(self) -> None:
        """After idle_free_s with nothing to do, ask ComfyUI once to unload models and free VRAM."""
        if self.freed or self.current is not None or not self.queue.empty():
            return
        if self.clock() - self.last_active < self.idle_free_s:
            return
        self.freed = True
        try:
            await self.renderer.free()
            log.info("idle for %g s: asked ComfyUI to free VRAM", self.idle_free_s)
        except Exception as e:
            log.warning("idle free failed: %s", e)

    def sweep(self) -> None:
        """Forget finished jobs after keep_s, or fetched_keep_s after their audio was first fetched."""
        now = self.clock()
        for job in list(self.jobs.values()):
            if job.finished_at is None:
                continue
            old = now - job.finished_at >= self.keep_s
            fetched = job.fetched_at is not None and now - job.fetched_at >= self.fetched_keep_s
            if old or fetched:
                if job.audio_path:
                    job.audio_path.unlink(missing_ok=True)
                del self.jobs[job.id]

    async def housekeeping(self, every_s: float = 5) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.idle_tick()
                self.sweep()
            except Exception:
                log.exception("housekeeping")
