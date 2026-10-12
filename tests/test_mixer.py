import numpy as np

from dndmusic.mixer import RATE, ArraySource, Deck, Engine


def const(value, seconds):
    return ArraySource(np.full((int(RATE * seconds), 2), value, np.float32))


def sine(freq, seconds, phase=0.0, amp=0.5):
    t = np.arange(int(RATE * seconds)) / RATE
    x = amp * np.sin(2 * np.pi * freq * t + phase)
    return ArraySource(np.stack([x, x], 1))


def deck(src, track_id=1, pool_id=1, seconds=60.0):
    return Deck(src, track_id, pool_id, seconds)


def settled(first_value=1.0):
    e = Engine()
    e.switch(deck(const(first_value, 30)), 0)
    e.render(RATE)
    return e


def test_single_deck_plays_at_full_gain():
    e = settled(0.5)
    assert np.allclose(e.render(1000), 0.5)


def test_crossfade_is_equal_power_and_completes():
    ea = settled(1.0)
    ea.switch(deck(const(0.0, 30), 2), 2.0)
    gain_a = ea.render(2 * RATE)[:, 0]
    eb = settled(0.0)
    eb.switch(deck(const(1.0, 30), 2), 2.0)
    gain_b = eb.render(2 * RATE)[:, 0]
    assert np.allclose(gain_a ** 2 + gain_b ** 2, 1.0, atol=1e-3)
    assert gain_a[0] > 0.99 and gain_b[-1] > 0.999
    ea.render(10)
    assert len(ea.decks) == 1 and ea.main.track_id == 2


def test_switch_mid_fade_drops_quieter_deck_within_half_a_second():
    e = settled(0.1)
    e.switch(deck(const(0.1, 30), 2), 4.0)
    e.render(RATE)
    a, b = e.decks
    assert a.gain > b.gain
    e.switch(deck(const(0.1, 30), 3), 4.0)
    assert a.curve == "out" and b.curve == "lin"
    e.render(int(RATE * 0.5) + 1)
    assert b not in e.decks and len(e.decks) == 2


def test_hard_cut_has_no_click():
    e = Engine()
    e.switch(deck(sine(440, 10)), 0)
    e.render(RATE)
    e.switch(deck(sine(440, 10, phase=np.pi), 2), 0)
    out = np.concatenate([e.render(480) for _ in range(20)])[:, 0]
    assert np.max(np.abs(np.diff(out))) < 0.05


def test_near_end_continues_once():
    calls = []

    def nxt(d):
        calls.append(d.track_id)
        return deck(const(0.2, 30), track_id=d.track_id + 1, pool_id=d.pool_id)

    e = Engine(on_near_end=nxt)
    e.switch(deck(const(0.2, 10), seconds=10.0), 0)
    for _ in range(25):
        e.render(RATE // 10)
    assert calls == [1] and e.main.track_id == 2
    for _ in range(10):
        e.render(RATE // 10)
    assert calls == [1]


def test_fade_to_silence():
    e = settled(0.5)
    e.switch(None, 1.0)
    assert e.main is None
    for _ in range(11):
        e.render(RATE // 10)
    assert e.decks == [] and np.allclose(e.render(100), 0.0)


def test_ended_deck_is_dropped_and_closed():
    e = Engine()
    src = const(0.5, 1.0)
    e.switch(deck(src, seconds=60.0), 0)
    for _ in range(15):
        e.render(RATE // 10)
    assert e.decks == [] and e.main is None and src.closed


def test_continuation_while_fading_in_short_track():
    calls = []

    def nxt(d):
        calls.append(d.track_id)
        return deck(const(0.2, 30), track_id=d.track_id + 1, pool_id=d.pool_id)

    e = Engine(on_near_end=nxt)
    e.switch(deck(const(0.2, 10), track_id=1, pool_id=1, seconds=3.0), 5.0)
    for _ in range(35):
        e.render(RATE // 10)
    assert calls == [1] and e.main.track_id == 2


def test_fade_capped_at_remaining_time():
    calls = []
    old_deck_ref = None

    def nxt(d):
        nonlocal old_deck_ref
        calls.append(d.track_id)
        old_deck_ref = d
        return deck(const(0.2, 30), track_id=d.track_id + 1, pool_id=d.pool_id)

    e = Engine(on_near_end=nxt)
    e.switch(deck(const(0.2, 10), track_id=1, pool_id=1, seconds=10.0), 0)
    while old_deck_ref is None:
        e.render(RATE // 10)
    while old_deck_ref in e.decks:
        e.render(RATE // 10)
    assert old_deck_ref.gain == 0.0


def test_callback_exception_logged_and_ignored():
    calls = []

    def nxt(d):
        calls.append(d.track_id)
        if d.track_id == 1:
            raise RuntimeError("test error")
        return deck(const(0.2, 30), track_id=d.track_id + 1, pool_id=d.pool_id)

    e = Engine(on_near_end=nxt)
    e.switch(deck(const(0.2, 10), track_id=1, pool_id=1, seconds=10.0), 0)
    for _ in range(25):
        e.render(RATE // 10)
    assert calls == [1]
    out = e.render(RATE // 10)
    assert out.shape == (RATE // 10, 2) and calls == [1]
