import shutil
import sqlite3
from pathlib import Path

import pytest

from dndmusic import library as libmod
from dndmusic.library import Library
from dndmusic.models import Prompt, Scene


def add(lib, pool_id, n=1):
    return [lib.add_track(pool_id, lib.new_track_path(), 120.0, i, "gen") for i in range(n)]


def test_scene_crud(lib):
    s = lib.create_scene(Scene(name="Tavern", location="tavern", mood="calm"))
    assert s.id is not None and s.tempo == "medium" and s.choir is False and s.start == "build"
    s.tempo, s.choir, s.start = "fast", True, "straight"
    lib.update_scene(s)
    got = lib.get_scene(s.id)
    assert (got.tempo, got.choir, got.start) == ("fast", True, "straight")
    assert [x.name for x in lib.list_scenes()] == ["Tavern"]
    lib.delete_scene(s.id)
    assert lib.get_scene(s.id) is None


def test_custom_scene_is_single_unsaved_row(lib):
    a = lib.custom_scene(Scene(name="Custom", location="crypt", mood="dread"))
    b = lib.custom_scene(Scene(name="Custom", location="forest road", mood="calm"))
    assert a.id == b.id
    assert lib.get_scene(a.id).location == "forest road"
    assert lib.get_scene(a.id).saved is False
    assert lib.list_scenes() == []


def test_pool_for_reuses_by_hash(lib):
    p1 = lib.pool_for(Prompt("a, b", "[instrumental]"), scene_id=1)
    p2 = lib.pool_for(Prompt("a, b", "[instrumental]"), scene_id=2)
    p3 = lib.pool_for(Prompt("a, c", "[instrumental]"))
    assert p1.id == p2.id != p3.id
    assert lib.get_pool(p1.id).tags == "a, b" and lib.get_pool(p1.id).scene_id == 1


def test_counts_and_recent(lib):
    pool = lib.pool_for(Prompt("x", ""))
    ids = [t.id for t in add(lib, pool.id, 4)]
    assert lib.track_count(pool.id) == 4 and lib.fresh_count(pool.id) == 4
    lib.record_play(ids[0], now=100.0)
    lib.record_play(ids[1], now=200.0)
    lib.record_play(ids[2], now=300.0)
    assert lib.recent_ids(pool.id) == [ids[2], ids[1]]
    assert lib.fresh_count(pool.id) == 2
    assert lib.get_track(ids[2]).plays == 1


def test_ratings_move_files_and_filter(lib):
    pool = lib.pool_for(Prompt("y", ""))
    a, b = add(lib, pool.id, 2)
    Path(b.path).write_bytes(b"audio")
    assert not lib.has_fav(pool.id)
    lib.set_rating(a.id, "fav")
    banned = lib.set_rating(b.id, "banned")
    assert lib.has_fav(pool.id)
    assert Path(banned.path).parent == lib.banned_dir and Path(banned.path).exists()
    assert [t.id for t in lib.tracks(pool.id)] == [a.id]
    assert lib.track_count(pool.id) == 1
    assert len(lib.tracks(pool.id, include_banned=True)) == 2
    restored = lib.set_rating(b.id, "normal")
    assert Path(restored.path).parent == lib.tracks_dir and Path(restored.path).exists()


def test_state_survives_reopen(tmp_path):
    Library(tmp_path).set_state("cast_targets", ["u1"])
    assert Library(tmp_path).get_state("cast_targets") == ["u1"]
    assert Library(tmp_path).get_state("missing", 5) == 5


def m1_db(data_dir, rows=()):
    """An M1-shaped database: the M1 schema, user_version 0, absolute track paths."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "dnd-music.db")
    db.executescript(libmod.SCHEMA_M1)
    db.execute("INSERT INTO pool (id, kind, tags, lyrics, prompt_hash) VALUES (1, 'scene', 't', 'l', 'h1')")
    for i, path in enumerate(rows, start=1):
        db.execute("INSERT INTO track (id, pool_id, path, duration_s, generator) VALUES (?, 1, ?, 60, 'g')", (i, path))
    db.commit()
    db.close()


def test_v1_makes_paths_relative_and_keeps_them_working_after_a_move(tmp_path):
    old = tmp_path / "old"
    m1_db(old, [str(old / "tracks" / "a.opus"), "/elsewhere/b.opus"])
    lib = Library(old)
    raw = lib._db.execute("SELECT path FROM track ORDER BY id").fetchall()
    assert [r[0] for r in raw] == ["tracks/a.opus", "/elsewhere/b.opus"]
    assert lib.get_track(1).path == str(old / "tracks" / "a.opus")
    assert lib._db.execute("PRAGMA user_version").fetchone()[0] == libmod.SCHEMA_VERSION
    lib._db.close()
    new = tmp_path / "new"
    shutil.copytree(old, new)
    assert Library(new).get_track(1).path == str(new / "tracks" / "a.opus")


def test_new_tracks_are_stored_relative(lib):
    pool = lib.pool_for(Prompt("z", ""))
    t = lib.add_track(pool.id, lib.new_track_path(), 60.0, 1, "g")
    stored = lib._db.execute("SELECT path FROM track WHERE id=?", (t.id,)).fetchone()[0]
    assert stored.startswith("tracks/") and t.path == str(lib.data_dir / stored)


def test_foreign_keys_are_enforced(lib):
    with pytest.raises(sqlite3.IntegrityError):
        lib.add_track(999, lib.new_track_path(), 60.0, 1, "g")


def test_migrations_run_once(tmp_path):
    Library(tmp_path)
    lib = Library(tmp_path)
    assert lib._db.execute("PRAGMA user_version").fetchone()[0] == libmod.SCHEMA_VERSION


def test_v2_maps_m1_scenes(tmp_path):
    m1_db(tmp_path)
    db = sqlite3.connect(tmp_path / "dnd-music.db")
    rows = [
        ("Fight", "High orchestral", '["tense", "dark"]', 4, "choir", "drums", 1),
        ("Inn", "Medieval tavern", '["playful"]', 2, "none", "", 1),
        ("Grave", "Dark ambient", '["eerie"]', 3, "chant", "", 1),
        ("Odd", "Polka", "[]", 5, "solo_vocalise", "", 0),
    ]
    db.executemany("INSERT INTO scene (name, genre, tones, intensity, vocals, extra, saved) VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    lib = Library(tmp_path)
    got = {s.name: s for s in [lib.get_scene(i) for i in range(1, 5)]}
    assert (got["Fight"].location, got["Fight"].mood, got["Fight"].tempo, got["Fight"].choir) == \
        ("castle hall", "tense", "fast", True)
    assert got["Fight"].start == "build" and got["Fight"].extra == "drums"
    assert (got["Inn"].location, got["Inn"].mood, got["Inn"].tempo, got["Inn"].choir) == ("tavern", "calm", "slow", False)
    assert (got["Grave"].location, got["Grave"].mood, got["Grave"].tempo) == ("crypt", "dread", "medium")
    assert (got["Odd"].location, got["Odd"].mood, got["Odd"].saved) == ("castle hall", "calm", False)
    assert lib.get_pool(1).straight is False
    t = lib.add_track(1, lib.new_track_path(), 60.0, 1, "g", quality=3)
    assert t.quality == 3


# M5: pinned pools

def _pool_with_scene(lib, name="Tavern"):
    s = lib.create_scene(Scene(name=name, location="tavern", mood="calm"))
    return s, lib.pool_for(Prompt("t", "l"), scene_id=s.id)


def test_fav_pins_pool_and_labels_it(lib):
    s, p = _pool_with_scene(lib)
    a, b = add(lib, p.id, 2)
    assert lib.get_pool(p.id).pinned is False
    lib.set_rating(a.id, "fav")
    got = lib.get_pool(p.id)
    assert got.pinned is True and got.label == "Tavern"
    lib.set_rating(b.id, "fav")
    lib.set_rating(a.id, "normal")
    assert lib.get_pool(p.id).pinned is True
    lib.set_rating(b.id, "normal")
    assert lib.get_pool(p.id).pinned is False


def test_pinned_pool_survives_scene_delete(lib):
    s, p = _pool_with_scene(lib)
    t = add(lib, p.id)[0]
    lib.set_rating(t.id, "fav")
    lib.delete_scene(s.id)
    (pool, tracks), = lib.favourites()
    assert pool.id == p.id and pool.label == "Tavern" and [x.id for x in tracks] == [t.id]
    with pytest.raises(ValueError):
        lib.delete_pool(p.id)


def test_delete_track_and_pool(lib, tmp_path):
    p = lib.pool_for(Prompt("t2", "l"))
    path = lib.new_track_path()
    path.write_bytes(b"x" * 10)
    t = lib.add_track(p.id, path, 100.0, 1, "g")
    assert lib.track_bytes(t) == 10
    lib.delete_track(t.id)
    assert lib.get_track(t.id) is None and not path.exists()
    lib.delete_track(t.id)  # idempotent
    lib.delete_pool(p.id)
    assert lib.get_pool(p.id) is None


def test_banned_tracks_listed(lib):
    p = lib.pool_for(Prompt("t3", "l"))
    t = add(lib, p.id)[0]
    lib.set_rating(t.id, "banned")
    assert [x.id for x in lib.banned_tracks()] == [t.id]


def test_migration_v3_backfills_pins(tmp_path):
    lib = Library(tmp_path)
    s, p = _pool_with_scene(lib, "Crypt")
    t = add(lib, p.id)[0]
    lib.set_rating(t.id, "fav")
    lib._db.close()
    db = sqlite3.connect(tmp_path / "dnd-music.db")
    for col, tbl in (("pinned", "pool"), ("label", "pool"), ("sha256", "track")):
        db.execute(f"ALTER TABLE {tbl} DROP COLUMN {col}")
    db.execute("ALTER TABLE scene DROP COLUMN prompt")   # added by v4
    db.execute("ALTER TABLE scene DROP COLUMN icon")     # added by v5
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()
    pool = Library(tmp_path).get_pool(p.id)
    assert pool.pinned is True and pool.label == "Crypt"


def test_snapshot_db_copies_and_prunes(lib, tmp_path):
    lib.create_scene(Scene(name="Keep", location="tavern", mood="calm"))
    first = lib.snapshot_db(keep=7)
    assert first.parent == lib.data_dir / "backup"
    copy = sqlite3.connect(first)
    assert copy.execute("SELECT name FROM scene").fetchall() == [("Keep",)]
    copy.close()
    for day in range(1, 9):
        (lib.data_dir / "backup" / f"dnd-music-2020010{day}.db").write_bytes(b"old")
    lib.snapshot_db(keep=7)
    assert len(list((lib.data_dir / "backup").glob("dnd-music-*.db"))) == 7
