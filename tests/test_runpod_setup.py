"""scripts/runpod_setup.py against httpx.MockTransport. Request and response shapes are taken
from the Runpod REST OpenAPI spec (2026-10-13), not yet exercised live."""
import importlib.util
import json
import stat
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runpod_setup", ROOT / "scripts/runpod_setup.py")
rs = importlib.util.module_from_spec(spec)
sys.modules["runpod_setup"] = rs
spec.loader.exec_module(rs)

TEMP_KEY = "rpa_TEMPALLACCESS999"
RESTRICTED = "rpa_RESTRICTED777"
IMAGE = "ghcr.io/thethirdadmin/dnd-music-generator:v0.1.0"
BASE = "https://x/v1"


class Server:
    def __init__(self, fail=None, ghcr_manifest=200):
        self.calls, self.fail, self.ghcr_manifest = [], fail or {}, ghcr_manifest

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "ghcr.io":   # anonymous image preflight, not a management call
            if req.url.path == "/token":
                return httpx.Response(200, json={"token": "anon"})
            return httpx.Response(self.ghcr_manifest)
        assert req.headers["authorization"] == f"Bearer {TEMP_KEY}"
        path = req.url.path.replace("/v1", "", 1)
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, path, body))
        for (m, prefix), status in self.fail.items():
            if req.method == m and path.startswith(prefix):
                return httpx.Response(status, json={"error": "nope"})
        if req.method == "GET" and path == "/endpoints":
            return httpx.Response(200, json=[])
        if req.method == "POST":
            return httpx.Response(200, json={"id": path.strip("/")[:3] + "-1"})
        if req.method == "GET":
            if path.startswith("/endpoints/"):
                return httpx.Response(200, json={"workersMin": 0, "workersMax": 1, "idleTimeout": 5,
                                                 "executionTimeoutMs": 600000, "templateId": "tem-1"})
            if path.startswith("/networkvolumes/"):
                return httpx.Response(200, json={"size": 15})
            return httpx.Response(200, json={"imageName": IMAGE})
        return httpx.Response(204)

    def posts(self):
        return [(p, b) for m, p, b in self.calls if m == "POST"]


def api(server):
    return rs.Api(TEMP_KEY, base=BASE, transport=httpx.MockTransport(server))


def test_plan_maps_pool_to_gpu_types_and_estimates_rate():
    p = rs.plan("AMPERE_16")
    assert p.rate_per_hr == 0.58 and p.worst_case_hr == pytest.approx(0.58)
    assert p.gpu_types == ("NVIDIA RTX A4000", "NVIDIA RTX A4500")
    assert p.data_center == "EU-RO-1" and p.volume_gb == 15 and p.volume_per_month == pytest.approx(1.05)
    assert rs.plan("AMPERE_16", max_workers=2, data_center="US-TX-3").worst_case_hr == pytest.approx(1.16)
    with pytest.raises(rs.SetupError, match="unknown pool"):
        rs.plan("ADA_24")


def test_default_bases_are_separate(monkeypatch):
    assert rs.MGMT_BASE == "https://rest.runpod.io/v1" and rs.JOB_BASE == "https://api.runpod.ai/v2"


def test_create_all_bodies_and_order():
    srv = Server()
    c = rs.create_all(api(srv), rs.plan(), IMAGE)
    assert [p for p, _ in srv.posts()] == ["/templates", "/networkvolumes", "/endpoints"]
    tpl, vol, ep = (b for _, b in srv.posts())
    assert tpl == {"name": "dnd-music-generator", "imageName": IMAGE, "isServerless": True,
                   "containerDiskInGb": 20, "env": {"MODEL_DIR": "/runpod-volume"}, "ports": [], "category": "NVIDIA"}
    assert vol == {"name": "dnd-music-models", "size": 15, "dataCenterId": "EU-RO-1"}
    assert ep == {"name": "dnd-music", "templateId": "tem-1", "computeType": "GPU",
                  "gpuTypeIds": ["NVIDIA RTX A4000", "NVIDIA RTX A4500"], "gpuCount": 1, "minCudaVersion": "13.0",
                  "workersMin": 0, "workersMax": 1, "idleTimeout": 5, "executionTimeoutMs": 600000,
                  "flashboot": True, "scalerType": "QUEUE_DELAY", "scalerValue": 4,
                  "networkVolumeId": "net-1", "dataCenterIds": ["EU-RO-1"]}
    assert (c.template_id, c.volume_id, c.endpoint_id) == ("tem-1", "net-1", "end-1")


def test_create_all_without_volume():
    srv = Server()
    c = rs.create_all(api(srv), rs.plan(volume_gb=0), IMAGE)
    assert [p for p, _ in srv.posts()] == ["/templates", "/endpoints"]
    assert c.volume_id is None and "networkVolumeId" not in srv.posts()[1][1]
    assert srv.posts()[0][1]["env"] == {"MODEL_DIR": "/models"}


def test_endpoint_failure_deletes_template_and_volume():
    srv = Server(fail={("POST", "/endpoints"): 400})
    with pytest.raises(rs.ApiError) as e:
        rs.create_all(api(srv), rs.plan(), IMAGE)
    assert e.value.status == 400
    deletes = [p for m, p, _ in srv.calls if m == "DELETE"]
    assert deletes == ["/networkvolumes/net-1", "/templates/tem-1"]


def test_dry_run_makes_no_http_calls(tmp_path, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("HTTP made")
    monkeypatch.setattr(httpx.Client, "request", boom)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("prompted"))
    assert rs.main(["--data-dir", str(tmp_path), "--dry-run", "--image", IMAGE]) == 0
    out = capsys.readouterr().out
    assert "AMPERE_16" in out and "NVIDIA RTX A4500" in out and "$0.58/hr" in out and "estimate" in out and "Dry run" in out
    assert not (tmp_path / "runpod.json").exists()


def test_dry_run_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUNPOD_SETUP_DRY_RUN", "1")
    assert rs.main(["--data-dir", str(tmp_path), "--image", IMAGE]) == 0
    assert "Dry run" in capsys.readouterr().out


@pytest.fixture
def prompts(monkeypatch):
    """Scripted input() and getpass(); returns the lists of answers and prompts seen."""
    class P:
        answers, secrets, asked = [], [], []
    p = P()
    p.answers, p.secrets, p.asked = [], [], []

    def _input(prompt=""):
        p.asked.append(prompt)
        return p.answers.pop(0)

    def _getpass(prompt=""):
        p.asked.append(prompt)
        return p.secrets.pop(0)
    real_check = rs.check_image   # no real ghcr.io call when a test gives no transport
    monkeypatch.setattr(rs, "check_image", lambda image, transport=None: True if transport is None
                        else real_check(image, transport))
    monkeypatch.setattr("builtins.input", _input)
    monkeypatch.setattr(rs.getpass, "getpass", _getpass)
    monkeypatch.setattr(sys, "argv", ["runpod_setup.py", "--data-dir", "x"])
    return p


def test_full_flow_writes_restricted_key_only(tmp_path, prompts, capsys, monkeypatch):
    srv = Server()
    prompts.answers = ["y"]
    prompts.secrets = [TEMP_KEY, RESTRICTED]
    jobs = []
    monkeypatch.setattr(rs, "run_test_job", lambda cfg, **k: jobs.append(cfg) or {"delay_s": 12.0, "exec_s": 40.0})
    code = rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv))
    assert code == 0
    out = capsys.readouterr().out
    assert TEMP_KEY not in out and RESTRICTED not in out
    assert TEMP_KEY not in " ".join(sys.argv)
    f = tmp_path / "runpod.json"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    text = f.read_text()
    assert TEMP_KEY not in text and RESTRICTED in text
    doc = json.loads(text)
    assert doc["endpoint_id"] == "end-1" and doc["rate_per_hr"] == 0.58
    assert doc["created"] == {"template_id": "tem-1", "endpoint_id": "end-1", "volume_id": "net-1"}
    assert jobs and jobs[0].api_key == RESTRICTED
    assert "cold start 12 s" in out and "estimate" in out and "--teardown" in out
    # the temporary key was the bearer on every management call
    assert prompts.asked[0].startswith("Have you turned off auto-pay in Runpod (Settings > Billing)? [y/N]")


def test_load_config_ignores_created(tmp_path):
    from dndmusic.runpod.config import load_config
    rs.write_config(tmp_path, "ep1", RESTRICTED, 0.58)
    rs.record_created(tmp_path, rs.Created("t1", "ep1", None))
    cfg = load_config(tmp_path, {})
    assert cfg.endpoint_id == "ep1" and cfg.api_key == RESTRICTED
    rs.write_config(tmp_path, "ep1", RESTRICTED, 0.6)   # a later save keeps the ids
    assert json.loads((tmp_path / "runpod.json").read_text())["created"] == {"template_id": "t1", "endpoint_id": "ep1"}


@pytest.mark.parametrize("answer", ["", "n", "yes please", "N"])
def test_autopay_not_confirmed_aborts_before_any_call(tmp_path, prompts, capsys, answer):
    srv = Server()
    prompts.answers = [answer]
    code = rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv))
    assert code == 20 and srv.calls == []
    assert "auto-pay" in capsys.readouterr().out and not (tmp_path / "runpod.json").exists()


def test_403_on_create_falls_back_to_manual_steps(tmp_path, prompts, capsys):
    srv = Server(fail={("POST", "/templates"): 403})
    prompts.answers = ["y", "abc123xyz"]
    prompts.secrets = [TEMP_KEY, RESTRICTED]
    code = rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv))
    assert code == 10
    out = capsys.readouterr().out
    for need in (IMAGE, "AMPERE_16", "min 0", "max 1", "idle timeout 5", "execution timeout 600", "CUDA 13.0 or newer", "NVIDIA RTX A4000", "read-only on\n     everything else (the balance is read from your account)", "auto-pay",
                 "cannot create endpoints"):
        assert need in out
    assert TEMP_KEY not in out and RESTRICTED not in out
    doc = json.loads((tmp_path / "runpod.json").read_text())
    assert doc["endpoint_id"] == "abc123xyz" and doc["api_key"] == RESTRICTED
    assert stat.S_IMODE((tmp_path / "runpod.json").stat().st_mode) == 0o600


def test_key_check_401_goes_manual(tmp_path, prompts, capsys):
    srv = Server(fail={("GET", "/endpoints"): 401})
    prompts.answers, prompts.secrets = ["y", ""], [TEMP_KEY]
    assert rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv)) == 10
    assert "this key cannot create endpoints" in capsys.readouterr().out
    assert [m for m, *_ in srv.calls] == ["GET"]


def test_no_capacity_on_endpoint_create_cleans_up_and_goes_manual(tmp_path, prompts, capsys):
    srv = Server(fail={("POST", "/endpoints"): 500})
    prompts.answers, prompts.secrets = ["y", ""], [TEMP_KEY]
    assert rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv)) == 10
    assert "nothing was left behind" in capsys.readouterr().out
    assert [p for m, p, _ in srv.calls if m == "DELETE"] == ["/networkvolumes/net-1", "/templates/tem-1"]


def test_key_check_hits_endpoints_on_the_management_base(tmp_path, prompts):
    srv = Server()
    prompts.answers, prompts.secrets = ["y", ""], [TEMP_KEY, ""]
    rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv))
    assert srv.calls[0] == ("GET", "/endpoints", None)


def test_readback_mismatch_is_only_a_warning():
    def h(req):
        if req.method == "GET" and req.url.path.startswith("/v1/endpoints/"):
            return httpx.Response(200, json={"workersMax": 3})
        return httpx.Response(200, json={})
    a = rs.Api(TEMP_KEY, base=BASE, transport=httpx.MockTransport(h))
    assert rs.verify(a, rs.plan(volume_gb=0), rs.Created("t", "e", None), IMAGE) == ["workersMax"]


def test_empty_temp_key_skips_to_manual_and_all_skipped_writes_nothing(tmp_path, prompts, capsys):
    prompts.answers, prompts.secrets = ["y", ""], [""]
    assert rs.main(["--data-dir", str(tmp_path), "--image", IMAGE]) == 10
    out = capsys.readouterr().out
    assert "Settings > Runpod" in out and "min 0" in out
    assert not (tmp_path / "runpod.json").exists()


def test_endpoint_id_without_key_is_saved_for_the_ui(tmp_path, prompts):
    prompts.answers, prompts.secrets = ["y", "abc123xyz"], ["", ""]
    assert rs.main(["--data-dir", str(tmp_path), "--image", IMAGE]) == 10
    doc = json.loads((tmp_path / "runpod.json").read_text())
    assert doc["endpoint_id"] == "abc123xyz" and doc["api_key"] == ""
    from dndmusic.runpod.config import load_config
    assert load_config(tmp_path, {}) is None   # UI adds the key later


def test_skipped_restricted_key_after_create_exits_10(tmp_path, prompts, capsys):
    srv = Server()
    prompts.answers, prompts.secrets = ["y"], [TEMP_KEY, ""]
    assert rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv)) == 10
    doc = json.loads((tmp_path / "runpod.json").read_text())
    assert doc["endpoint_id"] == "end-1" and doc["api_key"] == "" and "created" in doc
    assert "Settings > Runpod" in capsys.readouterr().out


def test_manual_flag_makes_no_http_calls(tmp_path, prompts, monkeypatch):
    monkeypatch.setattr(httpx.Client, "request", lambda *a, **k: pytest.fail("HTTP made"))
    prompts.answers, prompts.secrets = ["y", ""], []
    assert rs.main(["--data-dir", str(tmp_path), "--manual", "--image", "docker.io/me/gen:1"]) == 10


def test_teardown_deletes_and_verifies(tmp_path, prompts, capsys):
    rs.write_config(tmp_path, "end-1", RESTRICTED, 0.58)
    rs.record_created(tmp_path, rs.Created("tem-1", "end-1", "net-1"))
    gone = set()

    def h(req):
        path = req.url.path.replace("/v1", "")
        if req.method == "DELETE":
            gone.add(path)
            return httpx.Response(200, json={})
        return httpx.Response(404 if path in gone else 200, json={})
    prompts.answers, prompts.secrets = ["y"], [TEMP_KEY]
    code = rs.main(["--data-dir", str(tmp_path), "--teardown"], transport=httpx.MockTransport(h))
    assert code == 0 and gone == {"/endpoints/end-1", "/templates/tem-1", "/networkvolumes/net-1"}
    doc = json.loads((tmp_path / "runpod.json").read_text())
    assert "created" not in doc and doc["endpoint_id"] == "" and doc["api_key"] == ""
    assert TEMP_KEY not in capsys.readouterr().out


def test_teardown_declined_deletes_nothing(tmp_path, prompts):
    rs.write_config(tmp_path, "end-1", RESTRICTED, 0.58)
    rs.record_created(tmp_path, rs.Created("tem-1", "end-1", None))
    prompts.answers = ["n"]
    assert rs.main(["--data-dir", str(tmp_path), "--teardown"]) == 20


def test_http_errors_carry_status_only():
    def h(req):
        raise httpx.ConnectError("boom https://x/?key=SECRET")
    a = rs.Api("SECRET", base=BASE, transport=httpx.MockTransport(h))
    with pytest.raises(rs.ApiError) as e:
        a.call("GET", "/endpoints", "check key")
    assert "SECRET" not in str(e.value) and "unreachable" in str(e.value)


def ghcr(manifest_status=200, token_status=200, seen=None):
    def h(req):
        if seen is not None:
            seen.append((req.method, str(req.url)))
        if req.url.host == "ghcr.io" and req.url.path == "/token":
            return httpx.Response(token_status, json={"token": "anon"})
        if req.url.path.startswith("/v2/") and "/manifests/" in req.url.path:
            assert req.headers["authorization"] == "Bearer anon"
            assert "application/vnd.oci.image.index.v1+json" in req.headers["accept"]
            return httpx.Response(manifest_status)
        return httpx.Response(500)
    return httpx.MockTransport(h)


def test_default_image_uses_the_published_tag(monkeypatch):
    assert rs.default_image() == "ghcr.io/thethirdadmin/dnd-music-generator:v0.1.0"
    monkeypatch.setattr(rs, "IMAGE_BASE", "ghcr.io/me/gen")
    assert rs.default_image() == "ghcr.io/me/gen:v0.1.0"
    monkeypatch.setattr(rs, "IMAGE_BASE", "ghcr.io/me/gen:v2")
    assert rs.default_image() == "ghcr.io/me/gen:v2"


def test_check_image_found_missing_and_skipped():
    seen = []
    assert rs.check_image(IMAGE, ghcr(200, seen=seen)) is True
    assert seen[0][1].startswith("https://ghcr.io/token?scope=repository") and seen[1][0] == "HEAD"
    assert "manifests/v0.1.0" in seen[1][1]
    for st in (404, 401, 403):
        assert rs.check_image(IMAGE, ghcr(st)) is False
    assert rs.check_image(IMAGE, ghcr(token_status=403)) is False
    assert rs.check_image("docker.io/me/gen:1", ghcr(404)) is None   # not ghcr.io: no check


def test_check_image_network_error_is_a_warning(capsys):
    def boom(req):
        raise httpx.ConnectError("down")
    assert rs.check_image(IMAGE, httpx.MockTransport(boom)) is None
    assert "warning" in capsys.readouterr().out


def test_missing_image_aborts_before_autopay_and_any_create(tmp_path, prompts, capsys):
    srv = Server(ghcr_manifest=404)
    prompts.answers, prompts.secrets = [], []   # any prompt would pop from an empty list
    code = rs.main(["--data-dir", str(tmp_path), "--image", IMAGE], transport=httpx.MockTransport(srv))
    assert code == 20 and srv.calls == []
    out = capsys.readouterr().out
    assert f"image {IMAGE} was not found or is not public on ghcr.io (anonymous pull failed)" in out
    assert "RUNPOD_IMAGE" in out and prompts.asked == []


def test_other_registry_skips_the_check(tmp_path, prompts):
    srv = Server(ghcr_manifest=404)
    prompts.answers, prompts.secrets = ["y", ""], [TEMP_KEY, ""]
    rs.main(["--data-dir", str(tmp_path), "--image", "docker.io/me/gen:1"], transport=httpx.MockTransport(srv))
    assert srv.calls and srv.calls[0][1] == "/endpoints"


def test_write_config_keeps_created_ids(tmp_path):
    rs.write_config(tmp_path, "ep1", "", 0.5)
    rs.record_created(tmp_path, rs.Created("t1", "ep1", "v1"))
    rs.write_config(tmp_path, "ep1", "rpa_key", 0.5)
    doc = json.loads((tmp_path / "runpod.json").read_text())
    assert doc["created"] == {"template_id": "t1", "endpoint_id": "ep1", "volume_id": "v1"}
