"""Runpod serverless entrypoint: run the normal generator API in this container and proxy jobs to it."""
import asyncio
import base64
import logging
import os
import subprocess
import sys
import time

import httpx

log = logging.getLogger("runpod_worker")
BASE = f"http://127.0.0.1:{os.environ.get('PORT', '8001')}"
READY_TIMEOUT_S = float(os.environ.get("READY_TIMEOUT_S", "1800"))
OUTPUT_LIMIT = int(os.environ.get("OUTPUT_LIMIT_BYTES", str(7 * 1024 * 1024)))  # raw bytes; base64 adds a third
POLL_S = 1.0


def ffmpeg_ogg(audio: bytes) -> bytes:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "192k",
                        "-f", "ogg", "pipe:1"], input=audio, capture_output=True, check=True)
    return r.stdout


def fit_output(audio: bytes, mime: str, limit: int, transcode=ffmpeg_ogg) -> tuple[bytes, str]:
    if len(audio) <= limit:
        return audio, mime
    small = transcode(audio)
    if len(small) > limit:
        raise RuntimeError(f"audio too large for the job output ({len(small)} bytes, limit {limit})")
    return small, "audio/ogg"


ERROR_POLLS = 3  # consecutive "error: ..." health polls before a job fails instead of hanging


async def wait_ready(client: httpx.AsyncClient, timeout_s: float = READY_TIMEOUT_S, poll_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    errors = 0
    while time.monotonic() < deadline:
        try:
            h = (await client.get("/health")).json()
            if h.get("ok"):
                return
            status = str(h.get("status") or "")
            errors = errors + 1 if status.startswith("error") else 0
            if errors >= ERROR_POLLS:
                raise RuntimeError(f"generator is not starting: {status}")
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(poll_s)
    raise RuntimeError("generator did not become ready (model download or ComfyUI start failed)")


async def render(client: httpx.AsyncClient, job_input: dict) -> dict:
    t0 = time.monotonic()
    r = await client.post("/jobs", json=job_input)
    r.raise_for_status()
    job_id = r.json()["id"]
    while True:
        st = (await client.get(f"/jobs/{job_id}")).json()
        if st["status"] == "done":
            break
        if st["status"] == "failed":
            raise RuntimeError(st.get("error") or "render failed")
        await asyncio.sleep(POLL_S)
    audio = (await client.get(f"/jobs/{job_id}/audio")).content
    audio, mime = fit_output(audio, "audio/flac", OUTPUT_LIMIT)
    return {"audio_b64": base64.b64encode(audio).decode(), "mime": mime, "seed": st["seed"],
            "duration_s": st["duration_s"], "render_seconds": round(time.monotonic() - t0, 2)}


_client: httpx.AsyncClient | None = None


async def handler(job: dict) -> dict:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=BASE, timeout=httpx.Timeout(60.0))
    await wait_ready(_client)
    return await render(_client, job["input"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import runpod   # only in the worker image
    gen = subprocess.Popen([sys.executable, "-m", "generator"])
    log.info("generator started (pid %d); waiting for Runpod jobs", gen.pid)
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":
    main()
