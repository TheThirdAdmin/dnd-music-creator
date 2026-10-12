import pytest

from dndmusic import ingest as ingest_mod
from dndmusic.ingest import TARGET_LUFS, RejectedAudio, analyse, ingest, loudness, straight_cut
from helpers import wav_bytes, wav_parts


def test_trims_silence_and_encodes_opus(tmp_path):
    out = tmp_path / "t.opus"
    duration = ingest(wav_bytes(35, pad_s=4), out)
    assert abs(duration - 35) < 0.5
    assert out.read_bytes()[:4] == b"OggS"


@pytest.mark.parametrize("amp", [0.05, 0.6])
def test_output_is_minus_16_lufs(tmp_path, amp):
    out = tmp_path / "t.opus"
    ingest(wav_bytes(35, amp=amp), out)
    integrated, _ = loudness(out)
    assert abs(integrated - TARGET_LUFS) <= 1.0


def test_straight_cut_finds_the_build_up():
    frames = [(t / 10, -40.0 if t < 400 else -14.0) for t in range(1, 2000)]
    assert straight_cut(200.0, -16.0, frames) == 33.0
    assert straight_cut(150.0, -16.0, frames) is None
    assert straight_cut(200.0, -16.0, [(t / 10, -15.0) for t in range(1, 2000)]) is None
    assert straight_cut(200.0, -16.0, [(t / 10, -40.0) for t in range(1, 2000)]) is None


def test_straight_in_cuts_the_quiet_start(tmp_path):
    raw = wav_parts([(40, 0.01), (150, 0.5)])
    plain = ingest(raw, tmp_path / "plain.opus")
    cut = ingest(raw, tmp_path / "cut.opus", straight=True)
    assert abs(plain - 190) < 1
    assert 150 < cut < 160


def test_straight_in_keeps_short_tracks_whole(tmp_path):
    assert abs(ingest(wav_parts([(40, 0.01), (100, 0.5)]), tmp_path / "t.opus", straight=True) - 140) < 1


def test_rechecks_length_after_trimming(tmp_path):
    out = tmp_path / "t.opus"
    with pytest.raises(RejectedAudio, match="too short after trimming"):
        ingest(wav_bytes(25, pad_s=4), out)
    assert list(tmp_path.iterdir()) == []


def test_no_partial_file_when_the_final_check_fails(tmp_path, monkeypatch):
    real = ingest_mod.analyse

    def failing(path):
        if path.name.startswith("."):
            raise RejectedAudio("undecodable: simulated")
        return real(path)

    monkeypatch.setattr(ingest_mod, "analyse", failing)
    with pytest.raises(RejectedAudio, match="simulated"):
        ingest(wav_bytes(35), tmp_path / "t.opus")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw,reason", [
    (wav_bytes(10), "too short"),
    (wav_bytes(35, amp=0.001), "near-silent"),
    (b"definitely not audio" * 100, "undecodable"),
], ids=["too_short", "near_silent", "undecodable"])
def test_rejects_bad_output(tmp_path, raw, reason):
    out = tmp_path / "x.opus"
    with pytest.raises(RejectedAudio, match=reason):
        ingest(raw, out)
    assert not out.exists()


def test_analyse_reports_duration(tmp_path):
    out = tmp_path / "t.opus"
    ingest(wav_bytes(35), out)
    assert abs(analyse(out)[0] - 35) < 0.5
