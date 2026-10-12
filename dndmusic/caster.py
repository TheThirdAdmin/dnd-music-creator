import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from .library import Library

log = logging.getLogger(__name__)

MEDIA_RECEIVER_APP_ID = "CC1AD845"


@dataclass
class Device:
    uuid: str
    name: str
    model: str
    is_group: bool


class CastBackend(Protocol):
    def devices(self) -> list[Device]: ...
    def play(self, uuid: str, url: str) -> None: ...
    def stop(self, uuid: str) -> None: ...
    def set_volume(self, uuid: str, volume: float) -> None: ...
    def is_playing_ours(self, uuid: str) -> bool | None: ...


class PyChromecastBackend:
    """Chromecast devices and Google speaker groups found by mDNS (needs host networking)."""

    def __init__(self):
        import pychromecast
        import zeroconf
        from pychromecast.discovery import CastBrowser, SimpleCastListener

        self._pc = pychromecast
        self._zconf = zeroconf.Zeroconf()
        self._browser = CastBrowser(SimpleCastListener(), self._zconf)
        self._browser.start_discovery()
        self._casts = {}
        self._urls = {}

    def devices(self) -> list[Device]:
        found = [Device(str(u), i.friendly_name or str(u), i.model_name or "", i.cast_type == "group")
                 for u, i in list(self._browser.devices.items())]
        return sorted(found, key=lambda d: d.name.lower())

    def _cast(self, uuid: str):
        cast = self._casts.get(uuid)
        if cast is None:
            info = next((i for u, i in list(self._browser.devices.items()) if str(u) == uuid), None)
            if info is None:
                return None
            cast = self._pc.get_chromecast_from_cast_info(info, self._zconf)
            try:
                cast.wait(timeout=10)
            except Exception:
                try:
                    cast.disconnect()
                except Exception:
                    pass
                raise
            self._casts[uuid] = cast
        return cast

    def play(self, uuid: str, url: str) -> None:
        try:
            cast = self._cast(uuid)
            if cast is None:
                raise KeyError(uuid)
            mc = cast.media_controller
            mc.play_media(url, "audio/mpeg", title="D&D music", stream_type="LIVE")
            mc.block_until_active(timeout=10)
            self._urls[uuid] = url
        except Exception:
            cast = self._casts.pop(uuid, None)
            if cast is not None:
                try:
                    cast.disconnect()
                except Exception:
                    pass
            raise

    def stop(self, uuid: str) -> None:
        cast = self._cast(uuid)
        if cast is not None:
            cast.quit_app()

    def set_volume(self, uuid: str, volume: float) -> None:
        cast = self._cast(uuid)
        if cast is not None:
            cast.set_volume(volume)

    def is_playing_ours(self, uuid: str) -> bool | None:
        try:
            cast = self._cast(uuid)
            if cast is None:
                return None
            if cast.status is None or cast.status.app_id != MEDIA_RECEIVER_APP_ID:
                return False
            if cast.media_controller.status.content_id != self._urls.get(uuid):
                return False
            return cast.media_controller.status.player_state in ("PLAYING", "BUFFERING")
        except Exception:
            cast = self._casts.pop(uuid, None)
            if cast is not None:
                try:
                    cast.disconnect()
                except Exception:
                    pass
            raise


class Caster:
    """Keeps the chosen Cast targets playing the stream; state survives restarts."""

    def __init__(self, backend: CastBackend, lib: Library, stream_url: str):
        self.backend = backend
        self.lib = lib
        self.stream_url = stream_url
        self.targets: list[str] = lib.get_state("cast_targets", [])
        self.casting: bool = lib.get_state("casting", False)
        self.volume: float = lib.get_state("volume", 0.5)
        self.status: dict[str, str] = {}
        self._lock = threading.Lock()

    def _save(self) -> None:
        self.lib.set_state("cast_targets", self.targets)
        self.lib.set_state("casting", self.casting)
        self.lib.set_state("volume", self.volume)

    def _play(self, uuid: str) -> None:
        try:
            self.backend.play(uuid, self.stream_url)
            self.backend.set_volume(uuid, self.volume)
            self.status[uuid] = "playing"
        except Exception as e:
            log.warning("cast to %s failed: %r", uuid, e)
            self.status[uuid] = f"error: {e}"

    def _stop_one(self, uuid: str) -> None:
        try:
            self.backend.stop(uuid)
        except Exception as e:
            log.warning("stop %s failed: %r", uuid, e)
        self.status[uuid] = "stopped"

    def devices(self):
        return self.backend.devices()

    def start(self, targets: list[str]) -> None:
        with self._lock:
            for uuid in self.targets:
                if uuid not in targets:
                    self._stop_one(uuid)
            self.targets = list(targets)
            self.casting = True
            self._save()
            for uuid in self.targets:
                self._play(uuid)

    def stop(self) -> None:
        with self._lock:
            self.casting = False
            self._save()
            for uuid in self.targets:
                self._stop_one(uuid)

    def set_volume(self, volume: float) -> None:
        with self._lock:
            self.volume = min(1.0, max(0.0, float(volume)))
            self._save()
            if self.casting:
                for uuid in self.targets:
                    try:
                        self.backend.set_volume(uuid, self.volume)
                    except Exception as e:
                        self.status[uuid] = f"error: {e}"

    def check(self) -> None:
        """Watchdog tick: re-cast any target that stopped playing our stream."""
        with self._lock:
            if not self.casting:
                return
            for uuid in self.targets:
                try:
                    ok = self.backend.is_playing_ours(uuid)
                except Exception as e:
                    self.status[uuid] = f"error: {e}"
                    continue
                if ok is None:
                    self.status[uuid] = "not found"
                elif ok:
                    self.status[uuid] = "playing"
                else:
                    log.info("re-casting to %s", uuid)
                    self._play(uuid)

    def snapshot(self) -> dict:
        try:
            names = {d.uuid: d.name for d in self.backend.devices()}
        except Exception:
            names = {}
        return {"casting": self.casting, "volume": self.volume,
                "targets": [{"uuid": u, "name": names.get(u, u), "status": self.status.get(u, "idle")}
                            for u in self.targets]}
