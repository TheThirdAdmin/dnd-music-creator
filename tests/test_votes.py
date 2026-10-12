import pytest

from dndmusic.votes import Votes, VoteError


def test_majority_needs_more_than_half():
    v = Votes()
    for players, dislikes, skip in [(1, 1, True), (2, 1, False), (2, 2, True), (3, 1, False), (3, 2, True),
                                    (4, 2, False), (4, 3, True)]:
        v = Votes()
        v.players = players
        v.sync(1)
        for i in range(dislikes):
            v.cast(1, f"token-{i:04d}", "dislike")
        assert v.skip_due(1) is skip, (players, dislikes)


def test_votes_change_clear_and_count():
    v = Votes()
    v.players = 3
    v.sync(7)
    v.cast(7, "aaaaaaaa", "like")
    v.cast(7, "bbbbbbbb", "dislike")
    assert v.tally(7) == {"likes": 1, "dislikes": 1, "needed": 2, "players": 3}
    v.cast(7, "aaaaaaaa", "dislike")
    assert v.tally(7)["dislikes"] == 2 and v.skip_due(7)
    v.cast(7, "aaaaaaaa", "none")
    assert v.tally(7)["dislikes"] == 1 and v.mine("aaaaaaaa") is None and v.mine("bbbbbbbb") == "dislike"


def test_track_change_resets():
    v = Votes()
    v.players = 2
    v.sync(1)
    v.cast(1, "aaaaaaaa", "dislike")
    v.sync(2)
    assert v.tally(2)["dislikes"] == 0
    with pytest.raises(VoteError, match="stale"):
        v.cast(1, "aaaaaaaa", "like")


def test_validation_and_off():
    v = Votes()
    v.sync(1)
    with pytest.raises(VoteError, match="off"):
        v.cast(1, "aaaaaaaa", "like")
    v.players = 2
    for bad in ("short", "has space!!", "x" * 65):
        with pytest.raises(VoteError, match="token"):
            v.cast(1, bad, "like")
    with pytest.raises(VoteError, match="vote"):
        v.cast(1, "aaaaaaaa", "maybe")
    assert v.skip_due(1) is False
    v.players = 0
    assert v.skip_due(1) is False


def test_set_players_range():
    v = Votes()
    for n in (-1, 21):
        with pytest.raises(ValueError):
            v.set_players(n)
    v.set_players(20)
    assert v.players == 20


def test_skip_issued_once_per_track():
    v = Votes(players=1)
    v.sync(5)
    v.cast(5, "aaaaaaaa", "dislike")
    assert v.skip_due(5)
    v.skipped_id = 5
    assert not v.skip_due(5)
