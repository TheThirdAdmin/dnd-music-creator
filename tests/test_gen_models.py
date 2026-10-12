import hashlib
import os

import httpx
import pytest

from generator.config import GenSettings
from generator.modelfiles import MEDIUM, SMALL, T5, ModelError, ModelFile, ModelStore, needed


def payload(n, k):
    return bytes((i * k) % 251 for i in range(n))


def mf(name, data):
    return ModelFile("checkpoints", name, len(data), hashlib.sha256(data).hexdigest())


class Broken(httpx.AsyncByteStream):
    """A body that drops the connection after `data`."""

    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data
        raise httpx.ReadError("connection reset")


class Hub:
    """Serves files by name, honours Range unless told not to; records requests."""

    def __init__(self, files, ignore_range=False, break_after=None):
        self.files, self.ignore_range, self.break_after, self.requests = files, ignore_range, break_after, []

    def __call__(self, request):
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        data = self.files.get(name)
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if rng and not self.ignore_range:
            return httpx.Response(206, content=data[int(rng.split("=")[1].rstrip("-")):])
        if self.break_after is not None:
            return httpx.Response(200, stream=Broken(data[:self.break_after]))
        return httpx.Response(200, content=data)


def store(tmp_path, files, hub):
    client = httpx.AsyncClient(transport=httpx.MockTransport(hub))
    return ModelStore(tmp_path / "models", files, client, "https://hub.test")


A, B = payload(10_000, 7), payload(6_000, 13)
FA, FB = mf("a.safetensors", A), mf("b.safetensors", B)


async def test_downloads_verifies_and_remembers(tmp_path):
    hub = Hub({"a.safetensors": A, "b.safetensors": B})
    s = store(tmp_path, [FA, FB], hub)
    reports = []
    await s.ensure(reports.append)
    assert s.path(FA).read_bytes() == A and s.path(FB).read_bytes() == B
    assert hub.requests[0].url == "https://hub.test/Comfy-Org/stable-audio-3/resolve/main/checkpoints/a.safetensors"
    assert reports[0] == "downloading 0%" and "downloading 100%" in reports and "verifying a.safetensors" in reports
    assert s.verified(FA) and s.verified(FB)
    n = len(hub.requests)
    await s.ensure(reports.append)
    assert len(hub.requests) == n


async def test_interrupted_download_resumes(tmp_path):
    s = store(tmp_path, [FA], Hub({"a.safetensors": A}, break_after=4000))
    with pytest.raises(ModelError, match="will resume"):
        await s.ensure(lambda _: None)
    part = s.path(FA).with_name("a.safetensors.part")
    assert part.stat().st_size == 4000
    hub = Hub({"a.safetensors": A})
    s.client = httpx.AsyncClient(transport=httpx.MockTransport(hub))
    await s.ensure(lambda _: None)
    assert hub.requests[0].headers["range"] == "bytes=4000-"
    assert s.path(FA).read_bytes() == A and not part.exists()


async def test_server_ignoring_range_restarts_cleanly(tmp_path):
    s = store(tmp_path, [FA], Hub({"a.safetensors": A}, ignore_range=True))
    s.path(FA).parent.mkdir(parents=True)
    s.path(FA).with_name("a.safetensors.part").write_bytes(b"x" * 4000)
    await s.ensure(lambda _: None)
    assert s.path(FA).read_bytes() == A


async def test_checksum_mismatch_deletes_the_file(tmp_path):
    bad = bytes(len(A))
    s = store(tmp_path, [FA], Hub({"a.safetensors": bad}))
    with pytest.raises(ModelError, match="checksum"):
        await s.ensure(lambda _: None)
    assert not s.path(FA).exists()


async def test_existing_file_is_hashed_not_downloaded(tmp_path):
    hub = Hub({})
    s = store(tmp_path, [FA], hub)
    s.path(FA).parent.mkdir(parents=True)
    s.path(FA).write_bytes(A)
    await s.ensure(lambda _: None)
    assert hub.requests == [] and s.verified(FA)


async def test_a_changed_file_is_checked_again(tmp_path):
    s = store(tmp_path, [FA], Hub({"a.safetensors": A}))
    await s.ensure(lambda _: None)
    s.path(FA).write_bytes(bytes(len(A)))
    st = s.path(FA).stat()
    os.utime(s.path(FA), ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert not s.verified(FA)
    with pytest.raises(ModelError, match="checksum"):
        await s.ensure(lambda _: None)


def test_model_list():
    assert needed(gpu=False) == [SMALL, T5]
    assert needed(gpu=True) == [SMALL, T5, MEDIUM]
    assert MEDIUM.rel == "checkpoints/stable_audio_3_medium.safetensors" and MEDIUM.size == 9222116660
    assert MEDIUM.sha256.startswith("48d9c65e") and SMALL.sha256.startswith("da85866b") and T5.sha256.startswith("1e1eba25")


def test_settings_from_env(tmp_path):
    s = GenSettings.from_env({"GENERATOR_DEVICE": "CPU", "MODEL_DIR": str(tmp_path), "PORT": "9001"})
    assert s.device == "cpu" and s.gpu is False and s.model_dir == tmp_path and s.port == 9001
    assert GenSettings.from_env({}).gpu is True
    with pytest.raises(ValueError, match="GENERATOR_DEVICE"):
        GenSettings.from_env({"GENERATOR_DEVICE": "tpu"})
