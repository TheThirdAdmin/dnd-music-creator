import pytest
import yaml

from dndmusic.models import Scene
from dndmusic.prompts import MAX_WORDS, scene_prompt
from dndmusic.starter import STARTER_SCENES
from dndmusic.vocab import Vocab, VocabError, load_vocab


@pytest.fixture
def vocab(tmp_path):
    return load_vocab(tmp_path)


def words(text):
    return len(text.split())


def test_default_vocab_is_written_and_loaded(tmp_path):
    v = load_vocab(tmp_path)
    assert (tmp_path / "vocab.yaml").exists()
    assert list(v.locations) == ["tavern", "town", "forest road", "mountain pass", "at sea", "castle hall",
                                 "dungeon", "crypt", "ancient ruins", "battlefield", "desert", "bardcore", "city", "sea battle", "cathedral",
                                 "circus", "nightclub", "temple"]
    assert list(v.moods) == ["calm", "mysterious", "tense", "dread", "combat", "boss fight", "triumphant", "lively", "farcical", "seductive", "sad"]
    assert list(v.tempos) == ["slow", "medium", "fast"]
    assert v.duration_s == (180, 240)


def test_user_edits_are_kept(tmp_path):
    load_vocab(tmp_path)
    p = tmp_path / "vocab.yaml"
    p.write_text(p.read_text().replace("Slow tempo.", "Very slow tempo."))
    assert load_vocab(tmp_path).tempos["slow"] == "Very slow tempo."


def test_m1_vocab_file_is_moved_aside(tmp_path, caplog):
    (tmp_path / "vocab.yaml").write_text(yaml.safe_dump({"genres": {"x": "y"}, "tones": {}, "duration_s": [120, 240]}))
    v = load_vocab(tmp_path)
    assert "tavern" in v.locations
    assert (tmp_path / "vocab.m1.yaml").exists()
    assert "vocab.m1.yaml" in caplog.text


def good():
    return {"locations": {"cave": "A cave."}, "moods": {"calm": "Calm."},
            "tempos": {"slow": "Slow.", "medium": "Medium.", "fast": "Fast."},
            "choir": "A choir.", "straight_in": "Full on.", "duration_s": [180, 240]}


@pytest.mark.parametrize("change,key", [
    (lambda d: d["locations"].update(cave=""), "locations.cave"),
    (lambda d: d["moods"].update(calm=3), "moods.calm"),
    (lambda d: d.pop("moods"), "moods"),
    (lambda d: d["tempos"].pop("fast"), "tempos"),
    (lambda d: d.update(choir=["a"]), "choir"),
    (lambda d: d.update(straight_in=None), "straight_in"),
    (lambda d: d.update(duration_s=[180]), "duration_s"),
    (lambda d: d.update(duration_s=[20, 240]), "duration_s"),
    (lambda d: d.update(duration_s=[240, 180]), "duration_s"),
    (lambda d: d.update(duration_s=[180, 400]), "duration_s"),
    (lambda d: d.update(duration_s=[180.5, 240]), "duration_s"),
])
def test_validation_names_the_key(change, key):
    d = good()
    change(d)
    with pytest.raises(VocabError, match=key.replace(".", r"\.")):
        Vocab.from_dict(d)


def test_prompt_order_and_parts(vocab):
    s = Scene(name="Ambush", location="battlefield", mood="combat", tempo="fast", choir=True, start="straight",
              extra="war horns")
    p = scene_prompt(s, vocab)
    parts = [vocab.straight_in, vocab.locations["battlefield"], vocab.moods["combat"], vocab.tempos["fast"],
             vocab.choir, "war horns.", "Instrumental."]
    positions = [p.tags.index(x) for x in parts]
    assert positions == sorted(positions)
    assert p.lyrics == "" and p.straight is True


def test_plain_scene_has_no_choir_or_straight_in(vocab):
    p = scene_prompt(Scene(name="x", location="tavern", mood="calm"), vocab)
    assert vocab.choir not in p.tags and vocab.straight_in not in p.tags
    assert p.tags.endswith("Instrumental.") and p.straight is False


def test_word_cap_cuts_vocab_sentences_but_keeps_extra(vocab):
    extra = " ".join(["drums"] * 40)
    s = Scene(name="x", location="battlefield", mood="combat", tempo="fast", choir=True, start="straight", extra=extra)
    p = scene_prompt(s, vocab)
    assert words(p.tags) <= MAX_WORDS
    assert extra + "." in p.tags and p.tags.endswith("Instrumental.")
    assert vocab.choir not in p.tags
    head = p.tags[: p.tags.index(extra)].strip()
    assert head == "" or head.endswith((".", "!", "?"))


def test_hash_ignores_name_but_not_choir_or_start(vocab):
    base = dict(location="crypt", mood="dread", tempo="slow")
    h = scene_prompt(Scene(name="a", **base), vocab).hash
    assert h == scene_prompt(Scene(name="other", **base), vocab).hash
    assert h != scene_prompt(Scene(name="a", choir=True, **base), vocab).hash
    assert h != scene_prompt(Scene(name="a", start="straight", **base), vocab).hash
    assert len(h) == 16


@pytest.mark.parametrize("field,value,msg", [
    ("name", " ", "name"),
    ("location", "swamp", "location"),
    ("mood", "sleepy", "mood"),
    ("tempo", "warp", "tempo"),
    ("start", "sideways", "start"),
])
def test_validate_rejects(vocab, field, value, msg):
    s = Scene(name="ok", location="tavern", mood="calm")
    setattr(s, field, value)
    with pytest.raises(ValueError, match=msg):
        vocab.validate(s)


@pytest.mark.parametrize("entry", STARTER_SCENES, ids=[s["slug"] for s in STARTER_SCENES])
def test_starter_scenes_fit_without_cutting(vocab, entry):
    s = Scene(**{k: v for k, v in entry.items() if k != "slug"})
    vocab.validate(s)
    p = scene_prompt(s, vocab)
    assert words(p.tags) <= MAX_WORDS
    if s.prompt:   # written by hand: used as is
        assert p.tags == " ".join(s.prompt.split())
        return
    assert vocab.locations[s.location] in p.tags and vocab.moods[s.mood] in p.tags
    assert (vocab.choir in p.tags) == s.choir


def test_advanced_prompt_is_used_as_written(tmp_path):
    vocab = load_vocab(tmp_path)
    s = Scene(name="x", location=next(iter(vocab.locations)), mood=next(iter(vocab.moods)),
              prompt=" Solo  tuba in a cave.\nInstrumental. ", start="straight")
    p = scene_prompt(s, vocab)
    assert p.tags == "Solo tuba in a cave. Instrumental." and p.straight
