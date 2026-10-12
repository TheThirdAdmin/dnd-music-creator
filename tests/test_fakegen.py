import io
import time
import wave

from fastapi.testclient import TestClient

from fakegen.app import create_app, pitch_for


def wait_done(c, job_id):
    for _ in range(250):
        st = c.get(f"/jobs/{job_id}").json()
        if st["status"] not in ("queued", "running"):
            return st
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_job_lifecycle():
    with TestClient(create_app(delay_s=0, max_duration_s=2)) as c:
        assert c.get("/health").json() == {"ok": True, "model": "fake", "busy": False, "gpu": False,
                                           "qualities": [1, 2, 3], "status": "ok"}
        r = c.post("/jobs", json={"tags": "epic", "lyrics": "[instrumental]", "duration_s": 120, "seed": 7})
        assert r.status_code == 202
        job_id = r.json()["id"]
        assert wait_done(c, job_id) == {"id": job_id, "status": "done", "seed": 7, "duration_s": 120, "error": None}
        audio = c.get(f"/jobs/{job_id}/audio")
        assert audio.headers["content-type"] == "audio/wav"
        with wave.open(io.BytesIO(audio.content)) as w:
            assert abs(w.getnframes() / w.getframerate() - 2.0) < 0.01


def test_unknown_and_unfinished_jobs():
    with TestClient(create_app(delay_s=2, max_duration_s=1)) as c:
        assert c.get("/jobs/nope").status_code == 404
        job_id = c.post("/jobs", json={"tags": "x", "duration_s": 10}).json()["id"]
        assert c.get(f"/jobs/{job_id}/audio").status_code == 404
        assert isinstance(c.get(f"/jobs/{job_id}").json()["seed"], int)


def test_pitch_depends_on_tags():
    assert pitch_for("a") == pitch_for("a")
    assert len({pitch_for(t) for t in ("a", "b", "c", "d")}) > 1


def test_quality_is_accepted_and_checked():
    with TestClient(create_app(delay_s=0, max_duration_s=1)) as c:
        assert c.post("/jobs", json={"tags": "x", "duration_s": 10, "quality": 3}).status_code == 202
        assert c.post("/jobs", json={"tags": "x", "duration_s": 10, "quality": 4}).status_code == 422
