import random

import numpy as np
import pytest

from dndmusic.director import Director
from dndmusic.library import Library
from dndmusic.mixer import RATE, ArraySource, Deck, Engine
from dndmusic.models import Prompt


def factory(track):
    return Deck(ArraySource(np.full((RATE * 20, 2), 0.1, np.float32)), track.id, track.pool_id, 20.0)


def add(lib, pool_id, n=1):
    return [lib.add_track(pool_id, lib.new_track_path(), 20.0, i, "g") for i in range(n)]


@pytest.fixture
def setup(lib):
    engine = Engine()
    return lib, engine, Director(lib, engine, deck_factory=factory, rng=random.Random(0))


def test_switch_to_ready_pool(setup):
    lib, engine, d = setup
    pool = lib.pool_for(Prompt("a", ""))
    add(lib, pool.id)
    assert d.switch_pool(pool.id, 2.0) is True
    assert engine.main.pool_id == pool.id and d.playing_pool_id == pool.id
    assert lib.get_state("playing_pool_id") == pool.id


def test_empty_pool_is_pending_until_a_track_lands(setup):
    lib, engine, d = setup
    a = lib.pool_for(Prompt("a", ""))
    add(lib, a.id)
    d.switch_pool(a.id, 0)
    b = lib.pool_for(Prompt("b", ""))
    assert d.switch_pool(b.id, 4.0) is False
    assert d.pending_pool_id == b.id and engine.main.pool_id == a.id
    d.on_track_added(a.id)
    assert d.pending_pool_id == b.id
    add(lib, b.id)
    d.on_track_added(b.id)
    assert d.pending_pool_id is None and engine.main.pool_id == b.id


def test_continues_with_another_track_of_the_same_pool(setup):
    lib, engine, d = setup
    pool = lib.pool_for(Prompt("a", ""))
    add(lib, pool.id, 2)
    d.switch_pool(pool.id, 0)
    first = engine.main.track_id
    for _ in range(130):
        engine.render(RATE // 10)
    assert engine.main.track_id != first and engine.main.pool_id == pool.id


def test_skip_and_fadeout(setup):
    lib, engine, d = setup
    pool = lib.pool_for(Prompt("a", ""))
    add(lib, pool.id, 2)
    d.switch_pool(pool.id, 0)
    first = engine.main.track_id
    d.skip()
    assert engine.main.track_id != first
    d.fadeout(1.0)
    assert engine.main is None and d.playing_pool_id is None
    for _ in range(12):
        engine.render(RATE // 10)
    assert engine.decks == []


def test_banning_the_playing_track_skips(setup):
    lib, engine, d = setup
    pool = lib.pool_for(Prompt("a", ""))
    add(lib, pool.id, 2)
    d.switch_pool(pool.id, 0)
    playing = engine.main.track_id
    d.rate(playing, "banned")
    assert engine.main.track_id != playing


def test_restore_resumes_last_pool(tmp_path):
    lib = Library(tmp_path)
    pool = lib.pool_for(Prompt("a", ""))
    add(lib, pool.id)
    Director(lib, Engine(), deck_factory=factory).switch_pool(pool.id, 0)
    d = Director(Library(tmp_path), Engine(), deck_factory=factory)
    d.restore()
    assert d.now_playing()["pool_id"] == pool.id


def test_skip_keeps_a_pending_apply(setup):
    lib, engine, d = setup
    a = lib.pool_for(Prompt("a", ""))
    add(lib, a.id, 2)
    d.switch_pool(a.id, 0)
    first = engine.main.track_id
    b = lib.pool_for(Prompt("b", ""))
    assert d.switch_pool(b.id, 4.0) is False
    d.skip()
    assert d.pending == (b.id, 4.0)
    assert engine.main.pool_id == a.id and engine.main.track_id != first
    add(lib, b.id)
    d.on_track_added(b.id)
    assert d.pending is None and engine.main.pool_id == b.id


def test_pool_dead_end_becomes_pending(setup):
    lib, engine, d = setup
    pool = lib.pool_for(Prompt("a", ""))
    (t,) = add(lib, pool.id)
    d.switch_pool(pool.id, 0)
    lib.set_rating(t.id, "banned")
    for _ in range(250):
        engine.render(RATE // 10)
    assert d.pending_pool_id == pool.id
    add(lib, pool.id)
    d.on_track_added(pool.id)
    assert engine.main is not None and engine.main.pool_id == pool.id
