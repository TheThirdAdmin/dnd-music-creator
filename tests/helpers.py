import io
import socket
import wave

import numpy as np


def wav_bytes(seconds: float, amp: float = 0.3, freq: float = 440.0, rate: int = 22050, pad_s: float = 0.0) -> bytes:
    """Mono 16-bit WAV: pad_s of silence, a sine of `seconds`, pad_s of silence."""
    t = np.arange(int(rate * seconds)) / rate
    pad = np.zeros(int(rate * pad_s))
    x = np.concatenate([pad, amp * np.sin(2 * np.pi * freq * t), pad])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def wav_parts(parts: list[tuple[float, float]], freq: float = 440.0, rate: int = 22050) -> bytes:
    """Mono 16-bit WAV of consecutive (seconds, amplitude) sine sections."""
    x = np.concatenate([amp * np.sin(2 * np.pi * freq * np.arange(int(rate * s)) / rate) for s, amp in parts])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
