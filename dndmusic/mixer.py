"""Deck mixer: gain envelopes, equal-power crossfades, automatic continuation."""
import logging
import math
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

RATE = 48000
NEAR_END_S = 8.0
TAIL_S = 0.5
MIN_FADE_S = 0.05


class Source(Protocol):
    def read(self, n: int) -> np.ndarray: ...
    def close(self) -> None: ...


class ArraySource:
    """In-memory stereo audio (tests, generated signals)."""

    def __init__(self, data: np.ndarray):
        self._data = np.asarray(data, dtype=np.float32).reshape(-1, 2)
        self._pos = 0
        self.closed = False

    def read(self, n: int) -> np.ndarray:
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def close(self) -> None:
        self.closed = True


class FfmpegSource:
    """Decodes a file to 48 kHz stereo float32 through an ffmpeg pipe."""

    def __init__(self, path: Path | str):
        self._proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(RATE), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, n: int) -> np.ndarray:
        want = n * 8
        buf = bytearray()
        while len(buf) < want:
            chunk = self._proc.stdout.read(want - len(buf))
            if not chunk:
                break
            buf += chunk
        usable = len(buf) - len(buf) % 8
        return np.frombuffer(bytes(buf[:usable]), dtype="<f4").reshape(-1, 2)

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.kill()
        self._proc.stdout.close()
        self._proc.wait()


@dataclass(eq=False)
class Deck:
    source: Source
    track_id: int | None
    pool_id: int | None
    duration_s: float
    pos: int = 0
    ended: bool = False
    continued: bool = False
    gain: float = 1.0
    _start: float = 1.0
    _end: float = 1.0
    _len: int = 0
    _at: int = 0
    _curve: str = "lin"

    @property
    def remaining_s(self) -> float:
        return self.duration_s - self.pos / RATE

    @property
    def ramping(self) -> bool:
        return self._len > 0

    @property
    def curve(self) -> str:
        return self._curve

    def ramp(self, end: float, seconds: float, curve: str) -> None:
        """Move gain from its current value to `end`; curve 'in' (sine), 'out' (cosine) or 'lin'."""
        self._start, self._end, self._curve = self.gain, end, curve
        self._len, self._at = max(1, int(seconds * RATE)), 0

    def envelope(self, n: int) -> np.ndarray:
        if not self._len:
            return np.full(n, self.gain, np.float32)
        t = np.minimum((self._at + np.arange(1, n + 1)) / self._len, 1.0)
        if self._curve == "in":
            shape = np.sin(t * math.pi / 2)
        elif self._curve == "out":
            shape = 1.0 - np.cos(t * math.pi / 2)
        else:
            shape = t
        g = self._start + (self._end - self._start) * shape
        self._at += n
        if self._at >= self._len:
            self._len = 0
            self.gain = self._end
        else:
            self.gain = float(g[-1])
        return g.astype(np.float32)


class Engine:
    """Sums decks under their gain envelopes. Hold `lock` around switch/render across threads."""

    def __init__(self, on_near_end: Callable[[Deck], Deck | None] | None = None):
        self.lock = threading.RLock()
        self.decks: list[Deck] = []
        self.main: Deck | None = None
        self.on_near_end = on_near_end

    def switch(self, deck: Deck | None, fade_s: float) -> None:
        """Crossfade to `deck` (None = silence). The loudest deck fades with the new one; others within TAIL_S."""
        fade_s = max(fade_s, MIN_FADE_S)
        loudest = max(self.decks, key=lambda d: d.gain, default=None)
        for d in self.decks:
            if d is loudest:
                d.ramp(0.0, fade_s, "out")
            else:
                d.ramp(0.0, min(TAIL_S, fade_s), "lin")
        if deck is not None:
            deck.gain = 0.0
            deck.ramp(1.0, fade_s, "in")
            self.decks.append(deck)
        self.main = deck

    def render(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), np.float32)
        for d in self.decks:
            chunk = d.source.read(n)
            m = len(chunk)
            d.pos += m
            g = d.envelope(n)
            if m:
                out[:m] += chunk * g[:m, None]
            if m < n:
                d.ended = True
        self._continue_main()
        self._drop_finished()
        return out

    def _continue_main(self) -> None:
        m = self.main
        if m is None or m.continued or self.on_near_end is None:
            return
        if m.ended or m.remaining_s <= NEAR_END_S:
            m.continued = True
            try:
                nxt = self.on_near_end(m)
            except Exception:
                logging.getLogger(__name__).exception("on_near_end callback failed")
                nxt = None
            if nxt is not None:
                fade_s = MIN_FADE_S if m.ended else min(NEAR_END_S, max(m.remaining_s, MIN_FADE_S))
                self.switch(nxt, fade_s)

    def _drop_finished(self) -> None:
        keep = []
        for d in self.decks:
            if d.ended or (d.gain == 0.0 and not d.ramping):
                d.source.close()
                if d is self.main:
                    self.main = None
            else:
                keep.append(d)
        self.decks = keep
