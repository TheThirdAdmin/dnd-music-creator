import io
import sys
import time
from pathlib import Path

import httpx
import pytest
import soundfile as sf

from fakecomfy import create_app as fake_comfy
from generator.comfy import (ComfyClient, ComfyError, ComfyProcess, ComfyRenderer, comfy_command, sa3_graph,
                             write_model_paths)
from generator.config import GenSettings
from helpers import free_port

FAKECOMFY = str(Path(__file__).with_name("fakecomfy.py"))


def comfy_for(app):
    return ComfyClient("http://comfy", client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                                base_url="http://comfy"), poll_s=0.01)


def test_graph_matches_the_approved_settings():
    g = sa3_graph("stable_audio_3_medium.safetensors", "Epic.", 200, 42, 8, "dndgen/x")
    by = {n["class_type"]: n["inputs"] for n in g.values()}
    assert by["CheckpointLoaderSimple"] == {"ckpt_name": "stable_audio_3_medium.safetensors"}
    assert by["CLIPLoader"] == {"clip_name": "t5gemma_b_b_ul2.safetensors", "type": "stable_audio", "device": "default"}
    assert by["CLIPTextEncode"]["text"] == "Epic."
    k = by["KSampler"]
    assert (k["sampler_name"], k["scheduler"], k["cfg"], k["steps"], k["seed"], k["denoise"]) == \
        ("lcm", "simple", 1.0, 8, 42, 1.0)
    assert g[k["negative"][0]]["class_type"] == "ConditioningZeroOut"
    assert by["EmptyLatentAudio"] == {"seconds": 200, "batch_size": 1}
    assert by["VAEDecodeAudio"]["vae"] == ["1", 2]
    assert by["SaveAudio"]["filename_prefix"] == "dndgen/x"


async def test_run_returns_the_flac(tmp_path):
    c = comfy_for(fake_comfy(tmp_path))
    assert await c.ready()
    out = await c.run(sa3_graph("m", "Epic.", 3, 1, 8, "dndgen/a"))
    x, rate = sf.read(io.BytesIO(out.audio))
    assert abs(len(x) / rate - 3) < 0.01 and out.render_s == 2.5
    assert (tmp_path / out.subfolder / out.filename).exists()


async def test_render_errors_carry_comfyuis_message(tmp_path):
    c = comfy_for(fake_comfy(tmp_path, error="CUDA out of memory"))
    with pytest.raises(ComfyError, match="CUDA out of memory"):
        await c.run(sa3_graph("m", "Epic.", 3, 1, 8, "dndgen/a"))


async def test_refused_graph(tmp_path):
    with pytest.raises(ComfyError, match="refused"):
        await comfy_for(fake_comfy(tmp_path)).run({"1": {"class_type": "Nope", "inputs": {}}})


async def test_unreachable():
    c = ComfyClient(f"http://127.0.0.1:{free_port()}")
    assert await c.ready() is False
    with pytest.raises(ComfyError, match="unreachable"):
        await c.run(sa3_graph("m", "x", 3, 1, 8, "p/q"))


async def test_renderer_cleans_up_and_frees(tmp_path):
    app = fake_comfy(tmp_path)
    r = ComfyRenderer(comfy_for(app), steps=8, output_dir=tmp_path)
    audio = await r.render("stable_audio_3_small_music.safetensors", "Calm.", 2, 5)
    assert audio[:4] == b"fLaC" and list(tmp_path.rglob("*.flac")) == []
    by = {n["class_type"]: n["inputs"] for n in app.state.graphs[0].values()}
    assert by["CheckpointLoaderSimple"]["ckpt_name"] == "stable_audio_3_small_music.safetensors"
    assert by["KSampler"]["steps"] == 8 and by["KSampler"]["seed"] == 5
    await r.free()
    assert app.state.frees == [{"unload_models": True, "free_memory": True}]


def test_command_and_model_paths(tmp_path):
    cpu = GenSettings(device="cpu", model_dir=tmp_path / "models", work_dir=tmp_path / "work")
    p = write_model_paths(cpu)
    assert f"base_path: {tmp_path / 'models'}" in p.read_text()
    assert (tmp_path / "work" / "comfy" / "custom_nodes").is_dir()
    cmd = comfy_command(cpu, p)
    assert cmd[cmd.index("--listen") + 1] == "127.0.0.1" and cmd[cmd.index("--port") + 1] == "8188"
    assert cmd[cmd.index("--base-directory") + 1] == str(tmp_path / "work" / "comfy")
    assert cmd[cmd.index("--extra-model-paths-config") + 1] == str(p)
    assert cmd[-1] == "--cpu" and "--disable-auto-launch" in cmd
    assert "--cpu" not in comfy_command(GenSettings(device="gpu", work_dir=tmp_path / "work"), p)


def test_process_starts_and_stops(tmp_path):
    port = free_port()
    proc = ComfyProcess([sys.executable, FAKECOMFY, "--port", str(port), "--output-directory", str(tmp_path)])
    proc.start()
    try:
        for _ in range(200):
            try:
                if httpx.get(f"http://127.0.0.1:{port}/system_stats").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            pytest.fail("fake ComfyUI did not start")
        assert proc.alive() and proc.pid > 0
    finally:
        proc.stop()
    assert not proc.alive() and proc.returncode is not None
