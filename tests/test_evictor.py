import pytest

from dndmusic.evictor import Item, plan
from dndmusic.models import Prompt

MB = 1 << 20


def item(i, rating="normal", played=None, pool=1, size=MB):
    return Item(i, pool, rating, size, played)


def test_order_banned_then_played_then_never_played():
    items = [item(1, played=50.0), item(2, played=10.0), item(3), item(4, "banned"), item(5, "banned"), item(6)]
    p = plan(items, 0, set())
    assert p.delete_ids == [4, 5, 2, 1, 3, 6] and p.over_cap is False


def test_stops_at_cap():
    items = [item(i, played=float(i)) for i in range(1, 6)]
    assert plan(items, 3 * MB, set()).delete_ids == [1, 2]


def test_favourites_and_protected_kept_and_over_cap_reported():
    items = [item(1, "fav"), item(2, "fav"), item(3, played=1.0), item(4, "banned")]
    p = plan(items, 0, {3})
    assert p.delete_ids == [4] and p.over_cap is True


def test_nothing_when_under_cap():
    assert plan([item(1), item(2)], 10 * MB, set()) == plan([], 0, set())


def fill(lib, pool_id, n, size=1000):
    out = []
    for i in range(n):
        path = lib.new_track_path()
        path.write_bytes(b"x" * size)
        out.append(lib.add_track(pool_id, path, 100.0, i, "g"))
    return out


def test_library_usage_and_cap_state(lib):
    p = lib.pool_for(Prompt("a", "b"))
    fill(lib, p.id, 3)
    assert lib.usage() == (3000, 3)
    assert lib.cap_bytes() == 5 * 1024 ** 3
    lib.set_cap_gb(12)
    assert lib.cap_bytes() == 12 * 1024 ** 3
    for bad in (0, 51):
        with pytest.raises(ValueError):
            lib.set_cap_gb(bad)


def test_evict_protects_last_two_per_pool_playing_and_favs(lib):
    p = lib.pool_for(Prompt("a", "b"))
    t = fill(lib, p.id, 6)
    for n, x in enumerate(t[:3]):
        lib.record_play(x.id, now=100.0 + n)       # t0 oldest played
    lib.set_rating(t[5].id, "fav")
    plan_ = lib.evict(1500, playing_track_id=t[0].id)
    left = {x.id for x in lib.tracks(p.id, include_banned=True)}
    # protected: playing t0, last 2 plays (t1, t2), last 2 by id (t4, t5=fav); t3 is the only candidate
    assert left == {t[0].id, t[1].id, t[2].id, t[4].id, t[5].id}
    assert plan_.over_cap is True and lib.usage()[1] == 5


def test_evict_drops_banned_first(lib):
    p = lib.pool_for(Prompt("a", "b"))
    t = fill(lib, p.id, 5)
    lib.set_rating(t[0].id, "banned")
    lib.evict(4000)
    assert lib.get_track(t[0].id) is None and lib.usage()[1] == 4


def test_size_cache_does_not_restat(lib, monkeypatch):
    import os
    p = lib.pool_for(Prompt("a", "b"))
    t = fill(lib, p.id, 3)
    lib.usage()
    calls = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda *a, **k: calls.append(a) or real(*a, **k))
    lib.usage()
    lib.delete_track(t[0].id)
    assert lib.usage() == (2000, 2)
    assert len([c for c in calls if str(c[0]).endswith(".opus")]) <= 1   # only the deleted file's own stat


def test_cleanup_unreachable(lib):
    keep = lib.pool_for(Prompt("keep", "x"))
    used = lib.pool_for(Prompt("used", "x"))
    orphan = lib.pool_for(Prompt("orphan", "x"))
    fill(lib, keep.id, 1)
    fill(lib, used.id, 1)
    o = fill(lib, orphan.id, 2)
    lib.set_rating(lib.tracks(keep.id)[0].id, "fav")
    freed = lib.cleanup_unreachable({used.prompt_hash})
    assert sorted(freed) == sorted(x.id for x in o)
    assert lib.get_pool(orphan.id) is None and lib.get_pool(keep.id) and lib.get_pool(used.id)
