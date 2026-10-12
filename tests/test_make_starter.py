import random

import httpx
import pytest
import yaml

from dndmusic.genclient import GeneratorClient
from dndmusic.ingest import ingest
from dndmusic.library import Library
from dndmusic.starter import STARTER_SCENES, import_starter
from dndmusic.vocab import load_vocab
from fakegen.app import create_app as fake_app
from helpers import wav_bytes
from scripts.check_tracks import main as check_main
from scripts.make_starter import make, preview_scenes


def client():
    return GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_app(delay_s=0, max_duration_s=31)), base_url="http://fake"))


def quiet(*_):
    pass


def scenes(*slugs):
    return [s for s in STARTER_SCENES if s["slug"] in slugs]


async def test_make_then_import(tmp_path):
    vocab = load_vocab(tmp_path / "v")
    out = tmp_path / "pack"
    spec = await make(client(), vocab, out, scenes("tavern-night", "combat"), tracks=2, quality=2,
                      rng=random.Random(1), poll_s=0.01, log=quiet)
    assert sorted(p.name for p in out.glob("*.opus")) == ["combat-1.opus", "combat-2.opus",
                                                          "tavern-night-1.opus", "tavern-night-2.opus"]
    assert [s["name"] for s in spec["scenes"]] == ["Tavern night", "Combat"]
    assert spec["scenes"][1]["start"] == "straight" and spec["scenes"][1]["choir"] is True
    assert all(29 <= t["duration_s"] <= 32 for s in spec["scenes"] for t in s["tracks"])
    assert (out / "starter.yaml").exists() and "combat:" in (out / "PROMPTS.txt").read_text()
    lib = Library(tmp_path / "lib")
    assert import_starter(lib, vocab, out) == 4


async def test_only_redoes_the_named_tracks(tmp_path):
    vocab = load_vocab(tmp_path / "v")
    out = tmp_path / "pack"
    first = await make(client(), vocab, out, scenes("crypt"), tracks=2, quality=2, rng=random.Random(1),
                       poll_s=0.01, log=quiet)
    again = await make(client(), vocab, out, scenes("crypt"), tracks=2, quality=2, rng=random.Random(2),
                       poll_s=0.01, only={"crypt-2"}, log=quiet)
    a, b = first["scenes"][0]["tracks"], again["scenes"][0]["tracks"]
    assert a[0] == b[0] and a[1]["seed"] != b[1]["seed"]


def test_preview_has_one_scene_per_location(tmp_path):
    vocab = load_vocab(tmp_path)
    ps = preview_scenes(vocab)
    assert [s["location"] for s in ps] == list(vocab.locations)
    assert next(s for s in ps if s["location"] == "desert")["mood"] == "calm"
    assert next(s for s in ps if s["location"] == "battlefield")["slug"] == "combat"


def test_check_tracks(tmp_path, capsys):
    ingest(wav_bytes(35), tmp_path / "ok.opus")
    (tmp_path / "loud.wav").write_bytes(wav_bytes(35, amp=0.9))
    assert check_main([str(tmp_path / "ok.opus")]) == 0
    assert check_main([str(tmp_path / "ok.opus"), str(tmp_path / "loud.wav")]) == 1
    assert "FAIL loud.wav" in capsys.readouterr().out
    assert check_main([]) == 2


async def test_partial_index_keeps_later_tracks(tmp_path):
    """Partial index written mid-scene preserves later existing tracks not yet reached."""
    vocab = load_vocab(tmp_path / "v")
    out = tmp_path / "pack"

    # First: create a pack with tracks 1 and 3 (by running with 3 tracks, then manually removing track 2)
    initial = await make(client(), vocab, out, scenes("crypt"), tracks=3, quality=2,
                         rng=random.Random(1), poll_s=0.01, log=quiet)

    # Get the original seeds
    original_seeds = {t["file"]: t["seed"] for t in initial["scenes"][0]["tracks"]}

    # Remove track 2 from disk and YAML (simulate having only 1 and 3)
    (out / "crypt-2.opus").unlink()
    yaml_content = yaml.safe_load((out / "starter.yaml").read_text())
    yaml_content["scenes"][0]["tracks"] = [t for t in yaml_content["scenes"][0]["tracks"] if t["file"] != "crypt-2.opus"]
    (out / "starter.yaml").write_text(yaml.safe_dump(yaml_content, sort_keys=False, allow_unicode=True))

    # Now run again with 3 tracks, interrupt after track 2 finishes
    completion_logs = []

    def log_and_interrupt(msg):
        msg_str = str(msg)
        completion_logs.append(msg_str)
        # Track 1 is reused (no completion msg), track 2 is rendered (completion msg), track 3 is reused (no completion msg)
        if ".opus:" in msg_str and msg_str.split()[-1] == "s":
            # Interrupt after the first (and only) completion message (track 2)
            raise KeyboardInterrupt("interrupt after track 2")

    with pytest.raises(KeyboardInterrupt):
        await make(client(), vocab, out, scenes("crypt"), tracks=3, quality=2,
                   rng=random.Random(1), poll_s=0.01, log=log_and_interrupt)

    # Verify YAML has all three tracks: 1 (original), 2 (new), 3 (original)
    yaml_data = yaml.safe_load((out / "starter.yaml").read_text())
    assert len(yaml_data["scenes"]) == 1
    tracks = yaml_data["scenes"][0]["tracks"]
    assert len(tracks) == 3, f"Should have 3 tracks, got {len(tracks)}"

    # Verify original seeds are preserved for tracks 1 and 3
    assert tracks[0]["file"] == "crypt-1.opus"
    assert tracks[0]["seed"] == original_seeds["crypt-1.opus"]
    assert tracks[2]["file"] == "crypt-3.opus"
    assert tracks[2]["seed"] == original_seeds["crypt-3.opus"]

    # Track 2 is new
    assert tracks[1]["file"] == "crypt-2.opus"


async def test_interrupt_mid_scene_preserves_tracks(tmp_path):
    """Interruption mid-scene (Ctrl-C) writes partial tracks to YAML. Rerun completes remaining tracks."""
    vocab = load_vocab(tmp_path / "v")
    out = tmp_path / "pack"

    completion_logs = []

    def log_and_interrupt(msg):
        msg_str = str(msg)
        completion_logs.append(msg_str)
        # Interrupt after first track completion (the "N s" message)
        if ".opus:" in msg_str and msg_str.split()[-1] == "s" and len(completion_logs) == 2:
            raise KeyboardInterrupt("mid-scene interrupt")

    # First run: render 3 tracks of crypt, interrupt after track 1
    with pytest.raises(KeyboardInterrupt):
        await make(client(), vocab, out, scenes("crypt"), tracks=3, quality=2,
                   rng=random.Random(1), poll_s=0.01, log=log_and_interrupt)

    # Verify starter.yaml was written with track 1
    assert (out / "starter.yaml").exists()
    yaml_data = yaml.safe_load((out / "starter.yaml").read_text())
    assert len(yaml_data["scenes"]) == 1
    assert len(yaml_data["scenes"][0]["tracks"]) == 1
    track1_seed = yaml_data["scenes"][0]["tracks"][0]["seed"]

    # Rerun: count POST /jobs to verify only tracks 2-3 are rendered
    jobs_count = [0]

    class CountingTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            if request.method == "POST" and "/jobs" in request.url.path:
                jobs_count[0] += 1
            return await super().handle_async_request(request)

    c = GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=CountingTransport(app=fake_app(delay_s=0, max_duration_s=31)), base_url="http://fake"))
    result = await make(c, vocab, out, scenes("crypt"), tracks=3, quality=2,
                        rng=random.Random(1), poll_s=0.01, log=quiet)

    # Should have only 2 new jobs (tracks 2 and 3)
    assert jobs_count[0] == 2
    # All 3 tracks now in result
    assert len(result["scenes"][0]["tracks"]) == 3
    # Track 1's seed preserved
    assert result["scenes"][0]["tracks"][0]["seed"] == track1_seed


async def test_survives_generator_error(tmp_path):
    """GeneratorError on one job doesn't abort the run. starter.yaml written per-track allows resuming."""
    vocab = load_vocab(tmp_path / "v")
    out = tmp_path / "pack"

    # Create a mock transport that fails the second job
    jobs_seen = [0]
    failed_job_id = [None]
    real_app = fake_app(delay_s=0, max_duration_s=31)

    class FailingTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            # Count POST /jobs calls
            if request.method == "POST" and "/jobs" in request.url.path:
                jobs_seen[0] += 1
                if jobs_seen[0] == 2:  # Make the 2nd job fail
                    job_id = (await response.aread()).decode()
                    import json
                    failed_job_id[0] = json.loads(job_id)["id"]
            # Intercept status check for the failed job and return failed status
            elif request.method == "GET" and failed_job_id[0] and failed_job_id[0] in str(request.url):
                import json
                response_body = await response.aread()
                data = json.loads(response_body)
                if data["id"] == failed_job_id[0]:
                    data["status"] = "failed"
                    data["error"] = "simulated generator error"
                    return httpx.Response(
                        200,
                        content=json.dumps(data).encode(),
                        headers={"content-type": "application/json"}
                    )
            return response

    c = GeneratorClient("http://fake", client=httpx.AsyncClient(
        transport=FailingTransport(app=real_app), base_url="http://fake"))

    # Run with 2 tracks for combat, first run fails on track 2
    first = await make(c, vocab, out, scenes("combat"), tracks=2, quality=2,
                       rng=random.Random(1), poll_s=0.01, log=quiet)

    # Check that starter.yaml was written despite the error
    assert (out / "starter.yaml").exists(), "starter.yaml should exist after partial run"
    yaml_content = yaml.safe_load((out / "starter.yaml").read_text())
    assert len(yaml_content["scenes"]) == 1
    assert len(yaml_content["scenes"][0]["tracks"]) == 1, "Only the successful track should be in YAML"
    assert (out / "combat-1.opus").exists()
    assert not (out / "combat-2.opus").exists()

    # Now run again with the same seed to verify we don't re-render track 1
    jobs_seen[0] = 0
    failed_job_id[0] = None
    second = await make(c, vocab, out, scenes("combat"), tracks=2, quality=2,
                        rng=random.Random(1), poll_s=0.01, log=quiet)

    # Should only have 1 new POST /jobs (for combat-2)
    assert jobs_seen[0] == 1, f"Should only render combat-2, but saw {jobs_seen[0]} jobs"
    assert len(second["scenes"][0]["tracks"]) == 2, "Now both tracks should be present"
    # Seed of first track should be preserved from first run
    assert first["scenes"][0]["tracks"][0]["seed"] == second["scenes"][0]["tracks"][0]["seed"]
