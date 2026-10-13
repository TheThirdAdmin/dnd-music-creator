from collections.abc import Callable

from .config import RunpodConfig


class Guard:
    """Why Runpod jobs should not be submitted right now (None: go ahead). Pure; the callables do the I/O."""

    def __init__(self, cfg: RunpodConfig, balance_fn: Callable[[], float | None],
                 spent_today_fn: Callable[[], float], max_failures: int = 3):
        self.cfg = cfg
        self.balance_fn = balance_fn
        self.spent_today_fn = spent_today_fn
        self.max_failures = max_failures
        self.fails = 0

    def record(self, ok: bool) -> None:
        self.fails = 0 if ok else self.fails + 1

    def reset(self) -> None:
        self.fails = 0

    def reason(self) -> str | None:
        if self.fails >= self.max_failures:
            return f"{self.fails} jobs failed in a row; check the endpoint, then Reset in Settings"
        balance = self.balance_fn()
        if balance is not None and balance < self.cfg.floor:
            return f"Runpod balance is below your floor of ${self.cfg.floor:.2f}"
        cap = self.cfg.daily_cap
        if cap is not None and self.spent_today_fn() >= cap:
            return f"Daily spend cap of ${cap:.2f} reached"
        return None
