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
    backend = FakeBackend()

    def factory(track):
        return Deck(ArraySource(np.zeros((RATE * 30, 2), np.float32)), track.id, track.pool_id, 30.0)

    services = build_services(settings, cast_backend=backend, deck_factory=factory)
    with TestClient(create_app(lambda: services, start_background=False)) as c:
        c.svc, c.backend = services, backend
        yield c


def test_vocab(client):
    v = client.get("/api/vocab").json()
    assert v["locations"][0] == "tavern" and "combat" in v["moods"]
    assert v["tempos"] == ["slow", "medium", "fast"] and v["starts"] == ["build", "straight"]


def test_scene_crud_and_validation(client):
    bad = client.post("/api/scenes", json={"name": "X", "location": "swamp", "mood": "calm"})
    assert bad.status_code == 422 and "location" in bad.json()["detail"]
    s = client.post("/api/scenes", json={"name": "Tavern", "location": "tavern", "mood": "calm"}).json()
    assert s["ready"] == 0 and s["pool_id"] and s["tempo"] == "medium" and s["choir"] is False
    updated = client.put(f"/api/scenes/{s['id']}", json={**s, "tempo": "fast"}).json()
    assert updated["tempo"] == "fast" and updated["pool_id"] != s["pool_id"]
    assert [x["name"] for x in client.get("/api/scenes").json()] == ["Tavern"]
    assert client.delete(f"/api/scenes/{s['id']}").status_code == 204
    assert client.get("/api/scenes").json() == []
    assert client.put("/api/scenes/999", json={"name": "x", "location": "town", "mood": "calm"}).status_code == 404


def test_apply_waits_for_a_track_then_plays(client):
    s = client.post("/api/scenes", json={"name": "Fight", "location": "battlefield", "mood": "combat"}).json()
    assert client.post("/api/apply", json={"scene_id": s["id"], "fade_s": 2}).json() == {"switched": False, "pending": True}
    assert client.get("/api/state").json()["pending"] == {"pool_id": s["pool_id"]}
    lib = client.svc.lib
    lib.add_track(s["pool_id"], lib.new_track_path(), 30.0, 1, "g")
    client.svc.director.on_track_added(s["pool_id"])
    state = client.get("/api/state").json()
    assert state["pending"] is None and state["now_playing"]["name"] == "Fight"
    assert state["now_playing"]["art"] == "combat"
    assert state["stream_url"] == "http://dm.test:8000/stream.mp3"


def test_apply_custom_and_errors(client):
    r = client.post("/api/apply", json={"scene": {"name": "Custom", "location": "crypt", "mood": "dread"}})
    assert r.json() == {"switched": False, "pending": True}
    assert client.get("/api/scenes").json() == []
    assert client.post("/api/apply", json={"fade_s": 1}).status_code == 422
    assert client.post("/api/apply", json={"scene_id": 999}).status_code == 404
    assert client.post("/api/apply", json={"scene_id": 1, "fade_s": 20}).status_code == 422


def test_fadeout_and_skip(client):
    assert client.post("/api/fadeout", json={"fade_s": 2}).json() == {"ok": True}
    assert client.post("/api/skip").json() == {"ok": True, "rendering": False}   # nothing playing


def _play(client):
    s = client.post("/api/scenes", json={"name": "Fight", "location": "battlefield", "mood": "combat"}).json()
    lib = client.svc.lib
    lib.add_track(s["pool_id"], lib.new_track_path(), 30.0, 1, "g")
    client.post("/api/apply", json={"scene_id": s["id"], "fade_s": 0})
    return s


def test_skip_queues_one_render_for_the_playing_pool(client):
    s = _play(client)
    sch = client.svc.scheduler
    assert client.svc.director.playing_pool_id == s["pool_id"]
    assert client.post("/api/skip").json() == {"ok": True, "rendering": True}
    assert sch.more[s["pool_id"]] == 1
    assert client.post("/api/skip").json() == {"ok": True, "rendering": False}   # pending: no stacking
    assert sch.more[s["pool_id"]] == 1
    sch.more[s["pool_id"]] = 0
    sch.in_flight[s["pool_id"]] = 1
    assert client.post("/api/skip").json()["rendering"] is False


def test_skip_never_bans_or_deletes_the_track(client):
    s = _play(client)
    lib = client.svc.lib
    before = lib.pool_tracks(s["pool_id"]) if hasattr(lib, "pool_tracks") else None
    tid = client.get("/api/state").json()["now_playing"]["track_id"]
    rating = lib.get_track(tid).rating
    client.post("/api/skip")
    t = lib.get_track(tid)
    assert t is not None and t.rating == rating and rating != "banned"
    assert before is None or len(lib.pool_tracks(s["pool_id"])) == len(before)


def test_stop_and_start_rendering(client):
    assert client.get("/api/state").json()["paused"] is False
    assert client.post("/api/stop", json={"fade_s": 0}).json() == {"paused": True}
    assert client.get("/api/state").json()["paused"] is True
    assert client.post("/api/start", json={"fade_s": 0}).json() == {"paused": False}
    assert client.get("/api/state").json()["paused"] is False


def test_advanced_prompt_scene(client):
    body = {"name": "Dragon hoard", "location": "tavern", "mood": "calm", "prompt": "  Slow  tuba\nsolo in a cave. Instrumental. "}
    r = client.post("/api/scenes", json=body)
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["prompt"] == "Slow  tuba\nsolo in a cave. Instrumental." and scene["art"] is None
    assert any(x["prompt"] == scene["prompt"] for x in client.get("/api/state").json()["scenes"])
    too_long = {**body, "name": "Long", "prompt": "word " * 71}
    assert client.post("/api/scenes", json=too_long).status_code == 422


def test_scene_icon_picks_art(client):
    body = {"name": "My chat", "location": "tavern", "mood": "calm", "prompt": "Soft lute. Instrumental.", "icon": "crypt"}
    r = client.post("/api/scenes", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["icon"] == "crypt" and r.json()["art"] == "crypt"
    assert client.post("/api/scenes", json={**body, "name": "Bad", "icon": "nope"}).status_code == 422
    plain = client.post("/api/scenes", json={**body, "name": "Plain", "icon": ""}).json()
    assert plain["art"] is None


def test_cast_endpoints(client):
    assert [d["name"] for d in client.get("/api/cast/devices").json()] == ["Lounge", "Downstairs"]
    snap = client.post("/api/cast", json={"targets": ["g1"]}).json()
    assert snap["casting"] is True
    assert ("play", "g1", "http://dm.test:8000/stream.mp3") in client.backend.calls
    assert client.post("/api/volume", json={"volume": 0.3}).json()["volume"] == 0.3
    assert client.post("/api/cast/stop").json()["casting"] is False


def test_stream_needs_background_pipeline(client):
    assert client.get("/stream.mp3").status_code == 503


def test_websocket_pushes_state(client):
    with client.websocket_connect("/ws") as ws:
        assert "scenes" in ws.receive_json()


def _bg_services(tmp_path):
    settings = Settings(8000, "http://dm.test:8000", (), tmp_path)
    return build_services(settings, cast_backend=FakeBackend())


def test_lifespan_background_starts_and_stops_pipeline(tmp_path):
    services = _bg_services(tmp_path)
    with TestClient(create_app(lambda: services, start_background=True)) as c:
        assert c.get("/api/state").status_code == 200
        assert services.pipeline is not None
        enc = services.pipeline._enc
    assert enc.returncode is not None


def test_lifespan_cleans_up_when_startup_fails(tmp_path, monkeypatch):
    services = _bg_services(tmp_path)

    def boom():
        raise RuntimeError("restore failed")

    monkeypatch.setattr(services.director, "restore", boom)
    with pytest.raises(RuntimeError):
        with TestClient(create_app(lambda: services, start_background=True)):
            pass
    assert services.pipeline is not None
    assert services.pipeline._enc.returncode is not None


def test_name_follows_what_was_applied_for_shared_pools(client):
    fields = {"location": "castle hall", "mood": "tense"}
    lib = client.svc.lib
    client.post("/api/apply", json={"scene": {"name": "x", **fields}, "fade_s": 0})
    s = client.post("/api/scenes", json={"name": "Tavern", **fields}).json()
    lib.add_track(s["pool_id"], lib.new_track_path(), 30.0, 1, "g")
    assert client.post("/api/apply", json={"scene_id": s["id"], "fade_s": 0}).json()["switched"] is True
    assert client.get("/api/state").json()["now_playing"]["name"] == "Tavern"


def test_quality_setting(client):
    assert client.get("/api/settings").json() == {"quality": 2, "library_cap_gb": 5}
    assert client.get("/api/state").json()["quality"] == 2
    assert client.put("/api/settings", json={"quality": 3}).json() == {"quality": 3, "library_cap_gb": 5}
    assert client.get("/api/state").json()["quality"] == 3
    assert client.put("/api/settings", json={"quality": 4}).status_code == 422
    assert client.svc.lib.get_state("quality") == 3


def test_build_services_adds_the_runpod_client_from_env(tmp_path, monkeypatch):
    from dndmusic.runpod.client import RunpodClient
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "ep9")
    svc = build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=FakeBackend())
    [c] = svc.scheduler.clients
    assert isinstance(c, RunpodClient) and c.base_url == "runpod:ep9"
    assert "runpod:ep9" in svc.scheduler.gates and svc.runpod is not None


def test_build_services_without_runpod_config_has_no_runpod_client(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_ENDPOINT_ID", raising=False)
    svc = build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=FakeBackend())
    assert svc.scheduler.clients == [] and svc.scheduler.gates == {}


def test_build_services_refuses_a_scene_the_vocab_lacks(tmp_path):
    from dndmusic.library import Library
    from dndmusic.models import Scene
    from dndmusic.vocab import VocabError
    Library(tmp_path).create_scene(Scene(name="Grave", location="graveyard", mood="calm"))
    with pytest.raises(VocabError, match="scene 'Grave' uses location 'graveyard'"):
        build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=FakeBackend())


def test_build_services_drops_an_invalid_custom_scene(tmp_path):
    from dndmusic.library import Library
    from dndmusic.models import Scene
    lib = Library(tmp_path)
    lib.custom_scene(Scene(name="Custom", location="graveyard", mood="calm"))
    svc = build_services(Settings(8000, "http://dm.test:8000", (), tmp_path), cast_backend=FakeBackend())
    assert svc.lib._one("SELECT id FROM scene WHERE saved=0") is None


def test_migration_targets_exist_in_the_default_vocab(tmp_path):
    from dndmusic.library import GENRE_TO_LOCATION, TONE_TO_MOOD
    from dndmusic.vocab import load_vocab
    v = load_vocab(tmp_path)
    for loc in (*GENRE_TO_LOCATION.values(), "castle hall"):
        assert loc in v.locations, loc
    for mood in (*TONE_TO_MOOD.values(), "calm"):
        assert mood in v.moods, mood


def test_art_in_scene_out_and_now_playing(client):
    s = client.post("/api/scenes", json={"name": "Dockside", "location": "at sea", "mood": "calm"}).json()
    assert s["art"] == "at-sea"
    assert client.post("/api/scenes", json={"name": "Odd", "location": "bardcore", "mood": "sad"}).json()["art"] == "bardcore"
    assert client.get("/api/state").json()["scenes"][0]["art"] == "at-sea"


def test_scene_from_text_returns_unsaved_draft(client):
    v = client.get("/api/vocab").json()
    before = len(client.get("/api/scenes").json())
    r = client.post("/api/scene-from-text", json={"text": "ambush in a dark forest"})
    assert r.status_code == 200
    d = r.json()
    assert d["id"] is None and d["saved"] is False
    assert d["location"] in v["locations"] and d["mood"] in v["moods"]
    assert "pool_id" not in d and "ready" not in d and "art" in d
    assert len(client.get("/api/scenes").json()) == before
    body = {k: d[k] for k in ("name", "location", "mood", "tempo", "choir", "start", "extra")}
    c = client.post("/api/scenes", json=body)
    assert c.status_code == 201
    assert {k: c.json()[k] for k in body} == body


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_scene_from_text_rejects_empty(client, text):
    assert client.post("/api/scene-from-text", json={"text": text}).status_code == 422


def test_scene_from_text_rejects_long_text(client):
    assert client.post("/api/scene-from-text", json={"text": "a" * 301}).status_code == 422
    assert client.post("/api/scene-from-text", json={"text": "a" * 300}).status_code == 200


def test_scene_from_text_stores_and_wakes_nothing(client):
    svc = client.app.state.svc
    n = lambda: svc.lib._db.execute("SELECT COUNT(*) FROM pool").fetchone()[0]
    before = n()
    svc.scheduler._wake.clear()
    client.post("/api/scene-from-text", json={"text": "a quiet tavern"})
    assert n() == before
    assert not svc.scheduler._wake.is_set()


def test_build_services_loads_rules_and_refuses_a_bad_file(tmp_path):
    from dndmusic.vocab import VocabError
    settings = Settings(8000, "http://dm.test:8000", (), tmp_path)
    assert build_services(settings, cast_backend=FakeBackend()).rules is not None
    (tmp_path / "freetext.yaml").write_text("- nope\n")
    with pytest.raises(VocabError):
        build_services(settings, cast_backend=FakeBackend())


def _scene(client, name, location, mood, tracks=1):
    s = client.post("/api/scenes", json={"name": name, "location": location, "mood": mood}).json()
    lib = client.svc.lib
    for i in range(tracks):
        lib.add_track(s["pool_id"], lib.new_track_path(), 30.0, i, "g")
    return s


def test_queue_next_drops_the_scene_that_was_playing(client):
    a, b, c = (_scene(client, n, loc, m) for n, loc, m in
               [("A", "tavern", "calm"), ("B", "battlefield", "combat"), ("C", "crypt", "dread")])
    assert client.put("/api/queue", json={"ids": [a["id"], b["id"], c["id"]]}).json() == {"ids": [a["id"], b["id"], c["id"]]}
    assert client.get("/api/state").json()["now_playing"] is None   # queueing never starts anything
    assert client.post("/api/queue/next", json={"fade_s": 1}).json()["switched"] is True
    st = client.get("/api/state").json()
    assert st["now_playing"]["name"] == "A" and [q["name"] for q in st["queue"]] == ["A", "B", "C"]
    client.post("/api/queue/next", json={"fade_s": 1})
    st = client.get("/api/state").json()
    assert st["now_playing"]["name"] == "B" and [q["name"] for q in st["queue"]] == ["B", "C"]


def test_queue_next_waits_for_an_empty_pool_and_keeps_the_playing_card(client):
    a = _scene(client, "A", "tavern", "calm")
    b = _scene(client, "B", "battlefield", "combat", tracks=0)
    client.put("/api/queue", json={"ids": [a["id"], b["id"]]})
    client.post("/api/queue/next", json={"fade_s": 1})
    r = client.post("/api/queue/next", json={"fade_s": 1}).json()
    assert r == {"switched": False, "pending": True}
    st = client.get("/api/state").json()
    assert st["now_playing"]["name"] == "A" and [q["name"] for q in st["queue"]] == ["A", "B"]
    lib = client.svc.lib
    lib.add_track(b["pool_id"], lib.new_track_path(), 30.0, 1, "g")
    client.svc.director.on_track_added(b["pool_id"])
    st = client.get("/api/state").json()
    assert st["now_playing"]["name"] == "B" and [q["name"] for q in st["queue"]] == ["B"]


def test_queue_errors_and_deleted_scenes(client):
    assert client.post("/api/queue/next", json={}).status_code == 409
    assert client.put("/api/queue", json={"ids": [999]}).status_code == 404
    a = _scene(client, "A", "tavern", "calm")
    b = _scene(client, "B", "battlefield", "combat")
    client.put("/api/queue", json={"ids": [a["id"], b["id"]]})
    client.post("/api/queue/next", json={})
    client.delete(f"/api/scenes/{b['id']}")
    assert [q["name"] for q in client.get("/api/state").json()["queue"]] == ["A"]
    assert client.post("/api/queue/next", json={}).status_code == 409   # nothing after the playing scene


@pytest.fixture
def locked(tmp_path):
    settings = Settings(8000, "http://dm.test:8000", (), tmp_path, dm_password=True)
    services = build_services(settings, cast_backend=FakeBackend(), deck_factory=None)
    with TestClient(create_app(lambda: services, start_background=False), follow_redirects=False) as c:
        c.password = (tmp_path / "dm-password").read_text().strip()
        yield c


def test_dm_password_gates_the_dm_pages_only(locked):
    import re
    assert re.fullmatch(r"([A-Z][a-z]+){3}", locked.password)
    assert locked.get("/").headers["location"] == "/login"
    assert locked.get("/api/state").status_code == 401
    assert locked.post("/api/queue/next", json={}).status_code == 401
    for open_path in ("/login", "/players", "/api/players/state", "/static/style.css"):
        assert locked.get(open_path).status_code == 200, open_path
    with pytest.raises(Exception):
        with locked.websocket_connect("/ws"):
            pass


def test_dm_password_login(locked):
    assert locked.post("/api/login", json={"password": "wrong"}).status_code == 401
    r = locked.post("/api/login", json={"password": locked.password})
    assert r.status_code == 200
    assert locked.get("/").status_code == 200 and locked.get("/api/state").status_code == 200
    with locked.websocket_connect("/ws") as ws:
        assert "queue" in ws.receive_json()


def test_dm_password_persists_and_can_be_off(tmp_path, client):
    from dndmusic.auth import load_password
    assert load_password(tmp_path) == load_password(tmp_path)
    assert client.get("/api/state").status_code == 200       # default test settings: off
    from dndmusic.config import Settings as S
    assert S.from_env({"DM_PASSWORD": "off"}).dm_password is False and S.from_env({}).dm_password is True


def test_status_rows_for_playing_and_next(client):
    a = _scene(client, "A", "tavern", "calm")      # one 30 s track
    b = _scene(client, "B", "battlefield", "combat", tracks=0)
    client.put("/api/queue", json={"ids": [a["id"], b["id"]]})
    client.post("/api/queue/next", json={})
    rows = {r["name"]: r for r in client.get("/api/state").json()["status"]}
    assert rows["A"]["role"] == "playing" and rows["A"]["ready_s"] == 30 and rows["A"]["target_s"] == 600
    assert rows["B"]["role"] == "next" and rows["B"]["tracks"] == 0 and rows["B"]["target_s"] == 360


def test_logout_clears_the_session(locked):
    assert locked.post("/api/login", json={"password": locked.password}).status_code == 200
    assert locked.get("/api/state").status_code == 200
    r = locked.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert locked.get("/api/state").status_code == 401
