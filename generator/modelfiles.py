"""Stable Audio 3 model files: downloaded on first start into MODEL_DIR, resumable, SHA-256 checked.
Verified files are remembered in MODEL_DIR/.verified.json (size, mtime, hash) so later starts skip hashing."""
import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

REPO = "Comfy-Org/stable-audio-3/resolve/main"
CHUNK = 1 << 20


class ModelError(Exception):
    """A model file could not be downloaded or failed its checksum."""


@dataclass(frozen=True)
class ModelFile:
    subdir: str
    name: str
    size: int
    sha256: str

    @property
    def rel(self) -> str:
        return f"{self.subdir}/{self.name}"


SMALL = ModelFile("checkpoints", "stable_audio_3_small_music.safetensors", 2270384940,
                  "da85866b11b01d0694d990785f6abbd79c8064df1b0e6f8aea52935e0ef84b64")
MEDIUM = ModelFile("checkpoints", "stable_audio_3_medium.safetensors", 9222116660,
                   "48d9c65e290e7bcd5194e0633bfc2424a59ee9683f5c2d58762d997b7d8ce0b5")
T5 = ModelFile("text_encoders", "t5gemma_b_b_ul2.safetensors", 1187264003,
               "1e1eba25be8872edb0d3c6335c6658fd6388e7b14b60da6e454e404cfcd8150e")


def needed(gpu: bool) -> list[ModelFile]:
    """Small and the text encoder always (quality 1 and CPU mode); Medium only with a GPU."""
    return [SMALL, T5, MEDIUM] if gpu else [SMALL, T5]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8 * CHUNK):
            h.update(chunk)
    return h.hexdigest()


class ModelStore:
    def __init__(self, root: Path, files: list[ModelFile], client: httpx.AsyncClient,
                 base_url: str = "https://huggingface.co"):
        self.root = Path(root)
        self.files = files
        self.client = client
        self.base_url = base_url.rstrip("/")
        self._cache_path = self.root / ".verified.json"

    def path(self, f: ModelFile) -> Path:
        return self.root / f.rel

    def _part(self, f: ModelFile) -> Path:
        return self.path(f).with_name(f.name + ".part")

    def _cache(self) -> dict:
        try:
            return json.loads(self._cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _stamp(self, f: ModelFile) -> dict | None:
        try:
            st = self.path(f).stat()
        except OSError:
            return None
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": f.sha256}

    def verified(self, f: ModelFile) -> bool:
        stamp = self._stamp(f)
        return stamp is not None and stamp["size"] == f.size and self._cache().get(f.rel) == stamp

    def _remember(self, f: ModelFile) -> None:
        cache = self._cache()
        cache[f.rel] = self._stamp(f)
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=1))
        os.replace(tmp, self._cache_path)

    def _have(self, f: ModelFile) -> int:
        for p in (self.path(f), self._part(f)):
            if p.exists():
                return min(p.stat().st_size, f.size)
        return 0

    async def ensure(self, report: Callable[[str], None]) -> None:
        """Download what's missing (resuming .part files), check every unverified file's SHA-256."""
        self.root.mkdir(parents=True, exist_ok=True)
        total = sum(f.size for f in self.files)
        done = {f.rel: self._have(f) for f in self.files}

        def progress() -> None:
            report(f"downloading {int(100 * sum(done.values()) / total)}%")

        for f in self.files:
            if self.verified(f):
                continue
            path = self.path(f)
            if not (path.exists() and path.stat().st_size == f.size):
                progress()

                def on_bytes(n: int, rel: str = f.rel) -> None:
                    done[rel] = n
                    progress()

                await self._download(f, on_bytes)
            report(f"verifying {f.name}")
            digest = await asyncio.to_thread(sha256_file, path)
            if digest != f.sha256:
                with contextlib.suppress(OSError):
                    path.unlink()
                raise ModelError(f"{f.name}: checksum mismatch (got {digest[:12]}…); deleted, will download again")
            self._remember(f)

    async def _download(self, f: ModelFile, on_bytes: Callable[[int], None]) -> None:
        path, part = self.path(f), self._part(f)
        path.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if have == f.size:
            os.replace(part, path)
            return
        if have > f.size:
            part.unlink()
            have = 0
        url = f"{self.base_url}/{REPO}/{f.rel}"
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            async with self.client.stream("GET", url, headers=headers, follow_redirects=True) as r:
                if r.status_code == 206 and have:
                    mode = "ab"
                elif r.status_code == 200:
                    mode, have = "wb", 0
                else:
                    raise ModelError(f"{f.name}: HTTP {r.status_code} from {url}; will resume")
                with open(part, mode) as out:
                    async for chunk in r.aiter_bytes():
                        out.write(chunk)
                        have += len(chunk)
                        on_bytes(have)
        except httpx.HTTPError as e:
            raise ModelError(f"{f.name}: download interrupted ({e}); will resume") from e
        if have != f.size:
            raise ModelError(f"{f.name}: got {have} of {f.size} bytes; will resume")
        os.replace(part, path)
