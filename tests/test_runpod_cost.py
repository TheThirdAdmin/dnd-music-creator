import time

import pytest

from dndmusic.library import Library
from dndmusic.runpod.cost import CostLedger, estimate_cost


def test_warm_and_cold():
    assert estimate_cost(30000, 800, 0.58, 5) == pytest.approx(35 * 0.58 / 3600)
    assert estimate_cost(30000, 60000, 0.58, 5) == pytest.approx(95 * 0.58 / 3600)


def test_ledger(tmp_path):
    lib = Library(tmp_path)
    led = CostLedger(lib)
    assert led.last() is None and led.avg_cost() is None
    led.add("a", 0.5, ts=100)
    led.add("b", 0.25, ts=200)
    assert led.total(150) == 0.25 and led.total(0) == 0.75
    assert led.last() == 0.25 and led.avg_cost() == pytest.approx(0.375)
    assert CostLedger(Library(tmp_path)).total(0) == 0.75   # survives a reopen


def test_today_session_month(tmp_path):
    led = CostLedger(Library(tmp_path))
    now = time.time()
    led.add("a", 1.0, ts=now - 40 * 86400)
    led.add("b", 2.0, ts=now)
    assert led.spent_today(now) == 2.0 and led.session_total == 2.0 and led.month_total(now) == 2.0


def test_trim(tmp_path):
    led = CostLedger(Library(tmp_path))
    for i in range(405):
        led.add(str(i), float(i), ts=i)
    rows = led.lib.get_state("runpod_ledger")
    assert len(rows) == 400 and rows[0] == [5, 5.0]
