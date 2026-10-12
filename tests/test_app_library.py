import numpy as np
import pytest
from fastapi.testclient import TestClient

from dndmusic.app import build_services, create_app
from dndmusic.config import Settings
from dndmusic.mixer import RATE, ArraySource, Deck
from fakes import FakeBackend


@pytest.fixture
def client(tmp_path):
    settings = Settings(8000, "http://dm.test:8000", (), tmp_path)

    def factory(track):
        return Deck(ArraySource(np.zeros((RATE * 30, 2), np.float32)), track.id, track.pool_id, 30.0)

    services = build_services(settings, cast_backend=FakeBackend(), deck_factory=factory)
    with TestClient(create_app(lambda: services, start_background=False)) as c:
        c.svc = services
        yield c


def make_scene(client, name="Tavern", **kw):
    return client.post("/api/scenes", json={"name": name, "location": "tavern", "mood": "calm", **kw}).json()


def make_track(client, pool_id, size=1000, n=1):
    lib = client.svc.lib
    out = []
    for i in range(n):
        p = lib.new_track_path()
        p.write_bytes(b"x" * size)
        out.append(lib.add_track(pool_id, p, 30.0, i, "g"))
    return out


def test_rating_roundtrip_and_validation(client):
    s = make_scene(client)
    t = make_track(client, s["pool_id"])[0]
    assert client.put(f"/api/tracks/{t.id}/rating", json={"rating": "fav"}).json() == {"id": t.id, "rating": "fav"}
    assert client.get("/api/library").json()["favourites"][0]["label"] == "Tavern"
    assert client.put(f"/api/tracks/{t.id}/rating", json={"rating": "meh"}).status_code == 422
    assert client.put("/api/tracks/999/rating", json={"rating": "fav"}).status_code == 404


def test_library_payload_groups_and_survives_scene_delete(client):
    s = make_scene(client)
    t, u = make_track(client, s["pool_id"], n=2)
    client.put(f"/api/tracks/{t.id}/rating", json={"rating": "fav"})
    client.put(f"/api/tracks/{u.id}/rating", json={"rating": "banned"})
    client.delete(f"/api/scenes/{s['id']}")
    lib = client.get("/api/library").json()
    assert lib["used_bytes"] == 2000 and lib["track_count"] == 2 and lib["cap_bytes"] == 5 * 1024 ** 3
    (group,) = lib["favourites"]
    assert group["label"] == "Tavern" and group["pool_id"] == s["pool_id"] and group["tracks"][0]["id"] == t.id
    assert group["tags"] and [b["id"] for b in lib["banned"]] == [u.id]
    assert lib["data_path"]


def test_settings_cap_validation_and_eviction(client):
    s = make_scene(client)
    make_track(client, s["pool_id"], n=6)
    assert client.put("/api/settings", json={"library_cap_gb": 0}).status_code == 422
    assert client.put("/api/settings", json={"library_cap_gb": 51}).status_code == 422
    assert client.put("/api/settings", json={}).status_code == 422
    assert client.put("/api/settings", json={"library_cap_gb": 7}).json()["library_cap_gb"] == 7
    assert client.get("/api/settings").json()["library_cap_gb"] == 7
    client.svc.lib.set_state("library_cap_bytes", 3000)   # below the 1 GB minimum, tests only
    client.svc.enforce_cap()
    assert client.svc.lib.usage()[1] == 3
    assert client.svc.last_plan is not None


def test_audio_range_and_404(client):
    s = make_scene(client)
    t = make_track(client, s["pool_id"], size=100)[0]
    full = client.get(f"/api/tracks/{t.id}/audio")
    assert full.status_code == 200 and len(full.content) == 100
    part = client.get(f"/api/tracks/{t.id}/audio", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10
    assert client.get("/api/tracks/999/audio").status_code == 404
    import os
    os.unlink(t.path)
    assert client.get(f"/api/tracks/{t.id}/audio").status_code == 404


def test_apply_pool_pending_then_plays(client):
    s = make_scene(client)
    assert client.post("/api/apply", json={"pool_id": s["pool_id"], "fade_s": 0}).json() == {"switched": False, "pending": True}
    make_track(client, s["pool_id"])
    r = client.post("/api/apply", json={"pool_id": s["pool_id"], "fade_s": 0}).json()
    assert r["switched"] is True
    assert client.post("/api/apply", json={"pool_id": 999}).status_code == 404
    assert client.get("/api/state").json()["now_playing"]["rating"] == "normal"


def test_cleanup_drops_orphans_keeps_favs(client):
    a, b = make_scene(client, "A"), make_scene(client, "B", mood="tense")
    ta = make_track(client, a["pool_id"])[0]
    make_track(client, b["pool_id"])
    client.put(f"/api/tracks/{ta.id}/rating", json={"rating": "fav"})
    client.delete(f"/api/scenes/{a['id']}")
    client.delete(f"/api/scenes/{b['id']}")
    r = client.post("/api/library/cleanup").json()
    assert r["deleted_tracks"] == 1 and r["freed_bytes"] == 1000
    assert client.get("/api/library").json()["favourites"][0]["tracks"][0]["id"] == ta.id


def test_delete_only_banned(client):
    s = make_scene(client)
    t = make_track(client, s["pool_id"])[0]
    assert client.delete(f"/api/tracks/{t.id}").status_code == 409
    client.put(f"/api/tracks/{t.id}/rating", json={"rating": "banned"})
    assert client.delete(f"/api/tracks/{t.id}").status_code == 204
    assert client.delete(f"/api/tracks/{t.id}").status_code == 404


def test_more_like_this(client):
    s = make_scene(client)
    t = make_track(client, s["pool_id"])[0]
    assert client.post(f"/api/tracks/{t.id}/more", json={"count": 0}).status_code == 422
    assert client.post(f"/api/tracks/{t.id}/more", json={"count": 9}).status_code == 422
    assert client.post("/api/tracks/999/more", json={"count": 2}).status_code == 404
    assert client.post(f"/api/tracks/{t.id}/more", json={"count": 3}).json() == {"queued": 3, "pool_id": s["pool_id"]}
    assert client.svc.scheduler.more[s["pool_id"]] == 3


def test_more_works_after_scene_deleted_for_a_favourite(client):
    s = make_scene(client)
    t = make_track(client, s["pool_id"])[0]
    client.put(f"/api/tracks/{t.id}/rating", json={"rating": "fav"})
    client.delete(f"/api/scenes/{s['id']}")
    assert client.post(f"/api/tracks/{t.id}/more", json={"count": 1}).status_code == 200


def test_export_then_import_via_api(client, tmp_path):
    s = make_scene(client)
    from dndmusic.ingest import ingest
    from helpers import wav_bytes
    lib = client.svc.lib
    path = lib.new_track_path()
    ingest(wav_bytes(35), path)
    t = lib.add_track(s["pool_id"], path, 35.0, 1, "g")
    client.put(f"/api/tracks/{t.id}/rating", json={"rating": "fav"})
    r = client.get("/api/library/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and r.content[:2] == b"PK"
    assert list((client.svc.settings.data_dir / "tmp").iterdir()) == []
    # same library: file de-duplicates
    got = client.post("/api/library/import", content=r.content).json()
    assert got["added"] == 0 and got["duplicates"] == 1
    assert client.post("/api/library/import", content=b"junk").status_code == 422


def test_import_over_limit_is_413(client, monkeypatch):
    import dndmusic.app as appmod
    monkeypatch.setattr(appmod, "MAX_UPLOAD_BYTES", 10)
    assert client.post("/api/library/import", content=b"x" * 100).status_code == 413
    assert list((client.svc.settings.data_dir / "tmp").iterdir()) == []


# M6: player voting

def play_one(client):
    s = make_scene(client, "Vote")
    make_track(client, s["pool_id"], n=3)
    client.post("/api/apply", json={"scene_id": s["id"], "fade_s": 0})
    return client.get("/api/state").json()["now_playing"]["track_id"], s


def test_players_page_and_state_when_off(client):
    assert client.get("/players").status_code == 200 and "Players" in client.get("/players").text
    st = client.get("/api/players/state", params={"token": "aaaaaaaa"}).json()
    assert st["voting"] is False and st["players"] == 0
    r = client.post("/api/players/vote", json={"token": "aaaaaaaa", "track_id": 1, "vote": "like"})
    assert r.status_code == 409


def test_dm_sets_players_and_range(client):
    assert client.put("/api/players", json={"count": 4}).json()["players"] == 4
    assert client.get("/api/state").json()["votes"]["players"] == 4
    assert client.put("/api/players", json={"count": 21}).status_code == 422
    assert client.svc.lib.get_state("players") == 4


def test_majority_dislike_skips_once_and_resets(client):
    track_id, _ = play_one(client)
    client.put("/api/players", json={"count": 3})
    vote = lambda tok, v="dislike", tid=None: client.post(
        "/api/players/vote", json={"token": tok, "track_id": tid or track_id, "vote": v})
    assert vote("aaaaaaaa").json()["skipped"] is False
    assert vote("bbbbbbbb", "like").json()["likes"] == 1
    st = client.get("/api/players/state", params={"token": "aaaaaaaa"}).json()
    assert st["voting"] and st["track_id"] == track_id and st["my_vote"] == "dislike" and st["needed"] == 2
    r = vote("cccccccc").json()
    assert r["skipped"] is True
    new = client.get("/api/state").json()
    assert new["now_playing"]["track_id"] != track_id and new["votes"]["dislikes"] == 0   # fresh track, fresh votes
    assert vote("dddddddd").status_code == 409   # a late vote on the old track is stale, not a second skip


def test_vote_validation(client):
    track_id, _ = play_one(client)
    client.put("/api/players", json={"count": 2})
    bad = client.post("/api/players/vote", json={"token": "x", "track_id": track_id, "vote": "like"})
    assert bad.status_code == 422
    bad = client.post("/api/players/vote", json={"token": "aaaaaaaa", "track_id": track_id, "vote": "meh"})
    assert bad.status_code == 422
