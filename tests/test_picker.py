import random

from dndmusic.models import Track
from dndmusic.picker import pick_track


def t(i, rating="normal"):
    return Track(id=i, pool_id=1, path=f"/x/{i}", duration_s=100, seed=i, generator="g", rating=rating)


def test_empty_and_all_banned():
    assert pick_track([], []) is None
    assert pick_track([t(1, "banned")], []) is None


def test_avoids_recent():
    rng = random.Random(1)
    assert {pick_track([t(1), t(2), t(3)], [1, 2], rng).id for _ in range(50)} == {3}


def test_small_pool_avoids_only_the_last_play():
    rng = random.Random(1)
    assert {pick_track([t(1), t(2)], [1, 2], rng).id for _ in range(50)} == {2}


def test_single_track_repeats():
    assert pick_track([t(1)], [1]).id == 1


def test_never_banned():
    rng = random.Random(3)
    assert all(pick_track([t(1, "banned"), t(2)], [2], rng).id == 2 for _ in range(20))


def test_favourites_weighted_three_times():
    rng = random.Random(42)
    picks = [pick_track([t(1, "fav"), t(2)], [], rng).id for _ in range(4000)]
    assert 0.72 < picks.count(1) / 4000 < 0.78
