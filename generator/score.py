"""Quality 3 keeps the better of two renders: penalise near-silence and loops. Higher is better (0 is best)."""
import io

import numpy as np
import soundfile as sf

QUIET_DB = 30.0     # a 1 s window this far below the track's RMS counts as quiet
MIN_LAG_S = 8       # repetition is judged at lags of at least 8 s ...
WORST = -2.0
SILENT_RMS = 0.003  # a whole track below this (about -50 dBFS) is a failed render
REPEAT_SIM = 0.9    # ... where the mean chroma similarity exceeds this


def decode(audio: bytes) -> tuple[np.ndarray, int]:
    x, rate = sf.read(io.BytesIO(audio), dtype="float32", always_2d=True)
    return x.mean(axis=1), rate


def duration_of(audio: bytes) -> float:
    return sf.info(io.BytesIO(audio)).duration


def _windows(x: np.ndarray, rate: int) -> np.ndarray:
    n = len(x) // rate
    return x[: n * rate].reshape(n, rate)


def quiet_share(x: np.ndarray, rate: int) -> float:
    """Share of 1 s windows more than QUIET_DB below the whole track's RMS."""
    w = _windows(x, rate)
    if len(w) == 0:
        return 1.0
    track = np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12
    each = np.sqrt(np.mean(np.square(w, dtype=np.float64), axis=1)) + 1e-12
    return float(np.mean(20 * np.log10(each / track) < -QUIET_DB))


def _chroma(x: np.ndarray, rate: int) -> np.ndarray:
    """A unit-length 12-bin pitch-class profile per 1 s window (55 Hz to 5 kHz)."""
    w = _windows(x, rate) * np.hanning(rate)
    mag = np.abs(np.fft.rfft(w, axis=1))
    freqs = np.fft.rfftfreq(rate, 1 / rate)
    band = (freqs >= 55) & (freqs <= 5000)
    pitch_class = np.round(12 * np.log2(freqs[band] / 440.0)).astype(int) % 12
    out = np.zeros((len(w), 12))
    for k in range(12):
        out[:, k] = mag[:, band][:, pitch_class == k].sum(axis=1)
    norm = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norm == 0, 1, norm)


def repetition(x: np.ndarray, rate: int) -> float:
    """Share of lags >= MIN_LAG_S (with at least MIN_LAG_S window pairs) whose mean chroma
    self-similarity exceeds REPEAT_SIM: a looped section shows up as a near-1 diagonal."""
    c = _chroma(x, rate)
    lags = range(MIN_LAG_S, len(c) - MIN_LAG_S + 1)
    if not lags:
        return 0.0
    sim = c @ c.T
    return float(np.mean([np.mean(np.diagonal(sim, offset=lag)) > REPEAT_SIM for lag in lags]))


def score(audio: bytes) -> float:
    x, rate = decode(audio)
    if len(x) == 0 or np.sqrt(np.mean(np.square(x, dtype=np.float64))) < SILENT_RMS:
        return WORST
    return -(quiet_share(x, rate) + repetition(x, rate))
