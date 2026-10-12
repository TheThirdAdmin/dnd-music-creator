from importlib import resources

import yaml

from dndmusic.vocab import load_vocab


def _default() -> str:
    return (resources.files("dndmusic") / "vocab_default.yaml").read_text(encoding="utf-8")


def test_merge_adds_missing_defaults_and_keeps_edits(tmp_path):
    d = yaml.safe_load(_default())
    text = _default()
    for key in ("circus", "nightclub", "farcical"):
        text = "\n".join(ln for ln in text.splitlines() if not ln.startswith(f"  {key}:")) + "\n"
    text = text.replace("# After editing", "# MY NOTE\n# After editing", 1)
    text = text.replace('  crypt: "', '  crypt: "Mine, ', 1)
    (tmp_path / "vocab.yaml").write_text(text, encoding="utf-8")
    v = load_vocab(tmp_path)
    assert {"circus", "nightclub"} <= set(v.locations) and "farcical" in v.moods
    assert v.locations["circus"] == d["locations"]["circus"].strip()
    assert v.locations["crypt"].startswith("Mine, ")
    out = (tmp_path / "vocab.yaml").read_text(encoding="utf-8")
    assert "# MY NOTE" in out
    assert (tmp_path / "vocab.pre-merge.yaml").read_text(encoding="utf-8") == text


def test_merge_is_a_noop_when_up_to_date(tmp_path):
    load_vocab(tmp_path)  # first run writes the defaults
    before = (tmp_path / "vocab.yaml").read_text(encoding="utf-8")
    load_vocab(tmp_path)
    assert (tmp_path / "vocab.yaml").read_text(encoding="utf-8") == before
    assert not (tmp_path / "vocab.pre-merge.yaml").exists()
