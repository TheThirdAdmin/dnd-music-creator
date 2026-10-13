import json
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_conductor_image_has_no_test_or_real_generator():
    df = (ROOT / "docker/conductor.Dockerfile").read_text()
    assert "fakegen" not in df and "COPY generator" not in df
    assert "COPY starter starter" in df and "STARTER_DIR=/app/starter" in df


def test_wheel_ships_only_the_conductor():
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert py["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["dndmusic"]
    assert "gpu" in py["tool"]["pytest"]["ini_options"]["addopts"]


def test_starter_audio_goes_through_lfs():
    if "no Git LFS" in (ROOT / ".gitattributes").read_text():  # the public export ships the music as a release asset
        assert (ROOT / "starter/starter.yaml").exists()
        return
    assert "starter/*.opus filter=lfs diff=lfs merge=lfs -text" in (ROOT / ".gitattributes").read_text()
    assert (ROOT / "starter/starter.yaml").exists()


def test_compose_profiles_and_generator_settings():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    s = c["services"]
    assert "profiles" not in s["conductor"]
    assert s["fake-generator"]["profiles"] == ["fake"]
    assert s["generator"]["profiles"] == ["gpu"] and s["generator-cpu"]["profiles"] == ["cpu"]
    assert s["generator"]["environment"]["GENERATOR_DEVICE"] == "gpu"
    assert s["generator-cpu"]["environment"]["GENERATOR_DEVICE"] == "cpu"
    dev = s["generator"]["deploy"]["resources"]["reservations"]["devices"][0]
    assert dev["driver"] == "nvidia" and dev["device_ids"] == ["${GENERATOR_GPU:-0}"] and dev["capabilities"] == ["gpu"]
    for name in ("generator", "generator-cpu"):
        g = s[name]
        assert g["ports"] == ["${GENERATOR_BIND:-127.0.0.1}:${GENERATOR_PORT:-8001}:8001"]
        assert g["init"] is True
        assert g["volumes"] == ["models:/models"] and g["user"] == "${APP_UID:-1000}:${APP_GID:-1000}"
    assert "models" in c["volumes"]


def test_generator_image_rules():
    df = (ROOT / "docker/generator.Dockerfile").read_text()
    assert "FROM python:3.12-slim" in df and "COMFYUI_VERSION=v0.39.2" in df
    assert "torch==2.14.1" in df and "torchvision==0.29.1" in df and "whl/cu130" in df
    assert "<<" not in df and "--mount" not in df
    for var in ("HOME=", "USER=", "LOGNAME="):
        assert var in df
    assert "gcc" in df and "libc6-dev" in df


def test_licence_files():
    notice = (ROOT / "NOTICE").read_text()
    assert ("This Stability AI Model is licensed under the Stability AI Community License, "
            "Copyright © Stability AI Ltd. All Rights Reserved") in notice
    assert "Powered by Stability AI" in notice and "GPL-3.0" in notice
    assert "COMMUNITY LICENSE AGREEMENT" in (ROOT / "generator/LICENSE-STABILITY-AI.md").read_text().upper()


@pytest.mark.parametrize("script", ["deploy.sh", "uninstall.sh", "scripts/smoke.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_deploy_help_lists_the_generator_options():
    out = subprocess.run([str(ROOT / "deploy.sh"), "--help"], capture_output=True, text=True, check=True).stdout
    for opt in ("--gpu ID", "--cpu-generator", "--generator URL", "--test-generator", "--generator-only", "--with-app"):
        assert opt in out


def test_deploy_help_lists_runpod():
    out = subprocess.run([str(ROOT / "deploy.sh"), "--help"], capture_output=True, text=True, check=True).stdout
    assert "--runpod" in out and "Settings > Runpod" in out


@pytest.mark.parametrize("other", [["--gpu", "0"], ["--cpu-generator"], ["--generator", "http://h:8001"],
                                   ["--test-generator"], ["--generator-only"]])
def test_deploy_runpod_conflicts_with_generator_options(other):
    r = subprocess.run([str(ROOT / "deploy.sh"), "--runpod", *other], capture_output=True, text=True)
    assert r.returncode != 0
    assert f"--runpod cannot be combined with {other[0]}" in r.stderr


def test_runpod_mode_leaves_generator_urls_empty():
    from dndmusic.config import Settings
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    # no colon: an empty GENERATOR_URLS in .env stays empty instead of falling back to localhost
    assert c["services"]["conductor"]["environment"]["GENERATOR_URLS"].startswith("${GENERATOR_URLS-http://localhost")
    assert Settings.from_env({"GENERATOR_URLS": "", "DATA_DIR": "/tmp"}).generator_urls == ()
    deploy = (ROOT / "deploy.sh").read_text()
    assert "GENERATOR_URLS=$GENERATOR_URLS" in deploy and 'runpod) GENERATOR_URLS=""' in deploy


def test_uninstall_help_offers_keep_models():
    out = subprocess.run([str(ROOT / "uninstall.sh"), "--help"], capture_output=True, text=True, check=True).stdout
    assert "--keep-models" in out and "--preserve-library" in out


def test_readme_covers_m2():
    readme = "\n".join(p.read_text() for p in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))])
    for needle in ("Powered by Stability AI", "Stability AI Community License", "NVIDIA Container Toolkit",
                   "Ampere", "--keep-models", "--generator-only", "--cpu-generator",
                   "Primary school guitar lesson", "Regional orchestra on a good night", "(almost) John Williams",
                   "ComfyUI", "GPL-3.0", "Milestone 2 (done)", "Location", "Mood", "Tempo", "straight in",
                   "~28 GB", "pytest -m gpu"):
        assert needle in readme, needle


def test_starter_yaml_is_valid():
    from dndmusic.starter import SCENE_FIELDS
    from dndmusic.models import Scene
    from dndmusic.vocab import Vocab
    vocab = Vocab.from_dict(yaml.safe_load((ROOT / "dndmusic/vocab_default.yaml").read_text()))
    pack = yaml.safe_load((ROOT / "starter/starter.yaml").read_text())
    assert pack.get("model")
    have_audio = any((ROOT / "starter").glob("*.opus"))  # the public export has none until the pack is downloaded
    for sc in pack["scenes"]:
        vocab.validate(Scene(**{k: v for k, v in sc.items() if k in SCENE_FIELDS}))
        for t in sc.get("tracks") or []:
            if have_audio:
                assert (ROOT / "starter" / t["file"]).is_file(), t["file"]
            assert t.get("seed") is not None and t.get("duration_s")


def test_freetext_default_yaml_ships_with_the_package():
    assert (ROOT / "dndmusic/freetext_default.yaml").is_file()
    from importlib import resources
    d = yaml.safe_load((resources.files("dndmusic") / "freetext_default.yaml").read_text())
    for key in ("locations", "moods", "tempos", "mood_tempo", "choir", "defaults"):
        assert key in d


def test_uninstall_help_mentions_runpod():
    out = subprocess.run([str(ROOT / "uninstall.sh"), "--help"], capture_output=True, text=True, check=True).stdout
    assert "Runpod" in out and "-y" in out


def _fake_install(tmp_path, runpod_json):
    import os
    import shutil
    inst = tmp_path / "inst"
    (inst / "data").mkdir(parents=True)
    (inst / "docker-compose.yml").write_text("services: {}\n")
    (inst / "deploy.sh").write_text("#!/bin/sh\n")
    if runpod_json is not None:
        (inst / "data" / "runpod.json").write_text(json.dumps(runpod_json))
    bin_ = tmp_path / "bin"   # a PATH without docker, so the script skips containers
    bin_.mkdir()
    for tool in ("sed", "tail", "find", "wc", "du", "cut", "paste", "date", "cat", "grep", "id", "rm", "mv",
                 "mkdir", "dirname", "realpath", "python3", "head", "tr", "sort", "env"):
        src = shutil.which(tool)
        if src:
            (bin_ / tool).symlink_to(src)
    return inst, {"PATH": str(bin_), "HOME": str(tmp_path), "TERM": "dumb"}


def test_uninstall_yes_lists_runpod_resources_and_never_prints_the_key(tmp_path):
    secret = "rpa_SECRETKEY"
    inst, env = _fake_install(tmp_path, {"endpoint_id": "ep1", "api_key": secret,
                                         "created": {"endpoint_id": "ep1", "template_id": "tp1", "volume_id": "vol1"}})
    r = subprocess.run(["/bin/bash", str(ROOT / "uninstall.sh"), "--dir", str(inst), "-y"],
                       capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout + r.stderr
    assert "endpoint ep1, template tp1, volume vol1" in out
    assert "Runpod resources were NOT deleted" in out and "Storage" in out
    assert secret not in out
    assert not inst.exists()


def test_uninstall_without_runpod_json_says_nothing_about_runpod(tmp_path):
    inst, env = _fake_install(tmp_path, None)
    r = subprocess.run(["/bin/bash", str(ROOT / "uninstall.sh"), "--dir", str(inst), "-y"],
                       capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 0 and "Runpod" not in r.stdout + r.stderr


def _run_on_pty(argv, env, cwd, answer: bytes):
    """Run argv with a controlling terminal, answer its first prompt; returns (exit code, terminal output)."""
    import os
    import pty
    import select
    import time
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve(argv[0], argv, env)
    out, sent, end = b"", False, time.time() + 20
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if not sent and b"Type '" in out:
            os.write(fd, answer + b"\n")
            sent = True
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def test_uninstall_prompt_is_visible_and_mismatch_says_what_was_expected(tmp_path):
    inst, env = _fake_install(tmp_path, None)
    code, out = _run_on_pty(["/bin/bash", str(ROOT / "uninstall.sh"), "--dir", str(inst)], env, tmp_path, b"nope")
    assert "Type 'yes' to continue:" in out
    assert "You typed 'nope'; expected 'yes'. Nothing was changed." in out
    assert code != 0 and inst.exists()


def test_uninstall_prompt_accepts_the_expected_answer(tmp_path):
    inst, env = _fake_install(tmp_path, None)
    code, out = _run_on_pty(["/bin/bash", str(ROOT / "uninstall.sh"), "--dir", str(inst)], env, tmp_path, b"yes")
    assert code == 0 and not inst.exists()


@pytest.mark.parametrize("script", ["deploy.sh", "uninstall.sh"])
def test_prompts_are_not_read_with_read_p_behind_a_stderr_redirect(script):
    import re
    text = "\n".join(re.sub(r"#.*$", "", ln) for ln in (ROOT / script).read_text().splitlines())
    # `read -p` prints its prompt on stderr; 2>/dev/null around it would hide the question
    assert not re.search(r"read\s+(-\w+\s+)*-p\b[^\n]*2>/dev/null|read\s+(-\w+\s+)*-p\b[^\n]*\n[^\n]*\}\s*2>/dev/null", text)
