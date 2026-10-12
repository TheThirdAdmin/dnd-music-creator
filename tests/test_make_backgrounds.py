import json

import httpx
import pytest
import yaml
from PIL import Image

import fakecomfy
from generator.comfy import ComfyClient
from scripts.make_backgrounds import (ART, build_palette, entries, load_prompts, main, make, patch_workflow,
                                      select)


@pytest.fixture
def comfy(tmp_path):
    app = fakecomfy.create_app(tmp_path / "comfy-out")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fake")
    return ComfyClient("http://fake", client=c, poll_s=0.01)


def quiet(*_):
    pass


async def test_only_renders_exactly_one_image(comfy, tmp_path):
    out = tmp_path / "r1"
    todo = await make(comfy, out, 1, ["at-sea:3"], palette_file=tmp_path / "none.json", log=quiet)
    assert [e["name"] for e in todo] == ["at-sea/3"]
    assert [p.as_posix() for p in (out / "at-sea").glob("*.webp")] == [(out / "at-sea" / "3.webp").as_posix()]
    im = Image.open(out / "at-sea" / "3.webp")
    assert im.format == "WEBP" and im.size == (512, 512)
    assert len(im.convert("RGB").getcolors(256 * 256)) <= 32
    assert (out / "sheet-at-sea.png").exists() and (out / "notes.txt").exists()
    assert "total" in (out / "sizes.txt").read_text()


async def test_prompts_file_matches_yaml_and_rerun_overwrites(comfy, tmp_path):
    out = tmp_path / "r1"
    await make(comfy, out, 1, ["crypt", "generic:5"], palette_file=tmp_path / "none.json", log=quiet)
    spec = yaml.safe_load((ART / "prompts.yaml").read_text())
    text = (out / "PROMPTS.txt").read_text()
    for i, it in enumerate(spec["sets"]["crypt"], 1):
        assert f"crypt/{i}\tseed {it['seed']}\t{it['prompt']}" in text
    assert f"generic/05\tseed {spec['generic'][4]['seed']}" in text
    assert (out / "generic" / "05.webp").exists()
    before = sorted(p.name for p in out.rglob("*.webp"))
    await make(comfy, out, 1, ["crypt:2"], palette_file=tmp_path / "none.json", log=quiet)
    assert sorted(p.name for p in out.rglob("*.webp")) == before


async def test_shared_palette_is_used(comfy, tmp_path):
    pal = tmp_path / "palette.json"
    pal.write_text(json.dumps([[i * 8, i * 4, 255 - i * 8] for i in range(32)]))
    out = tmp_path / "r"
    await make(comfy, out, 1, ["town:1"], palette_file=pal, log=quiet)
    colours = {c for _, c in Image.open(out / "town" / "1.webp").convert("RGB").getcolors(65536)}
    assert colours <= {tuple(c) for c in json.loads(pal.read_text())}


def test_patch_workflow_fills_prompt_seed_size():
    wf = json.loads((ART / "workflow.json").read_text())
    g = patch_workflow(wf, "pos", "neg", 77, "p/x")
    assert g["3"]["inputs"]["text"] == "pos" and g["4"]["inputs"]["text"] == "neg"
    assert g["6"]["inputs"]["seed"] == 77 and g["5"]["inputs"]["width"] == 1024
    assert g["8"]["inputs"]["filename_prefix"] == "p/x" and wf["6"]["inputs"]["seed"] == 0


def test_zeroed_negative_gets_no_text():
    wf = json.loads((ART / "workflow.json").read_text())
    wf["4"] = {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["3", 0]}}
    assert "text" not in patch_workflow(wf, "p", "n", 1, "x")["4"]["inputs"]


def test_select_and_entries():
    all_ = entries(load_prompts())
    assert len(all_) == 102 + 15 * 5 and len([e for e in all_ if e['slug'] == 'icon']) == 18 + 15   # + picker-only sets
    assert [e["name"] for e in select(all_, ["generic:12"])] == ["generic/12"]
    assert len(select(all_, ["combat"])) == 4
    assert [e["name"] for e in select(all_, ["icon:at-sea"])] == ["icons/at-sea"]
    assert len(select(all_, ["icon"])) == 33
    assert len(select(all_, ["hades"])) == 4
    with pytest.raises(SystemExit):
        select(all_, ["nope"])


def test_build_palette(tmp_path):
    for i in range(3):
        Image.new("RGB", (8, 8), (i * 90, 10, 200)).save(tmp_path / f"{i}.webp", lossless=True)
    pal = build_palette(sorted(tmp_path.glob("*.webp")), size=8)
    assert 1 <= len(pal) <= 32 and set(pal) == {(0, 10, 200), (90, 10, 200), (180, 10, 200)} and all(len(c) == 3 for c in pal)


def test_cli_needs_comfy_and_round(capsys):
    with pytest.raises(SystemExit):
        main()


async def test_icons_are_64px_with_alpha(comfy, tmp_path):
    out = tmp_path / "r"
    await make(comfy, out, 1, ["icon:at-sea", "icon:crypt"], log=quiet)
    im = Image.open(out / "icons" / "at-sea.webp")
    assert im.size == (64, 64) and im.mode == "RGBA"
    assert len(im.convert("RGB").getcolors(4096)) <= 17
    assert (out / "sheet-icon.png").exists() and (out / "icons" / "crypt.webp").exists()


async def test_sheet_shows_rendered_images_only(comfy, tmp_path):
    out = tmp_path / "r"
    await make(comfy, out, 1, ["generic:1", "generic:6"], palette_file=tmp_path / "none.json", log=quiet)
    sheet = Image.open(out / "sheet-generic.png")
    assert sheet.width < 1100 and sheet.height < 600  # two tiles at 512, not twelve slots
