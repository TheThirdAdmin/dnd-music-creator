import asyncio

import pytest

from generator.modelfiles import MEDIUM, SMALL
from generator.service import GenService, NotReady, QualityUnavailable
from genfakes import Clock, FakeRenderer


def service(tmp_path, gpu=True, **kw):
    svc = GenService(kw.pop("renderer", None) or FakeRenderer(), gpu=gpu, work_dir=tmp_path, **kw)
    svc.set_status("ok", ready=True)
    return svc


async def run_until_done(svc, *jobs, timeout_s=5):
    task = asyncio.create_task(svc.worker())
    try:
        for _ in range(int(timeout_s / 0.01)):
            if all(j.status in ("done", "failed") for j in jobs):
                return
            await asyncio.sleep(0.01)
        raise AssertionError([j.status for j in jobs])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_fifo_one_at_a_time(tmp_path):
    svc = service(tmp_path)
    jobs = [svc.submit(p, 30, None, 2) for p in ("a", "b", "c")]
    await run_until_done(svc, *jobs)
    assert [c[1] for c in svc.renderer.calls] == ["a", "b", "c"]
    assert svc.renderer.max_active == 1
    assert [j.public()["duration_s"] for j in jobs] == [30.0, 30.0, 30.0]
    assert all(j.public()["status"] == "done" and (tmp_path / "jobs" / f"{j.id}.flac").exists() for j in jobs)


async def test_cpu_offers_quality_1_only_and_caps_it_at_120_s(tmp_path):
    svc = service(tmp_path, gpu=False)
    for q in (2, 3):
        with pytest.raises(QualityUnavailable):
            svc.submit("x", 200, None, q)
    job = svc.submit("x", 200, 7, 1)
    await run_until_done(svc, job)
    assert svc.renderer.calls == [(SMALL.name, "x", 120, 7)]
    assert job.public() == {"id": job.id, "status": "done", "seed": 7, "duration_s": 120.0, "error": None}


async def test_quality_2_is_one_medium_render(tmp_path):
    svc = service(tmp_path)
    job = svc.submit("x", 200, 7, 2)
    await run_until_done(svc, job)
    assert svc.renderer.calls == [(MEDIUM.name, "x", 200, 7)]


@pytest.mark.parametrize("scores,kept", [([0.1, 0.5], 8), ([-0.4, -0.6], 7), ([-0.3, -0.3], 7)])
async def test_quality_3_keeps_the_better_of_two(tmp_path, scores, kept):
    it = iter(scores)
    svc = service(tmp_path, scorer=lambda audio: next(it))
    job = svc.submit("x", 60, 7, 3)
    await run_until_done(svc, job)
    assert [c[3] for c in svc.renderer.calls] == [7, 8] and {c[0] for c in svc.renderer.calls} == {MEDIUM.name}
    assert job.seed == kept


async def test_not_ready_refuses_jobs(tmp_path):
    svc = GenService(FakeRenderer(), gpu=True, work_dir=tmp_path)
    svc.set_status("downloading 43%")
    with pytest.raises(NotReady, match="downloading 43%"):
        svc.submit("x", 60, None, 2)
    assert svc.health()["ok"] is False and svc.health()["status"] == "downloading 43%"


async def test_queued_jobs_wait_while_comfyui_restarts(tmp_path):
    svc = service(tmp_path)
    job = svc.submit("x", 30, None, 2)
    svc.set_status("restarting ComfyUI")
    task = asyncio.create_task(svc.worker())
    await asyncio.sleep(0.1)
    assert job.status == "queued" and svc.renderer.calls == []
    svc.set_status("ok", ready=True)
    for _ in range(200):
        if job.status == "done":
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert job.status == "done"


async def test_a_failed_job_is_reported_and_the_next_one_runs(tmp_path):
    svc = service(tmp_path, renderer=FakeRenderer(fail="CUDA out of memory"))
    bad = svc.submit("x", 30, None, 2)
    await run_until_done(svc, bad)
    assert bad.public()["status"] == "failed" and bad.public()["error"] == "CUDA out of memory"
    svc.renderer.fail = None
    good = svc.submit("y", 30, None, 2)
    await run_until_done(svc, good)
    assert good.status == "done"


async def test_vram_is_freed_once_after_60_idle_seconds(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock=clock)
    await run_until_done(svc, svc.submit("x", 30, None, 2))
    clock.t += 59
    await svc.idle_tick()
    assert svc.renderer.frees == 0
    clock.t += 1
    await svc.idle_tick()
    await svc.idle_tick()
    assert svc.renderer.frees == 1


async def test_a_job_starting_during_the_free_is_not_marked_freed(tmp_path):
    clock = Clock()
    renderer = FakeRenderer()

    async def free_while_a_job_starts():
        svc.freed = False

    renderer.free = free_while_a_job_starts
    svc = service(tmp_path, clock=clock, renderer=renderer)
    svc.freed = False
    clock.t += 60
    await svc.idle_tick()
    assert svc.freed is False


async def test_housekeeping_survives_an_error(tmp_path):
    svc = service(tmp_path)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sweep failed")

    svc.sweep = boom
    task = asyncio.create_task(svc.housekeeping(every_s=0.01))
    await asyncio.sleep(0.15)
    assert not task.done() and calls["n"] >= 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_finished_audio_is_kept_an_hour_or_ten_minutes_after_fetching(tmp_path):
    clock = Clock()
    svc = service(tmp_path, clock=clock)
    a, b = svc.submit("a", 30, None, 2), svc.submit("b", 30, None, 2)
    await run_until_done(svc, a, b)
    path_a = a.audio_path
    clock.t = 100
    assert svc.audio_path(b.id) is not None
    clock.t = 699
    svc.sweep()
    assert b.id in svc.jobs
    clock.t = 700
    svc.sweep()
    assert b.id not in svc.jobs and a.id in svc.jobs
    clock.t = 3600
    svc.sweep()
    assert a.id not in svc.jobs and not path_a.exists()


def test_health(tmp_path):
    gpu, cpu = service(tmp_path), service(tmp_path, gpu=False)
    assert gpu.health() == {"ok": True, "model": "stable-audio-3-medium", "busy": False, "gpu": True,
                            "qualities": [1, 2, 3], "status": "ok"}
    assert cpu.health()["model"] == "stable-audio-3-small-music" and cpu.health()["qualities"] == [1]
