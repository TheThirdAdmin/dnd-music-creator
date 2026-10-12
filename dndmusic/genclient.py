from dataclasses import dataclass

import httpx

from .models import DEFAULT_QUALITY, Prompt


class GeneratorError(Exception):
    """The generator could not be reached, answered with an HTTP error, or sent a malformed reply."""


@dataclass
class JobStatus:
    id: str
    status: str
    seed: int | None
    error: str | None
    duration_s: float | None = None


class GeneratorClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, timeout: float = 30.0):
        self.base_url = base_url
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            r = await self._client.request(method, path, **kwargs)
            r.raise_for_status()
            return r
        except httpx.HTTPError as e:
            raise GeneratorError(f"{self.base_url}{path}: {e}") from e

    async def _json(self, method: str, path: str, *keys: str, **kwargs) -> dict:
        r = await self._call(method, path, **kwargs)
        try:
            d = r.json()
        except ValueError as e:
            raise GeneratorError(f"{self.base_url}{path}: reply is not JSON") from e
        if not isinstance(d, dict):
            raise GeneratorError(f"{self.base_url}{path}: reply is not a JSON object")
        missing = [k for k in keys if k not in d]
        if missing:
            raise GeneratorError(f"{self.base_url}{path}: reply lacks {', '.join(missing)}")
        return d

    async def health(self) -> dict:
        return await self._json("GET", "/health", "ok")

    async def submit(self, prompt: Prompt, duration_s: int, seed: int | None = None,
                     quality: int = DEFAULT_QUALITY) -> str:
        body = {"tags": prompt.tags, "lyrics": prompt.lyrics, "duration_s": duration_s, "seed": seed,
                "quality": quality}
        return str((await self._json("POST", "/jobs", "id", json=body))["id"])

    async def status(self, job_id: str) -> JobStatus:
        d = await self._json("GET", f"/jobs/{job_id}", "id", "status")
        return JobStatus(str(d["id"]), str(d["status"]), d.get("seed"), d.get("error"), d.get("duration_s"))

    async def audio(self, job_id: str) -> bytes:
        return (await self._call("GET", f"/jobs/{job_id}/audio")).content

    async def aclose(self) -> None:
        await self._client.aclose()
