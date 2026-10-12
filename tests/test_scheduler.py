import asyncio
from collections import Counter
import json
from pathlib import Path

import httpx
import pytest

from dndmusic.genclient import GeneratorClient, GeneratorError
from dndmusic.models import Scene
from dndmusic.prompts import scene_prompt
from dndmusic.scheduler import Scheduler, job_quality, next_pool
from dndmusic.vocab import load_vocab
from fakegen.app import create_app as fake_app


def pick(pending=None, playing=None, scenes=(), counts=None, fresh=None, favs=(), flight=None):
    counts = counts or {}
    fresh = counts if fresh is None else fresh
    return next_pool(pending=pending, playing=playing, scene_pools=list(scenes),
                     count=lambda p: counts.get(p, 0), fresh=lambda p: fresh.get(p, 0),
                     has_fav=lambda p: p in favs, in_flight=Counter(flight or {}))


def test_live_pending_pool_first():
    assert pick(pending=9, playing=1, scenes=[1, 2]) == 9


def test_pending_already_in_flight_falls_through():
    assert pick(pending=9, playing=1, scenes=[1], counts={1: 5}, fresh={1: 1}, flight={9: 1}) == 1


def test_playing_pool_kept_fresh():
    assert pick(playing=1, scenes=[2], counts={1: 3, 2: 3}, fresh={1: 2, 2: 3}) == 1


def test_scenes_filled_to_three_in_order():
    assert pick(scenes=[1, 2], counts={1: 3, 2: 1}) == 2


def test_in_flight_jobs_count():
    assert pick(scenes=[1, 2], counts={1: 2, 2: 0}, flight={1: 1, 2: 3}) is None


def test_favourite_scenes_topped_up_to_eight():
    assert pick(scenes=[1, 2], counts={1: 3, 2: 3}, favs={2}) == 2
    assert pick(scenes=[1, 2], counts={1: 3, 2: 8}, favs={2}) is None


class StubDirector:
    def __init__(self):
        self.pending_pool_id = None
        self.playing_pool_id = None
        self.added = []

    def on_track_added(self, pool_id):
        self.added.append(pool_id)


def fake_client(max_duration_s):
    app = fake_app(delay_s=0, max_duration_s=max_duration_s)
    return GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fake"))


@pytest.fixture
def env(lib):
    vocab = load_vocab(lib.data_dir)
    scene = lib.create_scene(Scene(name="Tavern", location="tavern", mood="calm"))
    return lib, vocab, lib.pool_for(scene_prompt(scene, vocab), scene_id=scene.id)


async def test_run_one_fills_a_scene_pool(env):
    lib, vocab, pool = env
    director, client = StubDirector(), fake_client(31)
    s = Scheduler(lib, vocab, [client], director, poll_s=0.01)
    assert await s.run_one(client) is True
    [track] = lib.tracks(pool.id)
    assert Path(track.path).exists() and 29.5 <= track.duration_s <= 31.5
    assert track.generator == "http://fake" and director.added == [pool.id]
    assert s.in_flight[pool.id] == 0


async def test_run_one_idle_when_nothing_is_needed(env):
    lib, vocab, pool = env
    for i in range(3):
        lib.add_track(pool.id, lib.new_track_path(), 100.0, i, "g")
    client = fake_client(31)
    assert await Scheduler(lib, vocab, [client], StubDirector()).run_one(client) is False


async def test_rejected_audio_is_dropped(env):
    lib, vocab, pool = env
    director, client = StubDirector(), fake_client(5)
    s = Scheduler(lib, vocab, [client], director, poll_s=0.01)
    assert await s.run_one(client) is True
    assert director.added == [] and s.failures == 1
    assert list(lib.tracks_dir.iterdir()) == []


async def test_generator_error_releases_the_slot(env):
    lib, vocab, pool = env
    client = GeneratorClient("http://down", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)), base_url="http://down"))
    s = Scheduler(lib, vocab, [client], StubDirector())
    with pytest.raises(GeneratorError):
        await s.run_one(client)
    assert sum(s.in_flight.values()) == 0


async def test_low_disk_pauses(env):
    lib, vocab, pool = env
    client = fake_client(31)
    s = Scheduler(lib, vocab, [client], StubDirector(), disk_free=lambda: 10)
    assert await s.run_one(client) is False and s.disk_low is True


async def test_failed_generator_job_raises_error(env):
    lib, vocab, pool = env
    def mock_handler(request):
        if request.method == "POST" and request.url.path == "/jobs":
            return httpx.Response(202, json={"id": "j1"})
        elif request.method == "GET" and request.url.path == "/jobs/j1":
            return httpx.Response(200, json={"id": "j1", "status": "failed", "seed": 1, "duration_s": 120, "error": "out of memory"})
        return httpx.Response(404)

    client = GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler), base_url="http://fake"))
    s = Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01)
    with pytest.raises(GeneratorError, match="out of memory"):
        await s.run_one(client)
    assert sum(s.in_flight.values()) == 0
    assert s.failures == 0


async def test_job_that_never_finishes_times_out(env):
    lib, vocab, pool = env

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"id": "j1"})
        return httpx.Response(200, json={"id": "j1", "status": "running", "seed": 1, "duration_s": 120, "error": None})

    client = GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake"))
    s = Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01, job_timeout_s=0.05)
    with pytest.raises(GeneratorError, match="job j1 timed out after 0.05 s"):
        await s.run_one(client)
    assert sum(s.in_flight.values()) == 0


@pytest.mark.parametrize("wanted,offered,expected", [
    (2, None, 2), (3, [1, 2, 3], 3), (3, [1], 1), (2, [1], 1), (1, [1, 2, 3], 1), (1, [2, 3], 2),
])
def test_job_quality(wanted, offered, expected):
    assert job_quality(wanted, offered) == expected


async def test_quality_setting_is_sent_and_recorded(env):
    lib, vocab, pool = env
    lib.set_state("quality", 3)
    sent = []
    app = fake_app(delay_s=0, max_duration_s=31)
    transport = httpx.ASGITransport(app=app)

    class Spy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "POST":
                sent.append(json.loads(await request.aread()))
            return await transport.handle_async_request(request)

    client = GeneratorClient("http://fake", client=httpx.AsyncClient(transport=Spy(), base_url="http://fake"))
    s = Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01)
    assert await s.run_one(client) is True
    assert sent[0]["quality"] == 3
    assert lib.tracks(pool.id)[0].quality == 3


async def test_quality_is_capped_by_what_the_generator_offers(env):
    lib, vocab, pool = env
    client = fake_client(31)
    s = Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01)
    s.health[client.base_url]["qualities"] = [1]
    assert await s.run_one(client) is True
    assert lib.tracks(pool.id)[0].quality == 1


async def test_health_check_records_generator_capabilities(env):
    lib, vocab, pool = env
    client = fake_client(31)
    s = Scheduler(lib, vocab, [client], StubDirector())
    await s.check_health(client)
    assert s.health[client.base_url] == {"ok": True, "busy": False, "error": None, "gpu": False,
                                         "qualities": [1, 2, 3], "status": "ok"}


async def test_straight_in_pools_are_ingested_with_the_cut(lib, monkeypatch):
    vocab = load_vocab(lib.data_dir)
    scene = lib.create_scene(Scene(name="Fight", location="battlefield", mood="combat", tempo="fast",
                                   start="straight"))
    pool = lib.pool_for(scene_prompt(scene, vocab), scene_id=scene.id)
    assert pool.straight is True
    calls = []

    def fake_ingest(raw, path, *, straight=False):
        calls.append(straight)
        path.write_bytes(b"x")
        return 60.0

    monkeypatch.setattr("dndmusic.scheduler.ingest", fake_ingest)
    client = fake_client(31)
    assert await Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01).run_one(client) is True
    assert calls == [True]


def gen_handler(health, sent, not_ready):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json=health())
        if request.method == "POST" and request.url.path == "/jobs":
            sent.append((not_ready(), json.loads(request.content)))
            return httpx.Response(202, json={"id": "j1"})
        if request.url.path == "/jobs/j1":
            return httpx.Response(200, json={"id": "j1", "status": "failed", "seed": 1, "duration_s": 60,
                                             "error": "stop here"})
        return httpx.Response(404)
    return handler


async def run_worker_until(s, client, sent):
    task = asyncio.create_task(s.worker(client))
    try:
        for _ in range(500):
            if sent:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_worker_waits_for_a_not_ready_generator(env):
    lib, vocab, pool = env
    calls = {"n": 0}

    def health():
        calls["n"] += 1
        if calls["n"] <= 3:
            return {"ok": False, "status": "downloading 43%"}
        return {"ok": True, "qualities": [1, 2, 3], "status": "ok"}

    sent = []
    client = GeneratorClient("http://gen", client=httpx.AsyncClient(
        transport=httpx.MockTransport(gen_handler(health, sent, lambda: calls["n"] <= 3)), base_url="http://gen"))
    s = Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01, backoff=(0.01,), not_ready_poll_s=0.01)
    await run_worker_until(s, client, sent)
    assert sent and sent[0][0] is False and calls["n"] >= 4


async def test_first_job_respects_what_a_cpu_generator_offers(env):
    lib, vocab, pool = env
    lib.set_state("quality", 2)
    sent = []
    client = GeneratorClient("http://gen", client=httpx.AsyncClient(
        transport=httpx.MockTransport(gen_handler(
            lambda: {"ok": True, "gpu": False, "qualities": [1], "status": "ok"}, sent, lambda: False)),
        base_url="http://gen"))
    s = Scheduler(lib, vocab, [client], StubDirector(), poll_s=0.01, backoff=(0.01,), not_ready_poll_s=0.01)
    await run_worker_until(s, client, sent)
    assert sent[0][1]["quality"] == 1


# M5: more like this

def test_more_beats_scene_fill_but_not_live():
    more = Counter({7: 2})
    kw = dict(scenes=[1], counts={1: 0, 7: 3}, fresh={7: 3})
    assert next_pool(pending=None, playing=None, scene_pools=[1], count=lambda p: kw["counts"].get(p, 0),
                     fresh=lambda p: 3, has_fav=lambda p: False, in_flight=Counter(), more=more) == 7
    assert next_pool(pending=9, playing=None, scene_pools=[1], count=lambda p: 0, fresh=lambda p: 3,
                     has_fav=lambda p: False, in_flight=Counter(), more=more) == 9


async def test_queue_more_runs_n_jobs_on_the_pool_prompt(env):
    lib, vocab, pool = env
    for i in range(3):
        lib.add_track(pool.id, lib.new_track_path(), 100.0, i, "g")   # scene is full: only `more` drives jobs
    director, client = StubDirector(), fake_client(31)
    s = Scheduler(lib, vocab, [client], director, poll_s=0.01)
    s.queue_more(pool.id, 2)
    assert await s.run_one(client) is True and s.more[pool.id] == 1
    assert await s.run_one(client) is True and s.more[pool.id] == 0
    assert await s.run_one(client) is False
    assert lib.track_count(pool.id) == 5
    seeds = [t.seed for t in lib.tracks(pool.id)[3:]]
    assert len(set(seeds)) == 2


# minutes-based warm-up: playing 10 min (floor 3), next queued 6 min (floor 3)

def secs(d):
    return lambda p: d.get(p, 0.0)


def warm(**kw):
    base = dict(pending=None, playing=1, scene_pools=[1, 2, 3], count=lambda p: 5, fresh=lambda p: 5,
                has_fav=lambda p: False, in_flight=Counter(), next_queued=2)
    base.update(kw)
    return next_pool(**base)


def test_next_scene_floor_beats_playing_target():
    assert warm(seconds=secs({1: 400, 2: 100})) == 2          # playing is above its floor, next is below 3 min


def test_playing_floor_comes_first():
    assert warm(seconds=secs({1: 100, 2: 0})) == 1


def test_playing_fills_to_ten_minutes_then_next_to_six():
    assert warm(seconds=secs({1: 400, 2: 200})) == 1
    assert warm(seconds=secs({1: 610, 2: 200})) == 2
    assert warm(seconds=secs({1: 610, 2: 370}), count=lambda p: 5) is None


def test_in_flight_jobs_count_as_expected_music():
    assert warm(seconds=secs({1: 610, 2: 100}), in_flight=Counter({2: 2})) is None
