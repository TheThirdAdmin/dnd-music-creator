import logging

import yaml

from dndmusic.library import Library
from dndmusic.prompts import scene_prompt
from dndmusic.starter import LFS_POINTER, import_starter, is_lfs_pointer
from dndmusic.vocab import load_vocab

POINTER = LFS_POINTER + b"\noid sha256:abc\nsize 3500000\n"


def make_pack(d):
    d.mkdir()
    (d / "tavern-night-1.opus").write_bytes(b"OggS" + b"\0" * 2000)
    (d / "tavern-night-2.opus").write_bytes(POINTER)
    (d / "crypt-1.opus").write_bytes(POINTER)
    spec = {"model": "stable_audio_3_medium", "quality": 2, "scenes": [
        {"name": "Tavern night", "location": "tavern", "mood": "calm", "tempo": "medium", "choir": False,
         "start": "build", "tracks": [{"file": "tavern-night-1.opus", "seed": 11, "duration_s": 201.4},
                                      {"file": "tavern-night-2.opus", "seed": 12, "duration_s": 190.0}]},
        {"name": "Crypt", "location": "crypt", "mood": "dread", "tempo": "slow", "choir": False, "start": "build",
         "tracks": [{"file": "crypt-1.opus", "seed": 21, "duration_s": 230.0}]},
    ]}
    (d / "starter.yaml").write_text(yaml.safe_dump(spec))
    return d


def test_pointer_detection(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(POINTER)
    assert is_lfs_pointer(p)
    p.write_bytes(b"OggS....")
    assert not is_lfs_pointer(p)


def test_import_creates_scenes_and_attaches_tracks(tmp_path, caplog):
    pack = make_pack(tmp_path / "starter")
    lib = Library(tmp_path / "lib")
    vocab = load_vocab(lib.data_dir)
    with caplog.at_level(logging.WARNING):
        assert import_starter(lib, vocab, pack) == 1
    scenes = {s.name: s for s in lib.list_scenes()}
    assert set(scenes) == {"Tavern night", "Crypt"}
    pool = lib.pool_for(scene_prompt(scenes["Tavern night"], vocab), scene_id=scenes["Tavern night"].id)
    [track] = lib.tracks(pool.id)
    assert track.seed == 11 and track.duration_s == 201.4 and track.quality == 2
    assert track.generator == "starter:stable_audio_3_medium"
    assert track.path.startswith(str(lib.tracks_dir)) and (pack / "tavern-night-1.opus").exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "git lfs pull" in warnings[0].getMessage()


def test_deleted_starter_scenes_stay_deleted(tmp_path):
    pack = make_pack(tmp_path / "starter")
    lib = Library(tmp_path / "lib")
    vocab = load_vocab(lib.data_dir)
    import_starter(lib, vocab, pack)
    for s in lib.list_scenes():
        lib.delete_scene(s.id)
    assert import_starter(lib, vocab, pack) == 0 and lib.list_scenes() == []


def test_existing_library_gets_the_missing_scenes_only(tmp_path):
    from dndmusic.models import Scene
    pack = make_pack(tmp_path / "starter")
    lib = Library(tmp_path / "lib")
    vocab = load_vocab(lib.data_dir)
    mine = lib.create_scene(Scene(name="Crypt", location="town", mood="calm"))
    lib.set_state("starter_imported", True)  # an install from before per-scene import
    assert import_starter(lib, vocab, pack) == 1
    scenes = {s.name: s for s in lib.list_scenes()}
    assert set(scenes) == {"Tavern night", "Crypt"}
    assert scenes["Crypt"].id == mine.id and scenes["Crypt"].location == "town"
    assert import_starter(lib, vocab, pack) == 0 and len(lib.list_scenes()) == 2


def test_missing_pack_is_fine(tmp_path):
    lib = Library(tmp_path / "lib")
    assert import_starter(lib, load_vocab(lib.data_dir), tmp_path / "nope") == 0
