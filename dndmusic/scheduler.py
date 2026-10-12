import asyncio
import logging
import random
import shutil
import time
from collections import Counter
from collections.abc import Callable

from .genclient import GeneratorClient, GeneratorError
from .ingest import RejectedAudio, ingest
from .library import Library
from .models import DEFAULT_QUALITY, Prompt
from .prompts import scene_prompt
from .vocab import Vocab

log = logging.getLogger(__name__)

FRESH_MIN = 3
SCENE_MIN = 3
FAV_TARGET = 8
# Minutes of ready music (seconds here) the DM should never run short of.
PLAYING_FLOOR_S, NEXT_FLOOR_S = 180.0, 180.0
PLAYING_TARGET_S, NEXT_TARGET_S = 600.0, 360.0
AVG_TRACK_S = 200.0     # what a job in flight is expected to add


def job_quality(wanted: int, offered: list[int] | None) -> int:
    """The quality to ask a generator for: the setting, or the best level below it the generator offers
    (or its lowest level if it offers nothing at or below the setting). Unknown capabilities: the setting."""
    if not offered:
        return wanted
    below = [q for q in offered if q <= wanted]
    return max(below) if below else min(offered)


def next_pool(*, pending: int | None, playing: int | None, scene_pools: list[int],
              count: Callable[[int], int], fresh: Callable[[int], int], has_fav: Callable[[int], bool],
              in_flight: Counter, more: Counter | None = None,
              seconds: Callable[[int], float] | None = None, next_queued: int | None = None) -> int | None:
    """The pool that most needs a new track, or None. With `seconds` (ready music per pool) the playing
    scene aims for 10 min and the next queued scene for 6 min, never below 3 min; without it, the old
    rule of FRESH_MIN fresh tracks for the playing scene."""
    if pending is not None and count(pending) + in_flight[pending] == 0:
        return pending
    if seconds is None:
        if playing is not None and fresh(playing) + in_flight[playing] < FRESH_MIN:
            return playing
    else:
        def have(p: int) -> float:
            return seconds(p) + in_flight[p] * AVG_TRACK_S
        steps = [(playing, PLAYING_FLOOR_S), (next_queued, NEXT_FLOOR_S),
                 (playing, PLAYING_TARGET_S), (next_queued, NEXT_TARGET_S)]
        for p, goal in steps:
            if p is not None and have(p) < goal:
                return p
    for p, n in (more or {}).items():
        if n > 0:
            return p
    for p in scene_pools:
        if count(p) + in_flight[p] < SCENE_MIN:
            return p
    for p in scene_pools:
        if has_fav(p) and count(p) + in_flight[p] < FAV_TARGET:
            return p
    return None


class Scheduler:
    """One worker per generator; each runs one job at a time for the neediest pool."""

    def __init__(self, lib: Library, vocab: Vocab, clients: list[GeneratorClient], director, *,
                 rng: random.Random | None = None, poll_s: float = 1.0,
                 backoff: tuple[int, ...] = (30, 60, 120, 300), min_free_bytes: int = 1 << 30,
                 disk_free: Callable[[], int] | None = None, job_timeout_s: float = 900, not_ready_poll_s: float = 10):
        self.lib = lib
        self.vocab = vocab
        self.clients = clients
        self.director = director
        self.rng = rng or random.Random()
        self.poll_s = poll_s
        self.job_timeout_s = job_timeout_s
        self.not_ready_poll_s = not_ready_poll_s
        self.backoff = backoff
        self.min_free_bytes = min_free_bytes
        self.disk_free = disk_free or (lambda: shutil.disk_usage(lib.data_dir).free)
        self.in_flight: Counter[int] = Counter()
        self.health = {c.base_url: {"ok": None, "busy": False, "error": None, "gpu": None, "qualities": None,
                                    "status": None} for c in clients}
        self.more: Counter[int] = Counter()   # 'more like this' jobs still wanted, per pool
        self.failures = 0
        self.on_added: Callable[[], None] | None = None
        self.disk_low = False
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    def _scene_pools(self) -> list[int]:
        return [self.lib.pool_for(scene_prompt(s, self.vocab), scene_id=s.id).id for s in self.lib.list_scenes()]

    def queue_more(self, pool_id: int, n: int) -> None:
        self.more[pool_id] += n
        self.wake()

    def queue_pools(self) -> list[int]:
        """Pool ids of the queued scenes, in order (the DM's queue is a list of scene ids in the state table)."""
        out = []
        for sid in self.lib.get_state("queue", []):
            scene = self.lib.get_scene(sid)
            if scene is not None and scene.saved:
                out.append(self.lib.pool_for(scene_prompt(scene, self.vocab), scene_id=scene.id).id)
        return out

    def next_queued_pool(self) -> int | None:
        pools = self.queue_pools()
        playing = self.director.playing_pool_id
        if pools and playing is not None and pools[0] == playing:
            pools = pools[1:]
        return pools[0] if pools else None

    def pick(self) -> int | None:
        return next_pool(seconds=self.lib.ready_seconds, next_queued=self.next_queued_pool(),
                         pending=self.director.pending_pool_id, playing=self.director.playing_pool_id,
                         scene_pools=self._scene_pools(), count=self.lib.track_count,
                         fresh=self.lib.fresh_count, has_fav=self.lib.has_fav, in_flight=self.in_flight,
                         more=self.more)

    @property
    def paused(self) -> bool:
        """Rendering stopped by the DM (persisted): no new jobs start; a running one finishes."""
        return bool(self.lib.get_state("paused", False))

    async def run_one(self, client: GeneratorClient) -> bool:
        if self.paused:
            return False
        self.disk_low = self.disk_free() < self.min_free_bytes
        if self.disk_low:
            return False
        pool_id = self.pick()
        if pool_id is None:
            return False
        pool = self.lib.get_pool(pool_id)
        lo, hi = self.vocab.duration_s
        quality = job_quality(self.lib.get_state("quality", DEFAULT_QUALITY),
                              self.health[client.base_url].get("qualities"))
        self.in_flight[pool_id] += 1
        if self.more[pool_id] > 0:
            self.more[pool_id] -= 1
        try:
            job_id = await client.submit(Prompt(pool.tags, pool.lyrics), self.rng.randint(lo, hi),
                                         self.rng.getrandbits(31), quality)
            deadline = time.monotonic() + self.job_timeout_s
            st = await client.status(job_id)
            while st.status in ("queued", "running"):
                if time.monotonic() >= deadline:
                    raise GeneratorError(f"job {job_id} timed out after {self.job_timeout_s:g} s")
                await asyncio.sleep(self.poll_s)
                st = await client.status(job_id)
            if st.status != "done":
                raise GeneratorError(f"job {st.status}: {st.error}")
            raw = await client.audio(job_id)
            path = self.lib.new_track_path()
            duration = await asyncio.to_thread(ingest, raw, path, straight=pool.straight)
            self.lib.add_track(pool_id, path, duration, st.seed, client.base_url, quality=quality)
        except RejectedAudio as e:
            self.failures += 1
            log.warning("pool %s: dropped generator output: %s", pool_id, e)
            return True
        finally:
            self.in_flight[pool_id] -= 1
        if self.on_added is not None:
            self.on_added()
        self.director.on_track_added(pool_id)
        return True

    async def worker(self, client: GeneratorClient) -> None:
        fails = 0
        await self.check_health(client)
        while True:
            if self.health[client.base_url]["ok"] is False:
                await asyncio.sleep(self.not_ready_poll_s)
                await self.check_health(client)
                continue
            try:
                ran = await self.run_one(client)
            except GeneratorError as e:
                self.health[client.base_url].update(ok=False, error=str(e))
                await asyncio.sleep(self.backoff[min(fails, len(self.backoff) - 1)])
                fails += 1
                await self.check_health(client)
                continue
            except Exception:
                log.exception("scheduler worker for %s", client.base_url)
                await asyncio.sleep(5)
                continue
            if ran:
                fails = 0
                self.health[client.base_url].update(ok=True, error=None)
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def check_health(self, client: GeneratorClient) -> None:
        try:
            h = await client.health()
        except GeneratorError as e:
            self.health[client.base_url].update(ok=False, error=str(e))
            return
        qualities = h.get("qualities")
        self.health[client.base_url].update(
            ok=bool(h.get("ok")), busy=bool(h.get("busy")), error=None,
            gpu=h.get("gpu") if isinstance(h.get("gpu"), bool) else None,
            qualities=[q for q in qualities if isinstance(q, int)] if isinstance(qualities, list) else None,
            status=str(h["status"]) if h.get("status") is not None else None)

    async def health_loop(self, client: GeneratorClient, interval_s: float = 30) -> None:
        while True:
            await self.check_health(client)
            await asyncio.sleep(interval_s)
