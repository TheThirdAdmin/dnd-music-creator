import time

from ..library import Library

KEY = "runpod_ledger"
MAX_ROWS = 400


def estimate_cost(exec_ms: int, delay_ms: int, rate_per_hr: float, idle_s: float,
                  cold_threshold_ms: int = 5000) -> float:
    """Runpod bills worker time: execution, the idle tail, and the start-up when this job woke the worker.
    Cold-start billing is an assumption: the M8 spike billing rows were still empty at teardown
    (see docs/runpod.md), so this is an upper-bound estimate."""
    billed_s = exec_ms / 1000 + idle_s + (delay_ms / 1000 if delay_ms > cold_threshold_ms else 0)
    return billed_s * rate_per_hr / 3600


def _day_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _month_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))


class CostLedger:
    """Estimated Runpod cost per job, as [ts, cost] rows in library state (newest 400)."""

    def __init__(self, lib: Library):
        self.lib = lib
        self.started = time.time()

    def _rows(self) -> list[list[float]]:
        return self.lib.get_state(KEY, [])

    def add(self, job_id: str | None, cost: float, ts: float | None = None) -> None:
        rows = self._rows()
        rows.append([ts if ts is not None else time.time(), cost])
        self.lib.set_state(KEY, rows[-MAX_ROWS:])

    def last(self) -> float | None:
        rows = self._rows()
        return rows[-1][1] if rows else None

    def total(self, since_ts: float) -> float:
        return sum(c for t, c in self._rows() if t >= since_ts)

    def spent_today(self, now: float) -> float:
        return self.total(_day_start(now))

    @property
    def session_total(self) -> float:
        return self.total(self.started)

    def month_total(self, now: float) -> float:
        return self.total(_month_start(now))

    def avg_cost(self) -> float | None:
        rows = self._rows()
        return sum(c for _, c in rows) / len(rows) if rows else None
