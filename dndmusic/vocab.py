import logging
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from .models import STARTS, TEMPOS, Scene

log = logging.getLogger(__name__)
MIN_DURATION_S = 30
MAX_DURATION_S = 380


class VocabError(ValueError):
    """vocab.yaml is malformed; the message names the key."""


def _texts(d: dict, key: str) -> dict[str, str]:
    m = d.get(key)
    if not isinstance(m, dict) or not m:
        raise VocabError(f"vocab.yaml: '{key}' must be a non-empty mapping of name: text")
    for name, text in m.items():
        if not isinstance(text, str) or not text.strip():
            raise VocabError(f"vocab.yaml: '{key}.{name}' must be a non-empty string")
    return {str(k): v.strip() for k, v in m.items()}


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise VocabError(f"vocab.yaml: '{key}' must be a string")
    return v.strip()


def _duration(d: dict) -> tuple[int, int]:
    v = d.get("duration_s")
    ok = (isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in v)
          and MIN_DURATION_S <= v[0] <= v[1] <= MAX_DURATION_S)
    if not ok:
        raise VocabError(f"vocab.yaml: 'duration_s' must be two whole numbers [min, max] with "
                         f"{MIN_DURATION_S} <= min <= max <= {MAX_DURATION_S}")
    return v[0], v[1]


@dataclass(frozen=True)
class Vocab:
    locations: dict[str, str]
    moods: dict[str, str]
    tempos: dict[str, str]
    choir: str
    straight_in: str
    duration_s: tuple[int, int]

    @classmethod
    def from_dict(cls, d) -> "Vocab":
        if not isinstance(d, dict):
            raise VocabError("vocab.yaml: expected a mapping at the top level")
        tempos = _texts(d, "tempos")
        missing = [t for t in TEMPOS if t not in tempos]
        if missing:
            raise VocabError(f"vocab.yaml: 'tempos' is missing {', '.join(missing)}")
        return cls(_texts(d, "locations"), _texts(d, "moods"), tempos, _text(d, "choir"), _text(d, "straight_in"),
                   _duration(d))

    def validate(self, scene: Scene) -> None:
        if not scene.name.strip():
            raise ValueError("name is required")
        if scene.location not in self.locations:
            raise ValueError(f"unknown location {scene.location!r}")
        if scene.mood not in self.moods:
            raise ValueError(f"unknown mood {scene.mood!r}")
        if scene.tempo not in TEMPOS:
            raise ValueError(f"unknown tempo {scene.tempo!r}")
        if scene.start not in STARTS:
            raise ValueError(f"unknown start {scene.start!r}")


def check_scenes(vocab: Vocab, scenes) -> None:
    """Raise VocabError for the first saved scene that uses a key vocab.yaml doesn't define."""
    for sc in scenes:
        for what, value, known in (("location", sc.location, vocab.locations), ("mood", sc.mood, vocab.moods),
                                   ("tempo", sc.tempo, vocab.tempos), ("start", sc.start, STARTS)):
            if value not in known:
                raise VocabError(f"scene {sc.name!r} uses {what} {value!r}, which vocab.yaml doesn't define: "
                                 f"add it back to vocab.yaml or change the scene")


MERGE_SECTIONS = ("locations", "moods", "tempos")


def _merge_defaults(path: Path, default_text: str) -> None:
    """Add default entries missing from vocab.yaml (locations, moods, tempos), keeping the user's text and
    comments. The file is backed up as vocab.pre-merge.yaml first. A deleted entry comes back."""
    try:
        have = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return  # load_vocab reports it
    want = yaml.safe_load(default_text)
    if not isinstance(have, dict) or not isinstance(want, dict):
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    added: list[str] = []
    for sec in MERGE_SECTIONS:
        mine, theirs = have.get(sec), want.get(sec)
        if not isinstance(mine, dict) or not isinstance(theirs, dict):
            continue
        new = []
        for key in theirs:
            if key in mine:
                continue
            m = re.search(rf"^  {re.escape(str(key))}:.*$", default_text, re.M)
            if m:
                new.append((str(key), m.group(0)))
        if not new:
            continue
        start = next((i for i, ln in enumerate(lines) if re.match(rf"{sec}:", ln)), None)
        if start is None:
            continue
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"[A-Za-z_]", lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():  # insert before trailing blank lines
            end -= 1
        for key, line in new:
            lines.insert(end, line)
            end += 1
            added.append(f"{sec}.{key}")
    if not added:
        return
    backup = path.with_name("vocab.pre-merge.yaml")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.warning("vocab.yaml: added new defaults %s (previous file kept as %s)", ", ".join(added), backup.name)


def load_vocab(data_dir: Path) -> Vocab:
    path = Path(data_dir) / "vocab.yaml"
    if path.exists():
        try:
            d = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise VocabError(f"vocab.yaml is not valid YAML: {e}") from e
        if isinstance(d, dict) and "genres" in d and "locations" not in d:
            backup = path.with_name("vocab.m1.yaml")
            path.rename(backup)
            log.warning("vocab.yaml used the old genre/tone format: moved it to %s and wrote the new defaults",
                        backup.name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        default = resources.files("dndmusic") / "vocab_default.yaml"
        path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
    _merge_defaults(path, (resources.files("dndmusic") / "vocab_default.yaml").read_text(encoding="utf-8"))
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise VocabError(f"vocab.yaml is not valid YAML: {e}") from e
    return Vocab.from_dict(d)
