#!/usr/bin/env python3
"""Duration and integrated loudness (ffmpeg ebur128) per audio file. Exit 1 if any track is shorter than 30 s
or outside -16 LUFS ±1.   scripts/check_tracks.py data/tracks/*.opus"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dndmusic.ingest import MIN_DURATION_S, TARGET_LUFS, analyse, loudness  # noqa: E402

TOLERANCE_LU = 1.0


def main(argv: list[str] | None = None) -> int:
    paths = [Path(a) for a in (sys.argv[1:] if argv is None else argv)]
    if not paths:
        print(__doc__)
        return 2
    bad = 0
    for p in paths:
        duration = analyse(p)[0]
        lufs, _ = loudness(p)
        ok = duration >= MIN_DURATION_S and abs(lufs - TARGET_LUFS) <= TOLERANCE_LU
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {p.name}  {duration:6.1f} s  {lufs:6.1f} LUFS")
    print(f"{len(paths) - bad}/{len(paths)} ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
