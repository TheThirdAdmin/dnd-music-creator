from pathlib import Path

from dndmusic.config import Settings


def test_defaults_detect_public_url():
    s = Settings.from_env({})
    assert s.port == 8000
    assert s.public_url.startswith("http://") and s.public_url.endswith(":8000")
    assert s.generator_urls == ("http://localhost:8001",)
    assert s.data_dir == Path("/data")


def test_env_overrides():
    s = Settings.from_env({
        "PORT": "9000",
        "PUBLIC_URL": "http://dm.lan:9000/",
        "GENERATOR_URLS": "http://gpu1:8001/, http://gpu2:8001",
        "DATA_DIR": "/tmp/x",
    })
    assert s.port == 9000
    assert s.public_url == "http://dm.lan:9000"
    assert s.generator_urls == ("http://gpu1:8001", "http://gpu2:8001")
    assert s.data_dir == Path("/tmp/x")


def test_starter_dir(tmp_path):
    from dndmusic.config import Settings
    assert Settings.from_env({"PUBLIC_URL": "http://x"}).starter_dir is None
    assert Settings.from_env({"PUBLIC_URL": "http://x", "STARTER_DIR": ""}).starter_dir is None
    assert Settings.from_env({"PUBLIC_URL": "http://x", "STARTER_DIR": str(tmp_path)}).starter_dir == tmp_path


def test_host_data_path_for_display():
    from dndmusic.config import Settings
    assert Settings.from_env({"HOST_DATA_PATH": "/home/x/music"}).host_data_path == "/home/x/music"
    assert Settings.from_env({}).host_data_path is None
