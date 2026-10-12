import asyncio
import collections
import logging
import subprocess
import threading
import time

import numpy as np

from .mixer import RATE, Engine

log = logging.getLogger(__name__)


class Broadcaster:
    """Fans MP3 chunks out to listener queues; drops listeners that fall behind."""

    def __init__(self, loop: asyncio.AbstractEventLoop, max_chunks: int = 400, burst_bytes: int = 48_000):
        self._loop = loop
        self._max = max_chunks
        self._burst = burst_bytes
        self._listeners: set[asyncio.Queue] = set()
        self._recent: collections.deque[bytes] = collections.deque()
        self._recent_bytes = 0

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._max + len(self._recent))
        for chunk in self._recent:
            q.put_nowait(chunk)
        self._listeners.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._listeners.discard(q)

    def publish(self, chunk: bytes) -> None:
        """Called from the encoder thread."""
        try:
            self._loop.call_soon_threadsafe(self._deliver, chunk)
        except RuntimeError:
            pass  # event loop closed during shutdown

    def hangup_all(self) -> None:
        """Hang up all current listeners with None sentinel (thread-safe)."""
        try:
            self._loop.call_soon_threadsafe(self._do_hangup_all)
        except RuntimeError:
            pass  # event loop closed during shutdown

    def _do_hangup_all(self) -> None:
        """Deliver None to all listeners (runs in event loop)."""
        for q in list(self._listeners):
            if q.full():
                q.get_nowait()
            q.put_nowait(None)
        self._listeners.clear()
        # Clear burst buffer so new subscribers don't get stale data
        self._recent.clear()
        self._recent_bytes = 0

    def _deliver(self, chunk: bytes) -> None:
        self._recent.append(chunk)
        self._recent_bytes += len(chunk)
        while self._recent and self._recent_bytes > self._burst:
            self._recent_bytes -= len(self._recent.popleft())
        for q in list(self._listeners):
            if q.full():
                self._listeners.discard(q)
                q.get_nowait()
                q.put_nowait(None)
            else:
                q.put_nowait(chunk)


class AudioPipeline:
    """Renders the engine in real time into an ffmpeg MP3 encoder feeding the broadcaster."""

    def __init__(self, engine: Engine, broadcaster: Broadcaster, *, block_s: float = 0.1,
                 lead_s: float = 0.5, bitrate: str = "192k"):
        self.engine = engine
        self.broadcaster = broadcaster
        self.block_s = block_s
        self.lead_s = lead_s
        self.bitrate = bitrate
        self._stop = threading.Event()
        self._enc: subprocess.Popen | None = None
        self._producer_thread: threading.Thread | None = None
        self._consumer_thread: threading.Thread | None = None

    def start(self) -> "AudioPipeline":
        self._stop.clear()
        self._start_encoder()
        self._producer_thread = threading.Thread(target=self._produce, name="mixer", daemon=True)
        self._producer_thread.start()
        return self

    def _start_encoder(self) -> None:
        """Spawn ffmpeg and its consumer thread."""
        # Clean up old encoder if restarting
        if self._enc is not None:
            old_enc = self._enc
            # Close old stdin
            try:
                old_enc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass
            # Kill and wait for old process
            if old_enc.poll() is None:
                try:
                    old_enc.kill()
                except (OSError, ValueError):
                    pass
            try:
                old_enc.wait()
            except (OSError, ValueError):
                pass
            # Wait for old consumer thread to finish reading
            if self._consumer_thread is not None and self._consumer_thread.is_alive():
                self._consumer_thread.join(timeout=2)
            # Close old stdout
            try:
                old_enc.stdout.close()
            except (BrokenPipeError, ValueError, OSError):
                pass
        # Spawn new encoder
        self._enc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", "-",
             "-c:a", "libmp3lame", "-b:a", self.bitrate, "-flush_packets", "1", "-f", "mp3", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        # Spawn new consumer thread
        self._consumer_thread = threading.Thread(target=self._consume, name="encoder", daemon=True)
        self._consumer_thread.start()

    def _produce(self) -> None:
        n = int(RATE * self.block_s)
        start = time.monotonic()
        sent = 0
        while not self._stop.is_set():
            ahead = sent / RATE - (time.monotonic() - start)
            if ahead + self.block_s > self.lead_s:
                time.sleep(min(ahead + self.block_s - self.lead_s, self.block_s))
                continue
            try:
                with self.engine.lock:
                    block = self.engine.render(n)
            except Exception:
                log.exception("mixer render failed; sending silence")
                block = np.zeros((n, 2), np.float32)
            try:
                self._enc.stdin.write(block.astype("<f4").tobytes())
                self._enc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                if not self._stop.is_set():
                    log.error("encoder pipe closed; restarting")
                    self.broadcaster.hangup_all()
                    self._start_encoder()
                else:
                    return
            sent += n

    def _consume(self) -> None:
        while chunk := self._enc.stdout.read1(4096):
            self.broadcaster.publish(chunk)

    def stop(self) -> None:
        if self._enc is None:
            return  # start() was never called
        self._stop.set()
        # Join producer thread (it may be sleeping in pacing, so wait for it)
        if self._producer_thread is not None:
            self._producer_thread.join(timeout=2)
            # If producer is still alive, it's blocked in stdin.write; kill encoder first
            if self._producer_thread.is_alive():
                try:
                    self._enc.kill()
                except (OSError, ValueError):
                    pass
        # Close stdin to signal EOF to ffmpeg
        try:
            self._enc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
        # Wait for ffmpeg to finish encoding any remaining buffered data
        try:
            self._enc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                self._enc.kill()
                self._enc.wait()
            except (OSError, ValueError):
                pass
        # Join consumer thread
        if self._consumer_thread is not None:
            self._consumer_thread.join(timeout=2)
        # Close stdout
        try:
            self._enc.stdout.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
