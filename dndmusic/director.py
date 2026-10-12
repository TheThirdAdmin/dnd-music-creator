import logging
import random
from collections.abc import Callable

from .library import Library
from .mixer import NEAR_END_S, RATE, Deck, Engine, FfmpegSource
from .models import Scene, Track
from .picker import pick_track
from .prompts import scene_prompt
from .vocab import Vocab

SKIP_FADE_S = 3.0
RESTORE_FADE_S = 3.0

log = logging.getLogger(__name__)


def ffmpeg_deck(track: Track) -> Deck:
    return Deck(FfmpegSource(track.path), track.id, track.pool_id, track.duration_s)


class Director:
    """Turns 'play this pool' into mixer switches; holds the switch while a pool is still empty."""

    def __init__(self, lib: Library, engine: Engine,
                 deck_factory: Callable[[Track], Deck] | None = None, rng: random.Random | None = None):
        self.lib = lib
        self.engine = engine
        self.deck_factory = deck_factory or ffmpeg_deck
        self.rng = rng or random.Random()
        self.pending: tuple[int, float] | None = None
        self.playing_pool_id: int | None = None
        self.applied: dict | None = lib.get_state("applied")
        engine.on_near_end = self._near_end

    @property
    def pending_pool_id(self) -> int | None:
        return self.pending[0] if self.pending else None

    def _deck_for(self, pool_id: int) -> Deck | None:
        track = pick_track(self.lib.tracks(pool_id), self.lib.recent_ids(pool_id), self.rng)
        if track is None:
            return None
        self.lib.record_play(track.id)
        return self.deck_factory(track)

    def _near_end(self, deck: Deck) -> Deck | None:
        if deck.pool_id is None:
            return None
        try:
            nxt = self._deck_for(deck.pool_id)
        except Exception:
            log.exception("continuing pool %s failed", deck.pool_id)
            nxt = None
        if nxt is None and self.pending is None:
            self.pending = (deck.pool_id, NEAR_END_S)
        return nxt

    def switch_pool(self, pool_id: int, fade_s: float, *, keep_pending: bool = False) -> bool:
        deck = self._deck_for(pool_id)
        if deck is None:
            if not (keep_pending and self.pending is not None):
                self.pending = (pool_id, fade_s)
            return False
        if not keep_pending:
            self.pending = None
        with self.engine.lock:
            self.engine.switch(deck, fade_s)
        self.playing_pool_id = pool_id
        self.lib.set_state("playing_pool_id", pool_id)
        return True

    def apply_scene(self, scene: Scene, vocab: Vocab, fade_s: float) -> bool:
        pool = self.lib.pool_for(scene_prompt(scene, vocab), scene_id=scene.id)
        self.applied = {"pool_id": pool.id, "name": scene.name if scene.saved else "Custom"}
        self.lib.set_state("applied", self.applied)
        return self.switch_pool(pool.id, fade_s)

    def apply_pool(self, pool_id: int, name: str, fade_s: float) -> bool:
        """Play a pool directly (a favourite's remembered prompt), no scene needed."""
        self.applied = {"pool_id": pool_id, "name": name}
        self.lib.set_state("applied", self.applied)
        return self.switch_pool(pool_id, fade_s)

    def on_track_added(self, pool_id: int) -> None:
        if self.pending and self.pending[0] == pool_id:
            self.switch_pool(*self.pending)

    def fadeout(self, fade_s: float) -> None:
        self.pending = None
        with self.engine.lock:
            self.engine.switch(None, fade_s)
        self.playing_pool_id = None
        self.lib.set_state("playing_pool_id", None)

    def skip(self, fade_s: float = SKIP_FADE_S) -> None:
        if self.playing_pool_id is not None:
            self.switch_pool(self.playing_pool_id, fade_s, keep_pending=True)

    def rate(self, track_id: int, rating: str) -> Track:
        track = self.lib.set_rating(track_id, rating)
        playing = self.now_playing()
        if rating == "banned" and playing and playing["track_id"] == track_id:
            self.skip()
        return track

    def now_playing(self) -> dict | None:
        with self.engine.lock:
            m = self.engine.main
            if m is None:
                return None
            return {"track_id": m.track_id, "pool_id": m.pool_id,
                    "position_s": round(m.pos / RATE, 1), "duration_s": m.duration_s}

    def restore(self) -> None:
        pool_id = self.lib.get_state("playing_pool_id")
        if pool_id is not None and self.lib.get_pool(pool_id) is not None:
            self.switch_pool(pool_id, RESTORE_FADE_S)
