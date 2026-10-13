from dndmusic.runpod.config import RunpodConfig
from dndmusic.runpod.guard import Guard


def guard(balance=10.0, spent=0.0, **kw):
    cfg = RunpodConfig("ep", "k", 0.5, **kw)
    return Guard(cfg, lambda: balance, lambda: spent)


def test_unknown_balance_is_fine():
    assert guard(balance=None).reason() is None


def test_below_floor():
    assert guard(balance=0.5).reason() == "Runpod balance is below your floor of $1.00"


def test_daily_cap():
    assert guard(spent=3.0, daily_cap=3.0).reason() == "Daily spend cap of $3.00 reached"
    assert guard(spent=2.9, daily_cap=3.0).reason() is None


def test_three_failures_trip_until_success_or_reset():
    g = guard()
    for _ in range(3):
        g.record(False)
    assert g.reason() == "3 jobs failed in a row; check the endpoint, then Reset in Settings"
    g.reset()
    assert g.reason() is None
    g.record(False)
    g.record(False)
    g.record(True)
    g.record(False)
    assert g.reason() is None
