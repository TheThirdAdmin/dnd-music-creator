import base64
import time
from dataclasses import dataclass

import httpx

from ..genclient import GeneratorError, JobStatus
from ..models import DEFAULT_QUALITY, Prompt
from .config import RunpodConfig

STATE = {"IN_QUEUE": "queued", "IN_PROGRESS": "running", "COMPLETED": "done"}


@dataclass
class JobMeta:
    exec_ms: int = 0
    delay_ms: int = 0


class RunpodClient:
    """Same surface as GeneratorClient, over the Runpod serverless REST API. Errors never carry the key."""

    def __init__(self, cfg: RunpodConfig, client: httpx.AsyncClient | None = None, exec_timeout_s: int = 600):
        self.cfg = cfg
        self.base_url = f"runpod:{cfg.endpoint_id}"
        self.exec_timeout_s = exec_timeout_s
        self.last_run: dict[str, JobMeta] = {}
        self.inflight: dict[str, dict] = {}   # job id -> {t0, state, t_state} (monotonic), for the activity line
        self.last_activity = 0.0
        self._audio: dict[str, bytes] = {}
        self._client = client or httpx.AsyncClient(
            base_url=f"{cfg.api_base}/{cfg.endpoint_id}", timeout=30.0,
            headers={"Authorization": f"Bearer {cfg.api_key}"})

    async def _json(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise GeneratorError(f"Runpod unreachable: {type(e).__name__}") from None
        if r.status_code in (401, 403):
            raise GeneratorError("Runpod key rejected (check Settings)")
        if r.status_code >= 400:
            raise GeneratorError(f"Runpod answered HTTP {r.status_code} for /{path.split('/')[1]}")
        try:
            d = r.json()
        except ValueError:
            raise GeneratorError("Runpod reply is not JSON") from None
        if not isinstance(d, dict):
            raise GeneratorError("Runpod reply is not a JSON object")
        return d

    async def health(self) -> dict:
        d = await self._json("GET", "/health")
        w, j = d.get("workers", {}), d.get("jobs", {})
        ready = int(w.get("idle", 0)) + int(w.get("running", 0)) + int(w.get("ready", 0))
        starting = int(w.get("initializing", 0))
        queued = int(j.get("inQueue", 0)) + int(j.get("inProgress", 0))
        warming = queued > 0 and ready == 0
        status = "warming up" if warming or starting else ("ready" if ready else "idle")
        counts = {k: int(w.get(k, 0)) for k in ("idle", "initializing", "ready", "running", "throttled")}
        return {"ok": True, "busy": queued > 0, "gpu": True, "qualities": [1, 2, 3], "status": status,
                "warming": warming, "workers": counts,
                "jobs": {"inQueue": int(j.get("inQueue", 0)), "inProgress": int(j.get("inProgress", 0))}}

    async def submit(self, prompt: Prompt, duration_s: int, seed: int | None = None,
                     quality: int = DEFAULT_QUALITY) -> str:
        body = {"input": {"tags": prompt.tags, "lyrics": prompt.lyrics, "duration_s": duration_s,
                          "seed": seed, "quality": quality},
                "policy": {"executionTimeout": self.exec_timeout_s * 1000}}
        d = await self._json("POST", "/run", json=body)
        if "id" not in d:
            raise GeneratorError("Runpod reply lacks id")
        now = time.monotonic()
        self.inflight[str(d["id"])] = {"t0": now, "state": "queued", "t_state": now}
        self.last_activity = now
        return str(d["id"])

    def active(self, linger_s: float = 15.0) -> bool:
        """A job is in flight or just finished: the health loop polls fast so the activity line is current."""
        return bool(self.inflight) or time.monotonic() - self.last_activity < linger_s

    def _track(self, job_id: str, raw: str) -> None:
        job = self.inflight.get(job_id)
        if job is None:
            return
        if raw in ("IN_QUEUE", "IN_PROGRESS"):
            state = "running" if raw == "IN_PROGRESS" else "queued"
            if job["state"] != state:
                job["state"], job["t_state"] = state, time.monotonic()
        else:   # terminal
            self.inflight.pop(job_id, None)
            self.last_activity = time.monotonic()

    def _meta(self, job_id: str, d: dict) -> None:
        self.last_run[job_id] = JobMeta(int(d.get("executionTime") or 0), int(d.get("delayTime") or 0))

    async def status(self, job_id: str) -> JobStatus:
        d = await self._json("GET", f"/status/{job_id}")
        raw = str(d.get("status", ""))
        self._track(job_id, raw)
        out = d.get("output") if isinstance(d.get("output"), dict) else {}
        if raw == "COMPLETED":
            self._meta(job_id, d)
            if "audio_b64" not in out:
                return JobStatus(job_id, "failed", None, str(out.get("error") or "no audio in job output"))
            try:
                self._audio[job_id] = base64.b64decode(out["audio_b64"])
            except ValueError:
                return JobStatus(job_id, "failed", None, "job audio is not valid base64")
            return JobStatus(job_id, "done", _num(out.get("seed"), int), None, _num(out.get("duration_s"), float))
        if raw in STATE:
            return JobStatus(job_id, STATE[raw], None, None)
        self._meta(job_id, d)
        return JobStatus(job_id, "failed", None, str(d.get("error") or out.get("error") or raw or "unknown"))

    async def audio(self, job_id: str) -> bytes:
        try:
            return self._audio.pop(job_id)
        except KeyError:
            raise GeneratorError(f"no audio held for job {job_id}") from None

    async def aclose(self) -> None:
        await self._client.aclose()


def _num(v, kind):
    try:
        return kind(v) if v is not None else None
    except (TypeError, ValueError):
        return None
