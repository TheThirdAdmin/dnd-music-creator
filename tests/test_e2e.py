import socket
import subprocess
import threading
import time

import httpx
import numpy as np
import pytest
import uvicorn

from dndmusic.app import build_services, create_app
from dndmusic.config import Settings
from dndmusic.models import Scene
from dndmusic.prompts import scene_prompt
from dndmusic.vocab import load_vocab
from fakegen.app import create_app as fake_app
from fakegen.app import pitch_for
from fakes import FakeBackend

pytestmark = pytest.mark.e2e


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def test_scene_to_stream(tmp_path):
    gport, cport = free_port(), free_port()
    gen = serve(fake_app(delay_s=0.2, max_duration_s=31), gport)
    settings = Settings(cport, f"http://127.0.0.1:{cport}", (f"http://127.0.0.1:{gport}",), tmp_path)
    con = serve(create_app(lambda: build_services(settings, cast_backend=FakeBackend())), cport)
    base = f"http://127.0.0.1:{cport}"
    try:
        scene = httpx.post(f"{base}/api/scenes", json={"name": "Road", "location": "forest road", "mood": "calm"}).json()
        assert httpx.post(f"{base}/api/apply", json={"scene_id": scene["id"], "fade_s": 1}).status_code == 200
        for _ in range(120):
            if httpx.get(f"{base}/api/state").json()["now_playing"]:
                break
            time.sleep(0.5)
        else:
            pytest.fail("no track was generated and played within 60 s")
        data = bytearray()
        with httpx.stream("GET", f"{base}/stream.mp3", timeout=10) as r:
            assert r.headers["content-type"] == "audio/mpeg"
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > 60_000:
                    break
        f = tmp_path / "s.mp3"
        f.write_bytes(data)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate",
                                "-of", "csv=p=0", str(f)], capture_output=True, text=True)
        assert probe.stdout.strip() == "mp3,48000"
    finally:
        con.should_exit = True
        gen.should_exit = True


def grab(base, seconds=3.0):
    """Roughly `seconds` of the live MP3 stream (192 kbit/s), decoded to 48 kHz mono float."""
    need = int(seconds * 24_000)
    data = bytearray()
    with httpx.stream("GET", f"{base}/stream.mp3", timeout=10) as r:
        for chunk in r.iter_bytes():
            data += chunk
            if len(data) >= need:
                break
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "mp3", "-i", "-", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
                       input=bytes(data), capture_output=True, check=True)
    return np.frombuffer(p.stdout, dtype="<f4")


def dominant_hz(x, rate=48000):
    x = x[-rate:]
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[np.argmax(spectrum)])


def wait_playing(base, pool_id, timeout_s=60):
    for _ in range(int(timeout_s / 0.5)):
        np_ = httpx.get(f"{base}/api/state").json()["now_playing"]
        if np_ and np_["pool_id"] == pool_id:
            return
        time.sleep(0.5)
    pytest.fail(f"pool {pool_id} did not start playing within {timeout_s} s")


def test_apply_changes_the_dominant_pitch(tmp_path):
    vocab = load_vocab(tmp_path)
    a = {"name": "Inn", "location": "tavern", "mood": "calm"}
    b = {"name": "Fight", "location": "battlefield", "mood": "combat", "tempo": "fast"}
    pa, pb = (pitch_for(scene_prompt(Scene(**x), vocab).tags) for x in (a, b))
    assert abs(pa - pb) / pa > 0.05, "pick two scenes whose fake pitches differ"
    gport, cport = free_port(), free_port()
    gen = serve(fake_app(delay_s=0.2, max_duration_s=31), gport)
    settings = Settings(cport, f"http://127.0.0.1:{cport}", (f"http://127.0.0.1:{gport}",), tmp_path)
    con = serve(create_app(lambda: build_services(settings, cast_backend=FakeBackend())), cport)
    base = f"http://127.0.0.1:{cport}"
    try:
        sa = httpx.post(f"{base}/api/scenes", json=a).json()
        sb = httpx.post(f"{base}/api/scenes", json=b).json()
        httpx.post(f"{base}/api/apply", json={"scene_id": sa["id"], "fade_s": 0})
        wait_playing(base, sa["pool_id"])
        time.sleep(3)
        assert abs(dominant_hz(grab(base)) - pa) / pa < 0.03
        httpx.post(f"{base}/api/apply", json={"scene_id": sb["id"], "fade_s": 0})
        wait_playing(base, sb["pool_id"])
        time.sleep(3)
        assert abs(dominant_hz(grab(base)) - pb) / pb < 0.03
    finally:
        con.should_exit = True
        gen.should_exit = True
