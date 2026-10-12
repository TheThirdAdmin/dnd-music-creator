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
