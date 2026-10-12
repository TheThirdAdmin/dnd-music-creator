import re

from .models import Prompt, Scene
from .vocab import Vocab

MAX_WORDS = 70
CLOSING = "Instrumental."
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _words(text: str) -> int:
    return len(text.split())


def scene_prompt(scene: Scene, vocab: Vocab) -> Prompt:
    """[straight_in] location mood tempo [choir] extra Instrumental. At most MAX_WORDS words: over the cap,
    whole vocab sentences are dropped from the end of the vocab part; the extra text is always kept."""
    if scene.prompt.strip():   # advanced mode: used as written (whitespace tidied)
        return Prompt(tags=" ".join(scene.prompt.split()), lyrics="", straight=scene.start == "straight")
    head = [vocab.straight_in] if scene.start == "straight" else []
    head += [vocab.locations[scene.location], vocab.moods[scene.mood], vocab.tempos[scene.tempo]]
    if scene.choir:
        head.append(vocab.choir)
    sentences = [s for part in head for s in _SENTENCE_END.split(part.strip()) if s]
    extra = " ".join(scene.extra.split())
    if extra and extra[-1] not in ".!?":
        extra += "."
    budget = MAX_WORDS - _words(extra) - _words(CLOSING)
    kept: list[str] = []
    used = 0
    for s in sentences:
        if used + _words(s) > budget:
            break
        kept.append(s)
        used += _words(s)
    text = " ".join([*kept, *([extra] if extra else []), CLOSING])
    return Prompt(tags=text, lyrics="", straight=scene.start == "straight")
