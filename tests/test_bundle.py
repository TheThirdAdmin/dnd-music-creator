import json
import zipfile

import pytest

from dndmusic.bundle import BundleError, export_zip, import_zip
from dndmusic.ingest import RejectedAudio
from dndmusic.library import Library
from dndmusic.models import Prompt, Scene
from helpers import wav_bytes


def ok_check(path):
    return 100.0


def make_fav(lib, tags="t", lyrics="l", data=b"audio-1", straight=False, label_scene="Tavern", seed=5):
    s = lib.create_scene(Scene(name=label_scene, location="tavern", mood="calm"))
    pool = lib.pool_for(Prompt(tags, lyrics, straight), scene_id=s.id)
    p = lib.new_track_path()
    p.write_bytes(data)
    t = lib.add_track(pool.id, p, 100.0, seed, "http://gen", quality=3)
    lib.set_rating(t.id, "fav")
    return pool, t


def zip_with(path, manifest, files):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        for n, b in files.items():
            z.writestr(n, b)
    return path


def entry(name="files/a.opus", **kw):
    return {"file": name, "tags": "t", "lyrics": "l", "straight": False, "label": "Tavern", "seed": 1,
            "quality": 2, "duration_s": 100.0, "rating": "fav", "source": "g", **kw}


@pytest.fixture
def other(tmp_path):
    return Library(tmp_path / "other")


def test_round_trip_into_empty_library(lib, other, tmp_path):
    pool, t = make_fav(lib, straight=True)
    z = tmp_path / "b.zip"
    assert export_zip(lib, None, z, "abc") == 1
    r = import_zip(other, z, check=ok_check)
    assert r == {"added": 1, "duplicates": 0, "rejected": [], "pools_created": 1}
    (p, tracks), = other.favourites()
    assert (p.tags, p.lyrics, p.straight, p.label, p.pinned) == ("t", "l", True, "Tavern", True)
    assert tracks[0].seed == 5 and tracks[0].quality == 3 and open(tracks[0].path, "rb").read() == b"audio-1"
    assert str(tmp_path) not in json.dumps(json.loads(zipfile.ZipFile(z).read("manifest.json")))


def test_dedup_by_file_and_by_prompt(lib, other, tmp_path):
    make_fav(lib)
    z = tmp_path / "b.zip"
    export_zip(lib, None, z, None)
    assert import_zip(other, z, check=ok_check)["added"] == 1
    assert import_zip(other, z, check=ok_check) == {"added": 0, "duplicates": 1, "rejected": [], "pools_created": 0}
    z2 = zip_with(tmp_path / "c.zip", {"version": 1, "tracks": [entry()]}, {"files/a.opus": b"different"})
    r = import_zip(other, z2, check=ok_check)
    assert r["added"] == 1 and r["pools_created"] == 0 and len(other.favourites()) == 1
    assert len(other.favourites()[0][1]) == 2


def test_subset_export(lib, tmp_path):
    _, a = make_fav(lib, tags="a", data=b"1")
    _, b = make_fav(lib, tags="b", data=b"2", label_scene="Crypt")
    assert export_zip(lib, [b.id], tmp_path / "s.zip", None) == 1


@pytest.mark.parametrize("bad", ["../evil.opus", "/abs.opus", "files/../x.opus", "files/a.mp3", "files/sub/a.opus"])
def test_rejects_traversal_and_odd_names(other, tmp_path, bad):
    z = zip_with(tmp_path / "x.zip", {"version": 1, "tracks": [entry(bad)]}, {bad: b"x"})
    r = import_zip(other, z, check=ok_check)
    assert r["added"] == 0 and r["rejected"][0]["reason"] == "bad file name"
    assert other.favourites() == []


def test_rejects_bad_zip_version_oversize(other, tmp_path):
    junk = tmp_path / "j.zip"
    junk.write_bytes(b"not a zip")
    with pytest.raises(BundleError):
        import_zip(other, junk)
    v2 = zip_with(tmp_path / "v.zip", {"version": 2, "tracks": []}, {})
    with pytest.raises(BundleError, match="version"):
        import_zip(other, v2)
    big = zip_with(tmp_path / "b.zip", {"version": 1, "tracks": [entry()]}, {"files/a.opus": b"x" * 100})
    with pytest.raises(BundleError, match="large"):
        import_zip(other, big, max_bytes=50)


def test_undecodable_file_is_rejected_others_import(other, tmp_path):
    def check(path):
        if path.read_bytes() == b"bad":
            raise RejectedAudio("undecodable: x")
        return 100.0
    z = zip_with(tmp_path / "m.zip", {"version": 1, "tracks": [entry("files/a.opus"), entry("files/b.opus", tags="u")]},
                 {"files/a.opus": b"bad", "files/b.opus": b"good"})
    r = import_zip(other, z, check=check)
    assert r["added"] == 1 and r["rejected"] == [{"file": "files/a.opus", "reason": "undecodable: x"}]


def test_real_audio_check_rejects_garbage_and_short(other, tmp_path):
    z = zip_with(tmp_path / "r.zip", {"version": 1, "tracks": [entry("files/a.opus"), entry("files/b.opus", tags="u")]},
                 {"files/a.opus": b"garbage", "files/b.opus": wav_bytes(5)})
    r = import_zip(other, z)
    assert r["added"] == 0 and len(r["rejected"]) == 2
