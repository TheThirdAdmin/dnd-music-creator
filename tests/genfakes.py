import asyncio
import io

import numpy as np
import soundfile as sf

RATE = 8000


def flac(seconds: float, freq: float = 440.0) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    buf = io.BytesIO()
    sf.write(buf, (0.3 * np.sin(2 * np.pi * freq * t)).astype("float32"), RATE, format="FLAC")
    return buf.getvalue()


class FakeRenderer:
    def __init__(self, fail: str | None = None, delay_s: float = 0.01):
        self.fail, self.delay_s = fail, delay_s
        self.calls: list[tuple] = []
        self.frees = self.active = self.max_active = 0

    async def render(self, ckpt, prompt, seconds, seed):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((ckpt, prompt, seconds, seed))
            await asyncio.sleep(self.delay_s)
            if self.fail:
                raise RuntimeError(self.fail)
            return flac(seconds)
        finally:
            self.active -= 1

    async def free(self):
        self.frees += 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t
