import pytest
import yaml

from dndmusic import freetext
from dndmusic.freetext import load_rules, rules_from_dict, text_to_scene
from dndmusic.prompts import CLOSING, MAX_WORDS, scene_prompt
from dndmusic.vocab import Vocab, VocabError, load_vocab


@pytest.fixture
def vocab(tmp_path):
    return load_vocab(tmp_path / "v")


def t2s(text, vocab, **kw):
    return text_to_scene(text, vocab, **kw)


def test_headline_example(vocab):
    sc = t2s("sea battle against pirates at dusk", vocab)
    assert (sc.location, sc.mood, sc.tempo, sc.start) == ("sea battle", "combat", "fast", "straight")
    assert sc.saved is False and sc.id is None
    assert sc.extra == "sea battle against pirates at dusk"


def test_unmapped_goes_to_defaults(vocab):
    sc = t2s("xyzzy", vocab)
    assert (sc.location, sc.mood, sc.tempo, sc.start, sc.choir) == ("bardcore", "calm", "medium", "build", False)


@pytest.mark.parametrize("text,loc", [
    ("a night at the inn", "tavern"), ("quiet pub", "tavern"), ("graveyard at midnight", "crypt"),
    ("an ancient tomb", "crypt"), ("a damp cave", "dungeon"), ("the cellar door", "dungeon"),
    ("on the ocean", "at sea"), ("aboard a ship", "at sea"), ("pirate ship ahoy", "sea battle"),
    ("throne room audience", "castle hall"), ("the palace", "castle hall"), ("a small village", "town"),
    ("busy market", "town"), ("a wood trail", "forest road"), ("crossing the desert", "desert"),
    ("high mountain pass", "mountain pass"), ("siege of the keep", "battlefield"), ("old ruins", "ancient ruins"),
    ("a cathedral", "cathedral"), ("the temple", "temple"), ("capital city", "city"),
    ("a circus brawl", "circus"), ("a succubus in a nightclub", "nightclub"),
])
def test_locations(vocab, text, loc):
    assert t2s(text, vocab).location == loc


def test_every_vocab_location_is_reachable(vocab):
    from importlib import resources
    d = yaml.safe_load((resources.files("dndmusic") / "freetext_default.yaml").read_text())
    assert {r["location"] for r in d["locations"]} >= set(vocab.locations)


@pytest.mark.parametrize("text,mood", [
    ("boss fight with a lich", "boss fight"), ("an ambush on the road", "combat"), ("a duel at dawn", "combat"),
    ("haunted halls", "dread"), ("tense negotiation", "tense"), ("a sad farewell", "sad"),
    ("victory feast", "triumphant"), ("a strange glow", "mysterious"), ("rowdy dancing", "lively"),
    ("quiet campfire", "calm"),
])
def test_moods(vocab, text, mood):
    assert t2s(text, vocab).mood == mood


@pytest.mark.parametrize("text,tempo", [
    ("a slow walk", "slow"), ("frantic escape", "fast"), ("a chase through town", "fast"),
    ("boss fight", "fast"), ("haunted halls", "slow"), ("a sad farewell", "slow"), ("rowdy dancing", "fast"),
    ("quiet campfire", "medium"), ("a slow battle", "slow"),
])
def test_tempos(vocab, text, tempo):
    assert t2s(text, vocab).tempo == tempo


@pytest.mark.parametrize("text,choir", [
    ("holy chant", True), ("boss fight", True), ("a dragon", True), ("funeral", True), ("victory", True),
    ("an ambush", True), ("quiet campfire", False), ("rowdy tavern", False),
])
def test_choir(vocab, text, choir):
    assert t2s(text, vocab).choir is choir


@pytest.mark.parametrize("text,start", [
    ("boss fight", "straight"), ("a battle", "straight"), ("suddenly the door opens", "straight"),
    ("quiet campfire", "build"), ("haunted halls", "build"),
])
def test_start(vocab, text, start):
    assert t2s(text, vocab).start == start


def test_deterministic(vocab):
    assert t2s("Sea battle at dusk", vocab) == t2s("Sea battle at dusk", vocab)


def test_whole_word_only(vocab):
    assert t2s("tavernous", vocab).location == "bardcore"
    assert t2s("an innkeeper", vocab).location == "bardcore"
    assert t2s("the inn.", vocab).location == "tavern"
    assert t2s("TAVERN", vocab).location == "tavern"


def test_first_match_wins_specific_before_generic(vocab):
    assert t2s("pirate ship in the sea", vocab).location == "sea battle"
    assert t2s("a ship at sea", vocab).location == "at sea"


def test_extra_capped_to_9_words_and_120_chars(vocab):
    sc = t2s(" ".join(f"w{i}" for i in range(30)), vocab)
    assert sc.extra == " ".join(f"w{i}" for i in range(9))
    r = rules_from_dict({"extra_max_words": 12, "extra_max_chars": 120})
    sc = t2s(" ".join(["abcdefghij"] * 14), vocab, rules=r)  # 12 words would be 131 chars
    assert len(sc.extra) <= 120 and sc.extra == " ".join(["abcdefghij"] * 11)


def test_name_capped_and_capitalised(vocab):
    sc = t2s("the dragon wakes under the mountain and everyone panics loudly", vocab)
    assert sc.name[0] == "T" and len(sc.name) <= 40 and sc.name == sc.name.strip()


def test_control_chars_and_quotes_stripped(vocab):
    sc = t2s('  a\x00 "fight"\n\tin `the`   “inn”\x07 ', vocab)
    assert sc.extra == "a fight in the inn" and sc.name == "A fight in the inn"
    assert sc.location == "tavern"


@pytest.mark.parametrize("bad", ["", "   ", '"" `', "\x00\x01"])
def test_empty_text_rejected(vocab, bad):
    with pytest.raises(ValueError, match="describe the scene in a few words"):
        t2s(bad, vocab)


def test_user_vocab_without_mapped_location_uses_default(vocab):
    d = {"locations": {"hall": "A hall."}, "moods": {"gloom": "Gloomy."}, "tempos": dict(vocab.tempos),
         "choir": "Choir.", "straight_in": "Straight.", "duration_s": [180, 240]}
    v = Vocab.from_dict(d)
    sc = t2s("battle at the inn", v)
    assert (sc.location, sc.mood) == ("hall", "gloom")
    v.validate(sc)


def test_user_vocab_missing_mood_falls_back_to_next_rule(vocab):
    d = {"locations": dict(vocab.locations), "moods": {k: v for k, v in vocab.moods.items() if k != "combat"},
         "tempos": dict(vocab.tempos), "choir": "c", "straight_in": "s", "duration_s": [180, 240]}
    v = Vocab.from_dict(d)
    sc = t2s("an ambush in the haunted crypt", v)
    assert sc.mood == "dread"
    v.validate(sc)


def test_result_always_validates(vocab):
    for text in ["xyzzy", "boss fight at sea", "funeral in the temple", "chase", "a b c"]:
        vocab.validate(t2s(text, vocab))


def test_prompt_budget_every_location(vocab):
    """Long extra, every location, every mood/tempo/start the rules can emit: <= 70 words, closing kept, and
    mood + tempo survive. Exception: the long default locations in straight scenes lose them to
    scene_prompt's trimming at any cap (noted in freetext_default.yaml)."""
    LONG_LOCATIONS = {"tavern", "bardcore", "at sea", "forest road", "town", "city", "circus", "nightclub"}
    extra = " ".join(["word"] * 40)
    combos = [(m, t, s) for m, t in (("combat", "fast"), ("boss fight", "fast"), ("tense", "medium"),
                                     ("lively", "fast"), ("triumphant", "medium"), ("dread", "slow"),
                                     ("mysterious", "medium"), ("sad", "slow"), ("calm", "medium"))
              for s in (("straight", "build") if m in ("combat", "boss fight") else ("build",))]
    for loc in vocab.locations:
        for mood, tempo, start in combos:
            sc = t2s(f"{loc} {extra}", vocab)
            sc = sc.__class__(**{**sc.__dict__, "location": loc, "mood": mood, "tempo": tempo, "start": start,
                                 "choir": True})
            p = scene_prompt(sc, vocab).tags
            assert len(p.split()) <= MAX_WORDS and p.endswith(CLOSING), (loc, mood)
            if loc in LONG_LOCATIONS and start == "straight":
                continue
            for sentence in (vocab.moods[mood], vocab.tempos[tempo]):
                assert sentence in p, (loc, mood, tempo, start, sentence)


def test_load_rules_copies_default_and_reads_user_copy(tmp_path):
    r = load_rules(tmp_path)
    assert (tmp_path / "freetext.yaml").exists() and r.defaults["location"] == "bardcore"
    (tmp_path / "freetext.yaml").write_text(
        (tmp_path / "freetext.yaml").read_text().replace("extra_max_words: 9", "extra_max_words: 5"))
    assert load_rules(tmp_path).extra_max_words == 5


def test_bad_yaml_names_the_file(tmp_path):
    (tmp_path / "freetext.yaml").write_text("locations: [unclosed")
    with pytest.raises(VocabError, match="freetext.yaml"):
        load_rules(tmp_path)
    (tmp_path / "freetext.yaml").write_text("locations: 3\n")
    with pytest.raises(VocabError, match="freetext.yaml"):
        load_rules(tmp_path)


def test_user_rules_passed_in(vocab, tmp_path):
    (tmp_path / "freetext.yaml").write_text("locations: [{when: [zorp], location: desert}]\n"
                                            "moods: []\ntempos: []\n")
    r = load_rules(tmp_path)
    assert t2s("zorp", vocab, rules=r).location == "desert"
    assert freetext  # module imported


def test_rule_words_with_apostrophes_hyphens_and_accents_match(vocab):
    from dndmusic.freetext import rules_from_dict
    r = rules_from_dict({"moods": [{"when": ["dragon's lair", "half-orc", "café"], "mood": "tense"}]})
    v = vocab
    for t in ("the dragon's lair", "half-orc camp", "café at night"):
        assert text_to_scene(t, v, r).mood == "tense"


def test_extra_caps_are_bounded_and_unreadable_file_is_a_vocab_error(tmp_path):
    import pytest
    from dndmusic.freetext import rules_from_dict, load_rules
    from dndmusic.vocab import VocabError
    for k, v in (("extra_max_chars", 250), ("extra_max_words", 99)):
        with pytest.raises(VocabError):
            rules_from_dict({k: v})
    (tmp_path / "freetext.yaml").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(VocabError):
        load_rules(tmp_path)
