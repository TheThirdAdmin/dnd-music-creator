from types import SimpleNamespace
import pytest

from dndmusic.caster import Caster, PyChromecastBackend, Device
from fakes import FakeBackend

URL = "http://dm:8000/stream.mp3"


def test_start_casts_and_sets_volume(lib):
    be = FakeBackend()
    c = Caster(be, lib, URL)
    c.start(["u1"])
    assert ("play", "u1", URL) in be.calls and ("volume", "u1", 0.5) in be.calls
    assert c.snapshot() == {"casting": True, "volume": 0.5,
                            "targets": [{"uuid": "u1", "name": "Lounge", "status": "playing"}]}


def test_watchdog_recasts_when_stopped_elsewhere(lib):
    be = FakeBackend()
    c = Caster(be, lib, URL)
    c.start(["u1"])
    be.playing["u1"] = False
    be.calls.clear()
    c.check()
    assert ("play", "u1", URL) in be.calls


def test_watchdog_quiet_after_stop(lib):
    be = FakeBackend()
    c = Caster(be, lib, URL)
    c.start(["u1"])
    c.stop()
    be.calls.clear()
    c.check()
    assert be.calls == [] and c.status["u1"] == "stopped"


def test_unknown_device(lib):
    c = Caster(FakeBackend(), lib, URL)
    c.start(["gone"])
    assert c.status["gone"].startswith("error")
    c.check()
    assert c.status["gone"] == "not found"


def test_switching_targets_stops_the_old_one(lib):
    be = FakeBackend()
    c = Caster(be, lib, URL)
    c.start(["u1"])
    c.start(["g1"])
    assert ("stop", "u1") in be.calls and c.targets == ["g1"]


def test_volume_clamped_and_state_restored(lib):
    c = Caster(FakeBackend(), lib, URL)
    c.start(["u1"])
    c.set_volume(1.7)
    assert c.volume == 1.0
    again = Caster(FakeBackend(), lib, URL)
    assert again.volume == 1.0 and again.casting and again.targets == ["u1"]


# PyChromecastBackend tests (no real network)

def test_pychromecast_foreign_cast_detected_by_content_id():
    """is_playing_ours is False when content_id differs from cast URL."""
    be = PyChromecastBackend.__new__(PyChromecastBackend)
    be._casts = {}
    be._urls = {}
    be._pc = None
    be._zconf = None
    be._browser = None

    # Create a fake Chromecast with matching app_id and player_state but wrong content_id
    fake_status = SimpleNamespace(app_id="CC1AD845", player_state="PLAYING")
    fake_mc_status = SimpleNamespace(content_id="http://different.url", player_state="PLAYING")
    fake_mc = SimpleNamespace(status=fake_mc_status)
    fake_cast = SimpleNamespace(status=fake_status, media_controller=fake_mc)

    uuid = "test-uuid"
    be._casts[uuid] = fake_cast
    be._urls[uuid] = URL

    assert be.is_playing_ours(uuid) is False


def test_pychromecast_our_cast_detected_by_content_id():
    """is_playing_ours is True when content_id matches cast URL."""
    be = PyChromecastBackend.__new__(PyChromecastBackend)
    be._casts = {}
    be._urls = {}
    be._pc = None
    be._zconf = None
    be._browser = None

    # Create a fake Chromecast with matching app_id, player_state, and content_id
    fake_status = SimpleNamespace(app_id="CC1AD845")
    fake_mc_status = SimpleNamespace(content_id=URL, player_state="PLAYING")
    fake_mc = SimpleNamespace(status=fake_mc_status)
    fake_cast = SimpleNamespace(status=fake_status, media_controller=fake_mc)

    uuid = "test-uuid"
    be._casts[uuid] = fake_cast
    be._urls[uuid] = URL

    assert be.is_playing_ours(uuid) is True


def test_pychromecast_cast_disconnects_on_wait_failure():
    """_cast disconnects Chromecast when wait() raises and does not cache it."""
    be = PyChromecastBackend.__new__(PyChromecastBackend)
    be._casts = {}
    be._urls = {}
    be._zconf = SimpleNamespace()
    be._browser = SimpleNamespace(devices={"uuid-x": SimpleNamespace(friendly_name="Test")})

    disconnect_called = []

    def mock_get_chromecast(info, zconf):
        fake_cast = SimpleNamespace()
        def failing_wait(timeout=None):
            raise RuntimeError("connection failed")
        fake_cast.wait = failing_wait
        def mock_disconnect():
            disconnect_called.append(True)
        fake_cast.disconnect = mock_disconnect
        return fake_cast

    be._pc = SimpleNamespace(get_chromecast_from_cast_info=mock_get_chromecast)

    with pytest.raises(RuntimeError):
        be._cast("uuid-x")

    assert disconnect_called, "disconnect() should be called"
    assert "uuid-x" not in be._casts, "failed cast should not be cached"


def test_pychromecast_stale_cache_dropped_on_is_playing_ours_error():
    """Exception in is_playing_ours drops the uuid from _casts."""
    be = PyChromecastBackend.__new__(PyChromecastBackend)
    be._casts = {}
    be._urls = {}

    uuid = "test-uuid"
    disconnect_called = []

    class FailingCast:
        def disconnect(self):
            disconnect_called.append(True)

        @property
        def status(self):
            raise RuntimeError("device disconnected")

    fake_cast = FailingCast()

    # Simulate an existing cached cast
    be._casts[uuid] = fake_cast
    be._urls[uuid] = URL

    # Mock _cast to return the cached one
    def mock_cast(u):
        return be._casts.get(u)
    be._cast = mock_cast

    with pytest.raises(RuntimeError):
        be.is_playing_ours(uuid)

    assert uuid not in be._casts, "stale cast should be removed from cache"
    assert disconnect_called, "disconnect should be called on stale cast"


def test_pychromecast_devices_with_none_friendly_name():
    """devices() uses uuid as name when friendly_name is None."""
    be = PyChromecastBackend.__new__(PyChromecastBackend)
    be._browser = SimpleNamespace(
        devices={
            "uuid-1": SimpleNamespace(friendly_name="Speaker", model_name="Google Home", cast_type="cast"),
            "uuid-2": SimpleNamespace(friendly_name=None, model_name="Google Group", cast_type="group"),
        }
    )

    devices = be.devices()

    # Should have 2 devices
    assert len(devices) == 2

    # Find the device with None friendly_name - should use uuid as name
    none_name_device = next((d for d in devices if d.uuid == "uuid-2"), None)
    assert none_name_device is not None
    assert none_name_device.name == "uuid-2", "Should use uuid as name when friendly_name is None"

    # Other device should have normal name
    normal_device = next((d for d in devices if d.uuid == "uuid-1"), None)
    assert normal_device is not None
    assert normal_device.name == "Speaker"
