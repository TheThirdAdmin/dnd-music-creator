from dndmusic.caster import Device


class FakeBackend:
    def __init__(self, devices=None):
        self._devices = devices or [Device("u1", "Lounge", "Google Home", False),
                                    Device("g1", "Downstairs", "Google Cast Group", True)]
        self.calls = []
        self.playing = {}

    def _known(self, uuid):
        return uuid in {d.uuid for d in self._devices}

    def devices(self):
        return list(self._devices)

    def play(self, uuid, url):
        if not self._known(uuid):
            raise KeyError(uuid)
        self.calls.append(("play", uuid, url))
        self.playing[uuid] = True

    def stop(self, uuid):
        self.calls.append(("stop", uuid))
        self.playing[uuid] = False

    def set_volume(self, uuid, volume):
        self.calls.append(("volume", uuid, volume))

    def is_playing_ours(self, uuid):
        if not self._known(uuid):
            return None
        return self.playing.get(uuid, False)
