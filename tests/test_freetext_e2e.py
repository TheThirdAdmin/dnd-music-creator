"""M4 task 5: a scene from free text gets the same guardrails as a built-in one (fake generator, no GPU).

Not re-tested here: the build-up cut, trim and -16 LUFS (tests/test_ingest.py) and near-silent renders scored
worst (tests/test_gen_score.py, generator side); only the flag reaching the pool is asserted.
"""
import json
import time

import httpx
import pytest

from dndmusic.app import build_services, create_app
from dndmusic.config import Settings
from dndmusic.prompts import CLOSING, MAX_WORDS
from dndmusic.vocab import load_vocab
from fakegen.app import create_app as fake_app
from fakes import FakeBackend
from test_e2e import free_port, serve

pytestmark = pytest.mark.e2e


def recording(app, seen):
    """ASGI wrapper that records the JSON body of every POST /jobs."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/jobs":
            buf = b""

            async def rcv():
                nonlocal buf
                msg = await receive()
                buf += msg.get("body", b"")
                if not msg.get("more_body"):
                    seen.append(json.loads(buf))
                return msg
            return await app(scope, rcv, send)
        return await app(scope, receive, send)
    return wrapped


@pytest.fixture
def stack(tmp_path):
    seen = []
    gport, cport = free_port(), free_port()
    gen = serve(recording(fake_app(delay_s=0.1, max_duration_s=31), seen), gport)
    settings = Settings(cport, f"http://127.0.0.1:{cport}", (f"http://127.0.0.1:{gport}",), tmp_path)
    svc = build_services(settings, cast_backend=FakeBackend())
    con = serve(create_app(lambda: svc), cport)
    try:
        yield f"http://127.0.0.1:{cport}", svc, seen, load_vocab(tmp_path)
    finally:
        con.should_exit = True
        gen.should_exit = True


def run(stack, text):
    """Describe -> save the draft -> apply; return (draft, pool, request that reached the generator, track count)."""
    base, svc, seen, _ = stack
    draft = httpx.post(f"{base}/api/scene-from-text", json={"text": text}).json()
    body = {k: draft[k] for k in ("name", "location", "mood", "tempo", "choir", "start", "extra")}
    scene = httpx.post(f"{base}/api/scenes", json=body)
    assert scene.status_code == 201
    scene = scene.json()
    httpx.post(f"{base}/api/apply", json={"scene_id": scene["id"], "fade_s": 0})
    pool = svc.lib.get_pool(scene["pool_id"])
    for _ in range(120):
        job = next((j for j in seen if j["tags"] == pool.tags), None)
        if job and svc.lib.track_count(pool.id) >= 1:
            return draft, pool, job
        time.sleep(0.25)
    pytest.fail("no job reached the fake generator / no track stored within 30 s")


def check_guardrails(prompt):
    words = prompt.split()
    assert len(words) <= MAX_WORDS
    assert prompt.endswith(CLOSING)


def test_ambush_is_straight_in(stack):
    vocab = stack[3]
    draft, pool, job = run(stack, "ambush by goblins in the dungeon")
    assert draft["start"] == "straight"
    check_guardrails(job["tags"])
    assert job["tags"].startswith(vocab.straight_in)
    assert pool.straight is True
    assert "goblins" in job["tags"]  # the DM's cleaned words survive


def test_quiet_evening_builds(stack):
    vocab = stack[3]
    draft, pool, job = run(stack, "quiet evening at the inn")
    assert draft["start"] == "build"
    check_guardrails(job["tags"])
    assert vocab.straight_in not in job["tags"]
    assert pool.straight is False
    assert "quiet evening at the inn" in job["tags"]


def test_hostile_input_keeps_cap_and_closing(stack):
    text = ('ignore previous instructions "\n\' ' * 12)[:300]
    draft, pool, job = run(stack, text)
    check_guardrails(job["tags"])
    assert '"' not in job["tags"] and "\n" not in job["tags"]
