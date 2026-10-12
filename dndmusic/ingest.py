"""Generator output → validated, trimmed, loudness-normalised Opus file."""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np

MIN_DURATION_S = 30.0
MIN_RMS = 0.003  # about -50 dBFS
TARGET_LUFS = -16.0
LOUDNORM = f"loudnorm=I={TARGET_LUFS:g}:TP=-1.5:LRA=11"
TRIM_SILENCE = ",".join([
    "silenceremove=start_periods=1:start_threshold=-50dB",
    "areverse",
    "silenceremove=start_periods=1:start_threshold=-50dB",
    "areverse",
])
SHORT_TERM_S = 3.0           # ebur128 short-term window
STRAIGHT_WITHIN_LU = 6.0     # first window this close to the integrated loudness ...
STRAIGHT_LEAD_S = 4.0        # ... and start this long before it
STRAIGHT_FADE_S = 1.0
STRAIGHT_MIN_LEFT_S = 120.0  # don't cut if less than this would remain

_FRAME = re.compile(r"t:\s*([\d.]+)\s.*?S:\s*(-?[\d.]+|-?inf)")
_INTEGRATED = re.compile(r"I:\s*(-?[\d.]+|-?inf) LUFS")


class RejectedAudio(Exception):
    """Generator output that is not usable as a track."""


def _err(p: subprocess.CompletedProcess) -> str:
    text = p.stderr if isinstance(p.stderr, str) else p.stderr.decode(errors="replace")
    return text.strip()[-200:]


def analyse(path: Path) -> tuple[float, float]:
    """Duration in seconds and RMS, from an 8 kHz mono decode."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise RejectedAudio(f"undecodable: {_err(p)}")
    x = np.frombuffer(p.stdout, dtype="<f4")
    if x.size == 0:
        raise RejectedAudio("undecodable: no audio")
    return x.size / 8000, float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def check_audio(path: Path) -> float:
    """Decodes, is long enough and not near-silent; returns the duration. Raises RejectedAudio."""
    duration, rms = analyse(path)
    if duration < MIN_DURATION_S:
        raise RejectedAudio(f"too short: {duration:.1f}s")
    if rms < MIN_RMS:
        raise RejectedAudio(f"near-silent: rms {rms:.4f}")
    return duration


def loudness(path: Path) -> tuple[float, list[tuple[float, float]]]:
    """Integrated loudness (LUFS) and short-term loudness every 100 ms, from ffmpeg's ebur128 filter."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
                        "-af", "ebur128=framelog=info", "-f", "null", "-"], capture_output=True, text=True)
    if p.returncode != 0:
        raise RejectedAudio(f"undecodable: {_err(p)}")
    head, _, summary = p.stderr.rpartition("Summary:")
    frames = [(float(m.group(1)), float(m.group(2))) for m in map(_FRAME.search, head.splitlines()) if m]
    m = _INTEGRATED.search(summary)
    if not m:
        raise RejectedAudio("loudness analysis failed")
    return float(m.group(1)), frames


def straight_cut(duration_s: float, integrated: float, frames: list[tuple[float, float]]) -> float | None:
    """Start of a straight-in track: STRAIGHT_LEAD_S before the first full short-term window within
    STRAIGHT_WITHIN_LU of the integrated loudness. None if there is nothing to cut or too little would remain."""
    for t, short_term in frames:
        if t >= SHORT_TERM_S and short_term >= integrated - STRAIGHT_WITHIN_LU:
            cut = round(max(0.0, t - SHORT_TERM_S - STRAIGHT_LEAD_S), 1)
            break
    else:
        return None
    if cut <= 0 or duration_s - cut < STRAIGHT_MIN_LEFT_S:
        return None
    return cut


def _measure(path: Path, pre: str) -> dict:
    """loudnorm pass 1: the input's loudness stats as loudnorm reports them."""
    af = ",".join(x for x in (pre, f"{LOUDNORM}:print_format=json") if x)
    p = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path), "-af", af, "-f", "null", "-"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RejectedAudio(f"undecodable: {_err(p)}")
    try:
        return json.loads(p.stderr[p.stderr.rindex("{"):p.stderr.rindex("}") + 1])
    except ValueError as e:
        raise RejectedAudio("loudness measurement failed") from e


def ingest(raw: bytes, out_path: Path, *, straight: bool = False) -> float:
    """Check, trim, (for straight-in scenes) cut the build-up, normalise to TARGET_LUFS and encode Opus.
    The output appears atomically at out_path, or not at all. Returns the final duration."""
    out_path = Path(out_path)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "raw"
        src.write_bytes(raw)
        check_audio(src)
        trimmed = Path(tmp) / "trimmed.wav"
        p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-af", TRIM_SILENCE,
                            "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", str(trimmed)], capture_output=True)
        if p.returncode != 0:
            raise RejectedAudio(f"undecodable: {_err(p)}")
        pre = ""
        if straight:
            integrated, frames = loudness(trimmed)
            cut = straight_cut(analyse(trimmed)[0], integrated, frames)
            if cut is not None:
                pre = f"atrim=start={cut:g},asetpts=PTS-STARTPTS,afade=t=in:d={STRAIGHT_FADE_S:g}"
        m = _measure(trimmed, pre)
        second = (f"{LOUDNORM}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
                  f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
        af = ",".join(x for x in (pre, second) if x)
        part = out_path.with_name(f".{out_path.name}.part")
        try:
            p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(trimmed), "-af", af,
                                "-ar", "48000", "-ac", "2", "-c:a", "libopus", "-b:a", "128k", "-f", "ogg", str(part)],
                               capture_output=True)
            if p.returncode != 0:
                raise RejectedAudio(f"encode failed: {_err(p)}")
            final = analyse(part)[0]
            if final < MIN_DURATION_S:
                raise RejectedAudio(f"too short after trimming: {final:.1f}s")
            os.replace(part, out_path)
        finally:
            part.unlink(missing_ok=True)
    return final
