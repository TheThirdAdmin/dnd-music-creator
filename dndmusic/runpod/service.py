import asyncio
import logging
import statistics
import time
from collections.abc import Callable

from ..library import Library
from .balance import Balance, BalanceReader
from .client import RunpodClient
from .config import RunpodConfig
from .cost import CostLedger, estimate_cost
from .guard import Guard

log = logging.getLogger(__name__)
COLD_KEY = "runpod_cold_starts"
BUDGET_KEY = "runpod_budget_since"
LAST_RUN_KEY = "runpod_last_run"
COLD_THRESHOLD_MS = 5000


def _mss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}m{s % 60:02d}s"


class RunpodService:
    def __init__(self, cfg: RunpodConfig, client: RunpodClient, guard: Guard, ledger: CostLedger,
                 balance_reader: BalanceReader, lib: Library | None = None,
                 health_fn: Callable[[], dict | None] | None = None):
        self.cfg, self.client, self.guard = cfg, client, guard
        self.ledger, self.reader = ledger, balance_reader
        self.lib = lib or ledger.lib
        self.health_fn = health_fn
        self.last = Balance()
        self.cold_starts: list[float] = list(self.lib.get_state(COLD_KEY, []))

    # balance

    def _balance(self) -> tuple[float | None, str]:
        if self.last.usd is not None:
            return self.last.usd, "runpod"
        if self.cfg.budget_usd is not None:
            since = self.lib.get_state(BUDGET_KEY, 0.0)
            return self.cfg.budget_usd - self.ledger.total(since), "budget"
        return None, "unknown"

    @property
    def balance_usd(self) -> float | None:
        return self._balance()[0]

    async def balance_loop(self, interval_s: float = 60) -> None:
        while True:
            try:
                self.last = await self.reader.read()
            except Exception:   # never kill the loop
                log.warning("Runpod balance read failed")
            await asyncio.sleep(interval_s)

    # cost

    def on_job_done(self, job_id: str | None) -> None:
        meta = self.client.last_run.pop(job_id, None) if job_id is not None else None
        if job_id is not None:
            self.client.inflight.pop(job_id, None)
        if meta is None:
            return
        cost = estimate_cost(meta.exec_ms, meta.delay_ms, self.cfg.rate_per_hr, self.cfg.idle_timeout_s,
                             COLD_THRESHOLD_MS)
        self.ledger.add(job_id, cost)
        self.lib.set_state(LAST_RUN_KEY, {"exec_s": meta.exec_ms / 1000, "delay_s": meta.delay_ms / 1000})
        if meta.delay_ms > COLD_THRESHOLD_MS:
            self.cold_starts = (self.cold_starts + [meta.delay_ms / 1000])[-20:]
            self.lib.set_state(COLD_KEY, self.cold_starts)

    def cold_start_s(self) -> float | None:
        return statistics.median(self.cold_starts) if self.cold_starts else None

    # activity line

    def activity(self) -> dict | None:
        """What Runpod is doing for the job in flight: {phase, since_s, detail}; None when no job is in flight."""
        jobs = self.client.inflight
        if not jobs:
            return None
        now = time.monotonic()
        h = self.health_fn() if self.health_fn else None
        h = h or {}
        running = [j for j in jobs.values() if j["state"] == "running"]
        oldest = min(jobs.values(), key=lambda j: j["t0"])
        if h.get("ok") is False:
            return {"phase": "unreachable", "since_s": round(now - oldest["t0"]),
                    "detail": "Can't read Runpod health right now"}
        w = h.get("workers") or {}
        j = h.get("jobs") or {}
        queued = int(j["inQueue"]) if "inQueue" in j else sum(1 for x in jobs.values() if x["state"] == "queued")
        more = max(0, queued - (0 if running else 1))   # our own job is the one in the queue unless it runs
        extra = f" · +{more} more queued" if more > 0 else ""
        if running:
            since = now - min(x["t_state"] for x in running)
            phase, text = "rendering", f"Rendering · {_mss(since)}"
            if since > 300:
                first = not self.cold_starts and self.ledger.last() is None
                text += " · longer than usual" + (": first run downloads the models" if first else "")
            else:
                text += " (a track normally takes 20-80 s after the worker is up)"
            return {"phase": phase, "since_s": round(since), "detail": text + extra}
        since = now - oldest["t0"]
        starting, throttled = int(w.get("initializing", 0)), int(w.get("throttled", 0))
        up = int(w.get("idle", 0)) + int(w.get("ready", 0)) + int(w.get("running", 0))
        if up > 0:
            phase, text = "worker_ready", f"Worker ready, starting the job · {_mss(since)}"
        elif starting == 0 and throttled > 0:
            phase, text = "throttled", (f"No GPU available right now ({throttled} throttled) · {_mss(since)}. "
                                        "Runpod is waiting for capacity.")
        else:
            n = f" ({starting} starting)" if starting > 0 else ""
            phase, text = "starting", (f"Waiting for a worker to start{n} · {_mss(since)}. A start after idle takes "
                                       "1-3 min; the first ever start on a new endpoint 5-10 min "
                                       "(image pull, model download).")
        return {"phase": phase, "since_s": round(since), "detail": text + extra}

    # snapshot

    def snapshot(self) -> dict:
        now = time.time()
        usd, source = self._balance()
        avg = self.ledger.avg_cost()
        h = self.health_fn() if self.health_fn else None
        blocked = self.guard.reason()
        if blocked:
            state = "blocked"
        elif h and h.get("ok") is False:
            state = "error"
        elif h and h.get("status") in ("warming up", "ready", "idle"):
            state = h["status"]
        else:
            state = "idle"
        return {
            "configured": True, "endpoint_id": self.cfg.endpoint_id, "state": state,
            "warming": bool(h and h.get("warming")), "cold_start_s": self.cold_start_s(),
            "cost": {"last_track": self.ledger.last(), "session": self.ledger.session_total,
                     "today": self.ledger.spent_today(now), "month": self.ledger.month_total(now),
                     "avg_track": avg, "estimated": True,
                     "last_run": self.lib.get_state(LAST_RUN_KEY, None)},
            "balance": {"usd": usd, "source": source,
                        "tracks_left": int(usd / avg) if usd is not None and avg else None,
                        "low": usd is not None and usd < self.cfg.warn_below,
                        "error": self.last.error},
            "activity": self.activity(),
            "blocked": blocked, "rate_per_hr": self.cfg.rate_per_hr,
        }
