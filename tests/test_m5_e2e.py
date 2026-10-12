"""M5: favourite -> scene deleted -> still listed and playable -> more like this -> export -> import elsewhere.
Fake generator, no GPU."""
import time

import httpx
import pytest

from dndmusic.app import build_services, create_app
from dndmusic.config import Settings
from dndmusic.library import Library
from dndmusic.bundle import import_zip
from fakegen.app import create_app as fake_app
from fakes import FakeBackend
from test_e2e import free_port, serve
from test_freetext_e2e import recording

pytestmark = pytest.mark.e2e


@pytest.fixture
def stack(tmp_path):
    seen = []
    gport, cport = free_port(), free_port()
    gen = serve(recording(fake_app(delay_s=0.05, max_duration_s=31), seen), gport)
    settings = Settings(cport, f"http://127.0.0.1:{cport}", (f"http://127.0.0.1:{gport}",), tmp_path)
    svc = build_services(settings, cast_backend=FakeBackend())
    con = serve(create_app(lambda: svc), cport)
    try:
        yield f"http://127.0.0.1:{cport}", svc, seen
    finally:
        con.should_exit = True
        gen.should_exit = True


def wait(cond, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.3)
    raise AssertionError("timed out")


def test_favourite_lifecycle(stack, tmp_path):
    base, svc, seen = stack
    s = httpx.post(f"{base}/api/scenes", json={"name": "Tavern", "location": "tavern", "mood": "calm"}).json()
    wait(lambda: svc.lib.track_count(s["pool_id"]) >= 1)
    track = svc.lib.tracks(s["pool_id"])[0]
    assert httpx.put(f"{base}/api/tracks/{track.id}/rating", json={"rating": "fav"}).status_code == 200

    httpx.delete(f"{base}/api/scenes/{s['id']}")
    lib = httpx.get(f"{base}/api/library").json()
    (group,) = lib["favourites"]
    assert group["label"] == "Tavern" and group["pool_id"] == s["pool_id"]

    before = svc.lib.track_count(s["pool_id"])
    seen.clear()
    assert httpx.post(f"{base}/api/tracks/{track.id}/more", json={"count": 2}).status_code == 200
    wait(lambda: svc.lib.track_count(s["pool_id"]) >= before + 2)
    assert {j["tags"] for j in seen} == {group["tags"]}
    assert len({t.seed for t in svc.lib.tracks(s["pool_id"])}) >= 3

    r = httpx.post(f"{base}/api/apply", json={"pool_id": s["pool_id"], "fade_s": 0}).json()
    assert r["switched"] is True

    bundle = httpx.get(f"{base}/api/library/export")
    zpath = tmp_path / "fav.zip"
    zpath.write_bytes(bundle.content)
    fresh = Library(tmp_path / "fresh")
    res = import_zip(fresh, zpath)
    assert res["added"] == 1 and res["rejected"] == []
    (pool, tracks), = fresh.favourites()
    assert pool.tags == group["tags"] and pool.label == "Tavern" and tracks[0].rating == "fav"
