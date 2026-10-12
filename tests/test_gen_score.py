import io

import numpy as np
import pytest
import soundfile as sf

from generator.score import decode, duration_of, quiet_share, repetition, score

RATE = 8000


def notes(seconds, seed):
    """Random notes, two per second, over three octaves: 'music' that never repeats."""
    rng = np.random.default_rng(seed)
    t = np.arange(RATE // 2) / RATE
    return np.concatenate([0.3 * np.sin(2 * np.pi * 110 * 2 ** (rng.integers(0, 36) / 12) * t)
                           for _ in range(int(seconds * 2))])


def flac(x):
    buf = io.BytesIO()
    sf.write(buf, x.astype("float32"), RATE, format="FLAC")
    return buf.getvalue()


def test_decode_and_duration():
    b = flac(notes(5, 1))
    x, rate = decode(b)
    assert rate == RATE and x.ndim == 1 and abs(len(x) / RATE - 5) < 0.01
    assert abs(duration_of(b) - 5) < 0.01


def test_varied_music_scores_near_zero():
    assert score(flac(notes(60, 1))) > -0.05


def test_silence_is_penalised():
    x = notes(60, 1)
    x[: RATE * 24] = 0
    assert quiet_share(x, RATE) == pytest.approx(0.4, abs=0.02)
    assert score(flac(x)) < score(flac(notes(60, 1))) - 0.3


def test_loops_are_penalised():
    loop = np.tile(notes(10, 2), 6)
    assert repetition(notes(60, 2), RATE) == 0.0
    assert repetition(loop, RATE) > 0.05
    assert score(flac(loop)) < score(flac(notes(60, 2)))


def test_near_silence_scores_worse_than_music():
    assert score(flac(np.zeros(RATE * 60))) < score(flac(notes(60, 1)))
    assert score(flac(notes(60, 1) * 0.0001)) < score(flac(notes(60, 1)))
