"""ComfyUI as a child process: the Stable Audio 3 graph, ComfyUI's HTTP API, starting and stopping it."""
import asyncio
import logging
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import GenSettings

log = logging.getLogger(__name__)
TEXT_ENCODER = "t5gemma_b_b_ul2.safetensors"


class ComfyError(Exception):
    """ComfyUI refused or failed a render, or could not be reached."""


def sa3_graph(ckpt: str, prompt: str, seconds: int, seed: int, steps: int, prefix: str) -> dict:
    """The fixed Stable Audio 3 text-to-audio graph used in the M0 listening rounds (cfg 1 makes the
    negative irrelevant; ConditioningZeroOut makes that explicit)."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": TEXT_ENCODER, "type": "stable_audio",
                                                      "device": "default"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": prompt}},
        "4": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}},
        "5": {"class_type": "EmptyLatentAudio", "inputs": {"seconds": seconds, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0], "latent_image": ["5", 0],
            "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "lcm", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudio", "inputs": {"audio": ["7", 0], "filename_prefix": prefix}},
    }


@dataclass
class Output:
    audio: bytes
    filename: str
    subfolder: str
    render_s: float | None


class ComfyClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None, poll_s: float = 0.5,
                 timeout_s: float = 1800):
        self.base_url = base_url
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=60)
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.client_id = uuid.uuid4().hex

    async def ready(self) -> bool:
        try:
            return (await self.client.get("/system_stats")).status_code == 200
        except httpx.HTTPError:
            return False

    async def run(self, graph: dict, kind: str = "audio") -> Output:
        try:
            r = await self.client.post("/prompt", json={"prompt": graph, "client_id": self.client_id})
            if r.status_code != 200:
                raise ComfyError(f"ComfyUI refused the graph: {r.text[:300]}")
            pid = r.json()["prompt_id"]
            deadline = time.monotonic() + self.timeout_s
            while True:
                entry = (await self.client.get(f"/history/{pid}")).json().get(pid)
                status = (entry or {}).get("status") or {}
                if status.get("completed") or status.get("status_str") == "error":
                    break
                if time.monotonic() > deadline:
                    raise ComfyError(f"render timed out after {self.timeout_s:g} s")
                await asyncio.sleep(self.poll_s)
            msgs = {m[0]: m[1] for m in status.get("messages", [])}
            if status.get("status_str") != "success":
                message = str(msgs.get("execution_error", {}).get("exception_message", "")).strip()
                raise ComfyError(message or "render failed")
            files = [f for node in entry.get("outputs", {}).values() for f in node.get(kind, [])]
            if not files:
                raise ComfyError(f"render produced no {kind}")
            f = files[0]
            got = await self.client.get("/view", params={"filename": f["filename"], "subfolder": f.get("subfolder", ""),
                                                         "type": f.get("type", "output")})
            if got.status_code != 200:
                raise ComfyError(f"could not fetch {f['filename']}: HTTP {got.status_code}")
            start = msgs.get("execution_start", {}).get("timestamp")
            end = msgs.get("execution_success", {}).get("timestamp")
            return Output(got.content, f["filename"], f.get("subfolder", ""),
                          (end - start) / 1000 if start and end else None)
        except httpx.HTTPError as e:
            raise ComfyError(f"ComfyUI unreachable: {e}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise ComfyError(f"unexpected reply from ComfyUI: {e!r}") from e

    async def free(self) -> None:
        try:
            await self.client.post("/free", json={"unload_models": True, "free_memory": True})
        except httpx.HTTPError as e:
            raise ComfyError(f"ComfyUI unreachable: {e}") from e


class ComfyRenderer:
    """One Stable Audio 3 render through ComfyUI; returns the FLAC and deletes ComfyUI's copy."""

    def __init__(self, comfy: ComfyClient, steps: int, output_dir: Path):
        self.comfy = comfy
        self.steps = steps
        self.output_dir = Path(output_dir)

    async def render(self, ckpt: str, prompt: str, seconds: int, seed: int) -> bytes:
        out = await self.comfy.run(sa3_graph(ckpt, prompt, seconds, seed, self.steps,
                                             f"dndgen/{uuid.uuid4().hex[:12]}"))
        (self.output_dir / out.subfolder / out.filename).unlink(missing_ok=True)
        if out.render_s is not None:
            log.info("rendered %d s with %s (seed %d) in %.1f s", seconds, ckpt, seed, out.render_s)
        return out.audio

    async def free(self) -> None:
        await self.comfy.free()


class ComfyProcess:
    """ComfyUI as a child process; its output goes to ours (docker logs)."""

    def __init__(self, cmd: list[str], cwd: Path | None = None):
        self.cmd = cmd
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    @property
    def returncode(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def stop(self, timeout_s: float = 10) -> None:
        if not self.alive():
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout_s)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def write_model_paths(s: GenSettings) -> Path:
    """Prepares ComfyUI's base directory and extra_model_paths.yaml pointing at MODEL_DIR."""
    path = s.work_dir / "extra_model_paths.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    (s.work_dir / "comfy" / "custom_nodes").mkdir(parents=True, exist_ok=True)
    path.write_text(f"dndmusic:\n  base_path: {s.model_dir}\n  checkpoints: checkpoints\n  text_encoders: text_encoders\n")
    return path


def comfy_command(s: GenSettings, model_paths: Path) -> list[str]:
    cmd = [sys.executable, str(s.comfy_dir / "main.py"), "--listen", "127.0.0.1", "--port", str(s.comfy_port),
           "--disable-auto-launch", "--base-directory", str(s.work_dir / "comfy"),
           "--extra-model-paths-config", str(model_paths)]
    if not s.gpu:
        cmd.append("--cpu")
    return cmd
