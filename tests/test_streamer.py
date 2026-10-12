import asyncio
import subprocess

import numpy as np

from dndmusic.mixer import RATE, ArraySource, Deck, Engine
from dndmusic.streamer import AudioPipeline, Broadcaster


async def test_new_listener_gets_recent_burst():
    b = Broadcaster(asyncio.get_running_loop(), max_chunks=2, burst_bytes=10)
    for chunk in (b"aaaa", b"bbbb", b"cccc"):
        b._deliver(chunk)
    q = b.subscribe()
    assert [q.get_nowait() for _ in range(q.qsize())] == [b"bbbb", b"cccc"]


async def test_slow_listener_is_dropped():
    b = Broadcaster(asyncio.get_running_loop(), max_chunks=2, burst_bytes=0)
    q = b.subscribe()
    for chunk in (b"1", b"2", b"3"):
        b._deliver(chunk)
    assert b.listener_count == 0
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert items[-1] is None


async def test_pipeline_streams_mp3(tmp_path):
    engine = Engine()
    t = np.arange(RATE * 10) / RATE
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    engine.switch(Deck(ArraySource(np.stack([x, x], 1)), 1, 1, 10.0), 0)
    b = Broadcaster(asyncio.get_running_loop())
    q = b.subscribe()
    pipeline = AudioPipeline(engine, b).start()
    data = bytearray()
    try:
        while len(data) < 40_000:
            data += await asyncio.wait_for(q.get(), timeout=5)
    finally:
        pipeline.stop()
    f = tmp_path / "s.mp3"
    f.write_bytes(data)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate",
                            "-of", "csv=p=0", str(f)], capture_output=True, text=True)
    assert probe.stdout.strip() == "mp3,48000"


class CountingEngine(Engine):
    def __init__(self):
        super().__init__()
        self.frames = 0

    def render(self, n):
        self.frames += n
        return super().render(n)


async def test_pipeline_runs_in_real_time():
    engine = CountingEngine()
    pipeline = AudioPipeline(engine, Broadcaster(asyncio.get_running_loop()), lead_s=0.5).start()
    await asyncio.sleep(1.0)
    pipeline.stop()
    assert 1.0 * RATE <= engine.frames <= 1.6 * RATE


async def test_encoder_restart_on_death():
    engine = Engine()
    t = np.arange(RATE * 10) / RATE
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    engine.switch(Deck(ArraySource(np.stack([x, x], 1)), 1, 1, 10.0), 0)
    b = Broadcaster(asyncio.get_running_loop())
    q = b.subscribe()
    pipeline = AudioPipeline(engine, b).start()
    # Receive some initial data
    initial_data = bytearray()
    while len(initial_data) < 10_000:
        initial_data += await asyncio.wait_for(q.get(), timeout=5)
    # Kill the encoder process
    pipeline._enc.kill()
    await asyncio.sleep(0.1)
    # Old listener should get None (hangup)
    old_q = q
    hangup_msg = None
    try:
        hangup_msg = await asyncio.wait_for(old_q.get(), timeout=2)
    except asyncio.TimeoutError:
        pass
    assert hangup_msg is None
    # New subscriber should get data within 5 seconds
    new_q = b.subscribe()
    new_data = bytearray()
    try:
        while len(new_data) < 5_000:
            new_data += await asyncio.wait_for(new_q.get(), timeout=5)
    finally:
        pipeline.stop()
    assert len(new_data) > 0


async def test_stop_before_start():
    pipeline = AudioPipeline(Engine(), Broadcaster(asyncio.get_running_loop()))
    pipeline.stop()  # Should not raise


async def test_stop_cleanup():
    engine = Engine()
    t = np.arange(RATE * 2) / RATE
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    engine.switch(Deck(ArraySource(np.stack([x, x], 1)), 1, 1, 2.0), 0)
    b = Broadcaster(asyncio.get_running_loop())
    q = b.subscribe()
    pipeline = AudioPipeline(engine, b).start()
    # Receive some data
    data = bytearray()
    while len(data) < 5_000:
        data += await asyncio.wait_for(q.get(), timeout=5)
    pipeline.stop()
    # Check that encoder process has a returncode
    assert pipeline._enc.returncode is not None
    # Check that consumer thread is not alive
    assert not pipeline._consumer_thread.is_alive()


async def test_stop_kills_stuck_encoder(monkeypatch):
    """Verify that stop() kills the encoder if the producer thread is stuck."""
    engine = Engine()
    t = np.arange(RATE * 2) / RATE
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    engine.switch(Deck(ArraySource(np.stack([x, x], 1)), 1, 1, 2.0), 0)
    b = Broadcaster(asyncio.get_running_loop())
    q = b.subscribe()
    pipeline = AudioPipeline(engine, b).start()
    # Receive some data
    while len([await asyncio.wait_for(q.get(), timeout=5)]) < 1:
        pass
    # Monkeypatch producer thread's is_alive to simulate a stuck producer
    original_is_alive = pipeline._producer_thread.is_alive
    monkeypatch.setattr(pipeline._producer_thread, "is_alive", lambda: True)
    # Call stop() - it should kill the encoder despite stuck producer
    pipeline.stop()
    # Verify encoder was killed
    assert pipeline._enc.returncode is not None


async def test_default_listener_queue_holds_about_ten_seconds():
    b = Broadcaster(asyncio.get_running_loop(), burst_bytes=0)
    q = b.subscribe()
    for _ in range(400):
        b._deliver(b"x")
    assert b.listener_count == 1
    b._deliver(b"x")
    assert b.listener_count == 0 and q.qsize() > 0
