"""Approved scene backgrounds in dndmusic/static/bg: manifest, budget, formats, promote script."""
import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

from dndmusic.starter import STARTER_SCENES

ROOT = Path(__file__).resolve().parents[1]
BG = ROOT / "dndmusic" / "static" / "bg"
MAX_FILE = 60 * 1024
MAX_TOTAL = 8 * 1024 * 1024


def _manifest():
    return json.loads((BG / "manifest.json").read_text())


def _listed(m):
    return ([p for s in m["sets"].values() for p in s] + m["generic"] + list(m["icons"].values()))


def _promote():
    spec = importlib.util.spec_from_file_location("promote_backgrounds", ROOT / "scripts" / "promote_backgrounds.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_manifest_paths_exist_and_no_orphans():
    listed = _listed(_manifest())
    assert len(listed) == len(set(listed))
    for p in listed:
        assert (BG / p).is_file(), p
    on_disk = {str(f.relative_to(BG)) for f in BG.rglob("*") if f.is_file()} - {"manifest.json"}
    assert on_disk == set(listed)


def test_counts():
    m = _manifest()
    for s in STARTER_SCENES:
        art = s.get("icon", s["slug"])   # a starter scene may borrow another scene's art
        assert len(m["sets"].get(art, [])) == 4, s["slug"]
        assert art in m["icons"], s["slug"]
    assert set(m["sets"]) >= {s.get("icon", s["slug"]) for s in STARTER_SCENES}   # picker-only sets are extra
    assert set(m["icons"]) == set(m["sets"])
    assert all(len(v) == 4 for v in m["sets"].values())
    assert len(m["generic"]) == 12


def test_sizes_and_dimensions():
    m = _manifest()
    total = 0
    for p in _listed(m):
        f = BG / p
        n = f.stat().st_size
        total += n
        assert n <= MAX_FILE, f"{p} {n} bytes"
        im = Image.open(f)
        assert im.format == "WEBP", p
        want = (64, 64) if p.startswith("icons/") else (512, 512)
        assert im.size == want, p
    assert total <= MAX_TOTAL, total


def test_desktop_set_matches_manifest():
    """bg-hd holds a 1920x1080 copy of every background (not icons) at the same relative path."""
    hd = BG.parent / "bg-hd"
    m = _manifest()
    want = {p for s in m["sets"].values() for p in s} | set(m["generic"])
    have = {str(f.relative_to(hd)) for f in hd.rglob("*.webp")}
    assert have == want
    total = 0
    for p in want:
        total += (hd / p).stat().st_size
        assert Image.open(hd / p).size == (1920, 1080), p
    assert total <= 20 * 1024 * 1024, total


def test_not_lfs():
    ga = (ROOT / ".gitattributes").read_text()
    assert "static/bg" not in ga and "webp" not in ga


def test_promote_precedence_and_only(tmp_path, monkeypatch, capsys):
    promote = _promote()
    monkeypatch.setattr(promote, "ALLOWED", {"a", "generic", "icons"})
    r1, r2, bg = tmp_path / "r1", tmp_path / "r2", tmp_path / "bg"
    for d in (r1 / "a", r2 / "a", r1 / "generic", r1 / "icons", r1 / "notes-dir"):
        d.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(r1 / "a" / "1.webp")
    Image.new("RGB", (8, 8), "red").save(r1 / "a" / "2.webp")
    Image.new("RGB", (8, 8), "blue").save(r2 / "a" / "2.webp")
    Image.new("RGB", (8, 8), "red").save(r1 / "generic" / "01.webp")
    Image.new("RGB", (8, 8), "red").save(r1 / "icons" / "a.webp")
    (r1 / "notes.txt").write_text("x")
    bg.mkdir()
    assert promote.main([str(r1), str(r2)], bg) == 0
    assert (bg / "a" / "2.webp").read_bytes() == (r2 / "a" / "2.webp").read_bytes()
    assert (bg / "a" / "1.webp").read_bytes() == (r1 / "a" / "1.webp").read_bytes()
    m = json.loads((bg / "manifest.json").read_text())
    assert m == {"sets": {"a": ["a/1.webp", "a/2.webp"]}, "generic": ["generic/01.webp"],
                 "icons": {"a": "icons/a.webp"}}
    capsys.readouterr()
    promote.main([str(r1), str(r2)], bg)
    assert "added 0, replaced 0" in capsys.readouterr().out
    # a stray subdir with a webp is rejected, nothing copied for it
    (r2 / "stray").mkdir()
    Image.new("RGB", (8, 8), "red").save(r2 / "stray" / "1.webp")
    with pytest.raises(SystemExit) as e:
        promote.main([str(r1), str(r2)], bg)
    assert "stray" in str(e.value)
    assert not (bg / "stray").exists()
    (r2 / "stray" / "1.webp").unlink()
    # --only touches just the named image
    Image.new("RGB", (8, 8), "green").save(r2 / "a" / "1.webp")
    promote.main([str(r1), str(r2), "--only", "a:2"], bg)
    assert (bg / "a" / "1.webp").read_bytes() == (r1 / "a" / "1.webp").read_bytes()


def _sc(name, location, mood):
    from dndmusic.models import Scene
    return Scene(name=name, location=location, mood=mood)


def test_art_for_rules():
    from dndmusic.starter import art_for
    assert art_for(_sc("Boss  Fight!", "tavern", "calm")) == "boss-fight"
    assert art_for(_sc("Ambush", "sea battle", "combat")) == "sea-battle"
    assert art_for(_sc("Ambush", "castle hall", "triumphant")) == "victory"
    assert art_for(_sc("Mourning", "castle hall", "sad")) in {"boss-fight", "victory"}
    assert art_for(_sc("Weird", "swamp", "calm")) is None


def test_art_for_slugs_exist_in_manifest():
    from dndmusic.starter import art_for
    sets = _manifest()["sets"]
    for s in STARTER_SCENES:
        assert art_for(_sc("x", s["location"], s["mood"])) in sets
