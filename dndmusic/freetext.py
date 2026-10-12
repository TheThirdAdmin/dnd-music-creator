"""Free text -> unsaved Scene by keyword tables (M4). Pure and deterministic; an LLM version can replace
text_to_scene's body later. Tables: freetext_default.yaml, user copy <data_dir>/freetext.yaml."""
import re
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

from .models import TEMPOS, Scene
from .vocab import Vocab, VocabError

NAME_MAX = 40
_QUOTES = set("\"'`‘’‚‛“”„‟«»")
_WORD = re.compile(r"[a-z0-9]+")
_FILE = "freetext.yaml"


@dataclass(frozen=True)
class Rules:
    locations: tuple[tuple[tuple[str, ...], str], ...]
    moods: tuple[tuple[tuple[str, ...], str], ...]
    tempos: tuple[tuple[tuple[str, ...], str], ...]
    mood_tempo: dict
    choir_words: tuple[str, ...]
    choir_moods: tuple[str, ...]
    straight_moods: tuple[str, ...]
    straight_words: tuple[str, ...]
    defaults: dict
    extra_max_words: int = 9
    extra_max_chars: int = 120


def _err(msg: str) -> VocabError:
    return VocabError(f"{_FILE}: {msg}")


def _words(v, where: str) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(w, str) and w.strip() for w in v):
        raise _err(f"'{where}' must be a list of words")
    return tuple(" ".join(w.lower().split()) for w in v)


def _phrases(v, where: str) -> tuple[str, ...]:
    """Rule words normalised like the DM's text (quotes dropped, a-z0-9 tokens), so they can match it."""
    out = tuple(" ".join(_WORD.findall("".join(c for c in w if c not in _QUOTES).lower())) for w in _words(v, where))
    if not all(out):
        raise _err(f"'{where}' has a word with no letters or digits")
    return out


def _table(d: dict, key: str, target: str) -> tuple:
    rows = d.get(key, [])
    if not isinstance(rows, list):
        raise _err(f"'{key}' must be a list of {{when: [...], {target}: ...}}")
    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not isinstance(r.get(target), str):
            raise _err(f"'{key}[{i}]' needs 'when' and '{target}'")
        out.append((_phrases(r.get("when"), f"{key}[{i}].when"), r[target]))
    return tuple(out)


def _int(d: dict, key: str, default: int, top: int) -> int:
    v = d.get(key, default)
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= top:
        raise _err(f"'{key}' must be a whole number from 1 to {top}")
    return v


def rules_from_dict(d) -> Rules:
    if not isinstance(d, dict):
        raise _err("expected a mapping at the top level")
    for k in ("mood_tempo", "defaults"):
        if not isinstance(d.get(k, {}), dict):
            raise _err(f"'{k}' must be a mapping")
    choir = d.get("choir", {})
    if not isinstance(choir, dict):
        raise _err("'choir' must be a mapping with 'words'")
    return Rules(
        locations=_table(d, "locations", "location"), moods=_table(d, "moods", "mood"),
        tempos=_table(d, "tempos", "tempo"),
        mood_tempo={str(k): str(v) for k, v in (d.get("mood_tempo") or {}).items()},
        choir_words=_phrases(choir.get("words", []), "choir.words"),
        choir_moods=_words(d.get("choir_moods", []), "choir_moods"),
        straight_moods=_words(d.get("straight_moods", []), "straight_moods"),
        straight_words=_phrases(d.get("straight_words", []), "straight_words"),
        defaults={str(k): str(v) for k, v in (d.get("defaults") or {}).items()},
        extra_max_words=_int(d, "extra_max_words", 9, 20), extra_max_chars=_int(d, "extra_max_chars", 120, 200))


def load_rules(data_dir: Path) -> Rules:
    path = Path(data_dir) / _FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        default = resources.files("dndmusic") / "freetext_default.yaml"
        path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise _err(f"not valid YAML: {e}") from e
    except (OSError, UnicodeDecodeError) as e:
        raise _err(f"cannot be read: {e}") from e
    return rules_from_dict(d)


def clean(text: str) -> str:
    out = []
    for ch in text or "":
        if ch in _QUOTES:
            continue
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            continue
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def _cap_words(text: str, max_words: int, max_chars: int) -> str:
    kept: list[str] = []
    for w in text.split()[:max_words]:
        if len(" ".join([*kept, w])) > max_chars:
            break
        kept.append(w)
    return " ".join(kept)


def _has(padded: str, phrases) -> bool:
    return any(f" {p} " in padded for p in phrases)


def _first(padded: str, table, allowed) -> str | None:
    for when, target in table:
        if target in allowed and _has(padded, when):
            return target
    return None


def text_to_scene(text: str, vocab: Vocab, rules: Rules | None = None) -> Scene:
    if rules is None:
        rules = rules_from_dict(yaml.safe_load(
            (resources.files("dndmusic") / "freetext_default.yaml").read_text(encoding="utf-8")))
    cleaned = clean(text)
    if not cleaned:
        raise ValueError("describe the scene in a few words")
    padded = f" {' '.join(_WORD.findall(cleaned.lower()))} "

    loc = _first(padded, rules.locations, vocab.locations)
    if loc is None:
        want = rules.defaults.get("location", "bardcore")
        loc = want if want in vocab.locations else next(iter(vocab.locations))
    mood = _first(padded, rules.moods, vocab.moods)
    if mood is None:
        want = rules.defaults.get("mood", "calm")
        mood = want if want in vocab.moods else next(iter(vocab.moods))
    explicit = _first(padded, rules.tempos, TEMPOS)
    tempo = explicit or rules.mood_tempo.get(mood)
    if tempo not in TEMPOS:
        tempo = rules.defaults.get("tempo", "medium")
        if tempo not in TEMPOS:
            tempo = "medium"
    choir = mood.lower() in rules.choir_moods or _has(padded, rules.choir_words)
    straight = mood.lower() in rules.straight_moods or _has(padded, rules.straight_words)

    name = cleaned[:NAME_MAX].rstrip()
    name = name[0].upper() + name[1:]
    scene = Scene(name=name, location=loc, mood=mood, tempo=tempo, choir=choir,
                  start="straight" if straight else "build",
                  extra=_cap_words(cleaned, rules.extra_max_words, rules.extra_max_chars), saved=False)
    vocab.validate(scene)
    return scene
