"""Fake ComfyUI: just enough of /prompt, /history, /view, /free and /system_stats for the generator's tests.
As a process: python tests/fakecomfy.py --port 8188 --output-directory DIR"""
import argparse
import asyncio
import hashlib
import io
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, HTTPException, Response

RATE = 8000
SA3_NODES = sorted(["CheckpointLoaderSimple", "CLIPLoader", "CLIPTextEncode", "ConditioningZeroOut",
                    "EmptyLatentAudio", "KSampler", "VAEDecodeAudio", "SaveAudio"])


def render(prompt: str, seconds: float, seed: int) -> bytes:
    pitch = 220.0 * 2 ** ((int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16) % 24) / 12)
    t = np.arange(int(RATE * seconds)) / RATE
    x = 0.3 * np.sin(2 * np.pi * pitch * t) + 0.02 * np.random.default_rng(seed).standard_normal(len(t))
    buf = io.BytesIO()
    sf.write(buf, np.stack([x, x], axis=1).astype("float32"), RATE, format="FLAC")
    return buf.getvalue()


def render_image(prompt: str, seed: int, width: int, height: int) -> bytes:
    """A blocky many-coloured PNG, so palette quantising and downscaling have something to do."""
    from PIL import Image
    rng = np.random.default_rng(seed)
    blocks = rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(blocks).resize((width, height), Image.BILINEAR).save(buf, format="PNG")
    return buf.getvalue()


def create_app(output_dir: Path, *, delay_s: float = 0.0, error: str | None = None) -> FastAPI:
    app = FastAPI()
    app.state.graphs, app.state.frees = [], []
    history: dict[str, dict] = {}
    tasks: set[asyncio.Task] = set()
    output_dir = Path(output_dir)

    def node(graph: dict, cls: str) -> dict:
        return next(n["inputs"] for n in graph.values() if n["class_type"] == cls)

    async def finish(pid: str, graph: dict) -> None:
        await asyncio.sleep(delay_s)
        start = ["execution_start", {"timestamp": 1000}]
        if error:
            history[pid] = {"status": {"status_str": "error", "completed": False,
                                       "messages": [start, ["execution_error", {"exception_message": error}]]},
                            "outputs": {}}
            return
        if any(n["class_type"] == "SaveImage" for n in graph.values()):
            subfolder, _, stem = node(graph, "SaveImage")["filename_prefix"].rpartition("/")
            name = f"{stem}_00001_.png"
            size = next(n["inputs"] for n in graph.values() if n["class_type"].startswith("Empty") and "width" in n["inputs"])
            (output_dir / subfolder).mkdir(parents=True, exist_ok=True)
            (output_dir / subfolder / name).write_bytes(render_image(
                node(graph, "CLIPTextEncode")["text"], node(graph, "KSampler")["seed"], size["width"], size["height"]))
            history[pid] = {"status": {"status_str": "success", "completed": True,
                                       "messages": [start, ["execution_success", {"timestamp": 3500}]]},
                            "outputs": {"9": {"images": [{"filename": name, "subfolder": subfolder,
                                                          "type": "output"}]}}}
            return
        subfolder, _, stem = node(graph, "SaveAudio")["filename_prefix"].rpartition("/")
        name = f"{stem}_00001_.flac"
        (output_dir / subfolder).mkdir(parents=True, exist_ok=True)
        audio = render(node(graph, "CLIPTextEncode")["text"], node(graph, "EmptyLatentAudio")["seconds"],
                       node(graph, "KSampler")["seed"])
        (output_dir / subfolder / name).write_bytes(audio)
        history[pid] = {"status": {"status_str": "success", "completed": True,
                                   "messages": [start, ["execution_success", {"timestamp": 3500}]]},
                        "outputs": {"8": {"audio": [{"filename": name, "subfolder": subfolder, "type": "output"}]}}}

    @app.post("/prompt")
    async def prompt(body: dict):
        graph = body.get("prompt") or {}
        classes = {n.get("class_type", "") for n in graph.values()}
        if "SaveImage" not in classes and sorted(n.get("class_type", "") for n in graph.values()) != SA3_NODES:
            raise HTTPException(400, {"error": "unexpected graph"})
        app.state.graphs.append(graph)
        pid = uuid.uuid4().hex
        task = asyncio.create_task(finish(pid, graph))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"prompt_id": pid, "number": len(app.state.graphs), "node_errors": {}}

    @app.get("/history/{pid}")
    async def get_history(pid: str):
        return {pid: history[pid]} if pid in history else {}

    @app.get("/view")
    async def view(filename: str, subfolder: str = "", type: str = "output"):
        path = output_dir / subfolder / filename
        if not path.exists():
            raise HTTPException(404)
        return Response(path.read_bytes(), media_type="image/png" if filename.endswith(".png") else "audio/flac")

    @app.post("/free")
    async def free(body: dict):
        app.state.frees.append(body)
        return {}

    @app.get("/system_stats")
    async def system_stats():
        return {"system": {"os": "fake"}, "devices": []}

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--output-directory", required=True)
    a = ap.parse_args()
    uvicorn.run(create_app(Path(a.output_directory)), host="127.0.0.1", port=a.port, log_level="warning")
