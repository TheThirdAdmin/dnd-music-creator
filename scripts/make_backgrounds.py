#!/usr/bin/env python3
"""Render scene backgrounds through ComfyUI and post-process them to the repo format.

  scripts/make_backgrounds.py --comfy http://127.0.0.1:8188 --round 1                 all 76 images
  scripts/make_backgrounds.py --comfy URL --round 2 --only at-sea:3 crypt generic:5   redo those images
  scripts/make_backgrounds.py --comfy URL --round 1 --only tavern-night generic icon:at-sea
  scripts/make_backgrounds.py --comfy URL --round 1 --wide [--only ...]                1920x1080 desktop set (no icons)
  scripts/make_backgrounds.py --make-palette DIR                                       art/palette.json from approved images

Backgrounds: art/prompts.yaml prompt + style through art/workflow.json (1024x1024), nearest-neighbour down to
--size (default 512), quantised to art/palette.json (32 colours; per-image median cut until that exists), lossless WebP.
Icons (--only icon or icon:<slug>): art/workflow-icon.json draws one object on a plain background; it is keyed to
transparent, cropped, averaged down to 64x64 and reduced to 16 colours.
Output DIR/<slug>/<n>.webp (generic/<nn>.webp, icons/<slug>.webp), sheet-<slug>.png (rendered images only),
PROMPTS.txt, notes.txt, sizes.txt. Raw renders stay in ComfyUI's output under dnd-music/image-raw/roundN/."""
import argparse
import asyncio
import copy
import io
import json
import sys
from pathlib import Path

import numpy as np
import yaml
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generator.comfy import ComfyClient, ComfyError  # noqa: E402

ART = ROOT / "art"
DEFAULT_OUT = "/mnt/storage/ai/comfyui/output/dnd-music/image-preview/round{round}"
COLOURS = 32
ICON_PX = 64
WIDE_RENDER = (1344, 768)  # SDXL-friendly 16:9
WIDE_GRID = (480, 270)     # pixel grid of a desktop image; x4 = 1920x1080
WIDE_SCALE = 4
MAX_BYTES = 60 * 1024


def load_prompts(path: Path = ART / "prompts.yaml") -> dict:
    """prompts.yaml with the picker-only sets (extra_sets / extra_icons / extra_people) folded in after the starter ones."""
    spec = yaml.safe_load(path.read_text())
    spec["sets"] = {**spec["sets"], **spec.get("extra_sets", {})}
    spec["icons"] = {**spec["icons"], **spec.get("extra_icons", {})}
    spec["people"] = [*spec.get("people", []), *spec.get("extra_people", [])]
    return spec


def entries(spec: dict) -> list[dict]:
    """Every image: slug, n, seed, prompt, name (relative file stem), in file order."""
    out = [{"slug": slug, "n": n, "seed": it["seed"], "prompt": it["prompt"], "name": f"{slug}/{n}"}
           for slug, items in spec["sets"].items() for n, it in enumerate(items, 1)]
    out += [{"slug": "generic", "n": n, "seed": it["seed"], "prompt": it["prompt"], "name": f"generic/{n:02d}"}
            for n, it in enumerate(spec["generic"], 1)]
    out += [{"slug": "icon", "n": i, "id": slug, "seed": it["seed"], "name": f"icons/{slug}",
             "prompt": spec["icon_style"].format(subject=it["subject"]), "negative": spec["icon_negative"]}
            for i, (slug, it) in enumerate(spec["icons"].items(), 1)]
    return out


def style_for(spec: dict, e: dict) -> str:
    return spec["style_people"] if e["slug"] in spec.get("people", []) else spec["style"]


def select(all_: list[dict], only: list[str] | None) -> list[dict]:
    """--only items are `slug` (whole set), `slug:n`, `icon` (all) or `icon:<slug>`; unknown ones are an error."""
    if not only:
        return all_
    keys = set()
    for o in only:
        slug, _, n = o.partition(":")
        hits = [e for e in all_ if e["slug"] == slug and
                (not n or (e["id"] == n if slug == "icon" else e["n"] == int(n)))]
        if not hits:
            raise SystemExit(f"--only {o}: no such image")
        keys.update(e["name"] for e in hits)
    return [e for e in all_ if e["name"] in keys]


def patch_workflow(wf: dict, positive: str, negative: str, seed: int, prefix: str,
                   render_size: tuple[int, int] | None = None) -> dict:
    """Fill prompt, negative, seed, size and save prefix; text nodes are found through the sampler's links."""
    g = copy.deepcopy(wf)
    sampler = next(n for n in g.values() if n["class_type"] in ("KSampler", "KSamplerAdvanced"))
    g[sampler["inputs"]["positive"][0]]["inputs"]["text"] = positive
    neg = g[sampler["inputs"]["negative"][0]]
    if neg["class_type"] == "CLIPTextEncode":  # Z-Image runs at cfg 1 with a zeroed negative: nothing to fill
        neg["inputs"]["text"] = negative
    sampler["inputs"]["seed" if "seed" in sampler["inputs"] else "noise_seed"] = seed
    for n in g.values():  # the workflow file sets the render size unless render_size overrides it
        if render_size and n["class_type"] == "EmptyLatentImage":
            n["inputs"]["width"], n["inputs"]["height"] = render_size
        if n["class_type"] == "SaveImage":
            n["inputs"]["filename_prefix"] = prefix
    return g


def load_palette(path: Path) -> list[int] | None:
    if not path.exists():
        return None
    return [c for rgb in json.loads(path.read_text()) for c in rgb]


def process(png: bytes, size: int, palette: list[int] | None) -> bytes:
    """Nearest-neighbour downscale, quantise (shared palette or own median cut), lossless WebP bytes."""
    im = Image.open(io.BytesIO(png)).convert("RGB").resize((size, size), Image.NEAREST)
    return _quantised_webp(im, palette)


def trim_bars(im: Image.Image, flat: float = 10.0, margin: int = 6, max_frac: float = 0.3) -> Image.Image:
    """Cut flat letterbox / border bands (SDXL likes to draw them on wide renders) from every side, then
    centre-crop the rest back to 16:9 so it fills the frame."""
    a = np.asarray(im).astype(np.int16)
    h, w = a.shape[:2]

    def run(line, n, limit):
        k = 0
        while k < min(n, limit) and line(k).std(axis=0).max() < flat:
            k += 1
        return k + margin if k else 0

    t = run(lambda k: a[k], h, int(h * max_frac))
    b = run(lambda k: a[h - 1 - k], h, int(h * max_frac))
    l = run(lambda k: a[:, k], w, int(w * max_frac))
    r = run(lambda k: a[:, w - 1 - k], w, int(w * max_frac))
    im = im.crop((l, t, w - r, h - b))
    cw, ch = im.size
    want_h = cw * WIDE_GRID[1] // WIDE_GRID[0]
    if want_h <= ch:  # too tall: trim top/bottom evenly
        o = (ch - want_h) // 2
        return im.crop((0, o, cw, o + want_h))
    want_w = ch * WIDE_GRID[0] // WIDE_GRID[1]  # too wide: trim left/right evenly
    o = (cw - want_w) // 2
    return im.crop((o, 0, o + want_w, ch))


def process_wide(png: bytes, palette: list[int] | None) -> bytes:
    """Trim letterbox bars, area-average down to the 480x270 pixel grid, quantise, then integer-upscale x4
    (hard pixels) to 1920x1080."""
    im = trim_bars(Image.open(io.BytesIO(png)).convert("RGB")).resize(WIDE_GRID, Image.BOX)
    im = _quantised_webp(im, palette, as_image=True)
    return _webp(im.resize((WIDE_GRID[0] * WIDE_SCALE, WIDE_GRID[1] * WIDE_SCALE), Image.NEAREST))


def _webp(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="WEBP", lossless=True, quality=100, method=6)
    return buf.getvalue()


def _quantised_webp(im: Image.Image, palette: list[int] | None, as_image: bool = False):
    if palette:
        pal = Image.new("P", (1, 1))
        pal.putpalette((palette + palette[-3:] * 256)[:768])  # pad with a real colour, not black
        im = im.quantize(palette=pal, dither=Image.Dither.NONE)
    else:
        im = im.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    return im.convert("RGB") if as_image else _webp(im)


def process_icon(png: bytes) -> bytes:
    """Key the plain background to transparent (flood fill from the corners), crop to the object, pad to a
    square, area-average down to ICON_PX, 16 colours, hard alpha, lossless WebP."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    marked = im.copy()
    for xy in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)):
        ImageDraw.floodfill(marked, xy, (255, 0, 255), thresh=48)
    mask = Image.eval(ImageChops.difference(marked, im).convert("L"), lambda v: 255 if v else 0)  # changed = bg
    mask = ImageChops.invert(mask)  # object = 255
    box = mask.getbbox() or (0, 0, im.width, im.height)
    side = int(max(box[2] - box[0], box[3] - box[1]) * 1.12)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    crop = (cx - side // 2, cy - side // 2, cx - side // 2 + side, cy - side // 2 + side)
    rgb = im.crop(crop).resize((ICON_PX, ICON_PX), Image.BOX)
    alpha = mask.crop(crop).resize((ICON_PX, ICON_PX), Image.BOX).point(lambda v: 255 if v > 160 else 0)
    rgb = rgb.quantize(colors=16, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGB")
    rgb.putalpha(alpha)
    buf = io.BytesIO()
    rgb.save(buf, format="WEBP", lossless=True, quality=100, method=6)
    return buf.getvalue()


def build_palette(files: list[Path], size: int = 64) -> list[tuple[int, int, int]]:
    """One 32-colour median-cut palette over all the images (each shrunk first, so each counts equally)."""
    tiles = [Image.open(f).convert("RGB").resize((size, size), Image.NEAREST) for f in files]
    sheet = Image.new("RGB", (size, size * len(tiles)))
    for i, t in enumerate(tiles):
        sheet.paste(t, (0, i * size))
    q = sheet.quantize(colors=COLOURS, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    flat = q.getpalette()[:COLOURS * 3]
    return [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]


def write_sheet(out: Path, slug: str, items: list[dict]) -> None:
    """Contact sheet of the images that exist, 4 per row, labelled; 512 px shown at 1x, icons at 4x."""
    items = [e for e in items if (out / f"{e['name']}.webp").exists()]
    if not items:
        return
    files = [out / f"{e['name']}.webp" for e in items]
    cols = min(4, len(items))
    w = Image.open(files[0]).width
    wide = Image.open(files[0]).size == (WIDE_GRID[0] * WIDE_SCALE, WIDE_GRID[1] * WIDE_SCALE)
    w = 640 if wide else w * (4 if w <= ICON_PX else 1)
    h = w * 9 // 16 if wide else w
    cols = min(cols, 3) if wide else cols
    rows = -(-len(items) // cols)
    sheet = Image.new("RGB", (cols * (w + 8) + 8, rows * (h + 24) + 8), (20, 16, 31))
    draw = ImageDraw.Draw(sheet)
    for i, (e, f) in enumerate(zip(items, files)):
        x, y = 8 + (i % cols) * (w + 8), 8 + (i // cols) * (h + 24)
        label = e["id"] if slug == "icon" else f"{slug}:{e['n']}"
        draw.text((x, y + 4), f"{label}  seed {e['seed']}", fill=(241, 230, 200))
        tile = Image.open(f).convert("RGBA").resize((w, h), Image.NEAREST)
        sheet.paste(tile, (x, y + 20), tile)
    sheet.save(out / f"sheet-{slug}.png")


async def render_all(comfy: ComfyClient, todo: list[dict], wf: dict, icon_wf: dict, spec: dict, out: Path,
                     size: int, palette: list[int] | None, rnd: int, log, wide: bool = False) -> None:
    for e in todo:
        icon = e["slug"] == "icon"
        prefix = f"dnd-music/image-raw/{'hd' if wide else 'round'}{rnd}/{e['name'].replace('/', '-')}"
        graph = (patch_workflow(icon_wf, e["prompt"], e["negative"], e["seed"], prefix) if icon else
                 patch_workflow(wf, f"{e['prompt']}, {style_for(spec, e)}", spec["negative"], e["seed"], prefix,
                                WIDE_RENDER if wide else None))
        log(f"{e['name']} seed {e['seed']} ...")
        res = await comfy.run(graph, kind="images")
        data = (process_icon(res.audio) if icon else
                process_wide(res.audio, palette) if wide else process(res.audio, size, palette))
        path = out / f"{e['name']}.webp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if len(data) > MAX_BYTES:
            log(f"  warning: {e['name']} is {len(data)} bytes (> {MAX_BYTES})")


def write_reports(out: Path, spec: dict, all_: list[dict]) -> None:
    lines = [f"{e['name']}\tseed {e['seed']}\t{e['prompt']}" for e in all_]
    (out / "PROMPTS.txt").write_text(f"style: {spec['style']}\nstyle (people): {spec['style_people']} for {spec['people']}\n"
                                     f"negative: {spec['negative']}\n\n" + "\n".join(lines) + "\n")
    sizes, total = [], 0
    for e in all_:
        f = out / f"{e['name']}.webp"
        if f.exists():
            b = f.stat().st_size
            total += b
            sizes.append(f"{e['name']}\t{b}" + (f"\tOVER {MAX_BYTES // 1024} KB" if b > MAX_BYTES else ""))
    (out / "sizes.txt").write_text("\n".join(sizes) + f"\ntotal\t{total}\n")
    notes = out / "notes.txt"
    if not notes.exists():
        notes.write_text("# Verdict per image: ok / redo / change prompt. Next round: --only slug:n\n" +
                         "".join(f"{e['name']}: \n" for e in all_))
    for slug in dict.fromkeys(e["slug"] for e in all_):
        write_sheet(out, slug, [e for e in all_ if e["slug"] == slug])


async def make(comfy: ComfyClient, out: Path, rnd: int, only: list[str] | None = None, size: int = 512,
               prompts: Path = ART / "prompts.yaml", workflow: Path = ART / "workflow.json", icon_workflow: Path = ART / "workflow-icon.json",
               palette_file: Path = ART / "palette.json", log=print, wide: bool = False) -> list[dict]:
    spec = load_prompts(prompts)
    all_ = entries(spec)
    if wide:  # desktop set: backgrounds only, same slugs and numbers as the 512 px set
        all_ = [e for e in all_ if e["slug"] != "icon"]
    todo = select(all_, only)
    out.mkdir(parents=True, exist_ok=True)
    await render_all(comfy, todo, json.loads(workflow.read_text()), json.loads(icon_workflow.read_text()), spec, out,
                     size, load_palette(palette_file), rnd, log, wide)
    write_reports(out, spec, all_)
    return todo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--comfy", help="ComfyUI base URL")
    ap.add_argument("--round", type=int, help="round number (names the default --out)")
    ap.add_argument("--only", nargs="+", metavar="SLUG[:N]")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--raw", type=Path, help="with --wide: re-process saved raw renders (NAME_00001_.png) instead of rendering")
    ap.add_argument("--wide", action="store_true", help="render the 1920x1080 desktop set (no icons)")
    ap.add_argument("--make-palette", type=Path, metavar="DIR", help="build art/palette.json from DIR/**/*.webp")
    a = ap.parse_args()
    if a.make_palette:
        files = sorted(a.make_palette.rglob("*.webp"))
        if not files:
            print(f"no .webp files under {a.make_palette}", file=sys.stderr)
            return 1
        pal = build_palette(files)
        (ART / "palette.json").write_text(json.dumps([list(c) for c in pal]) + "\n")
        print(f"art/palette.json: {len(pal)} colours from {len(files)} images")
        return 0
    if a.raw:
        out = a.out or Path(DEFAULT_OUT.format(round=a.round or 0))
        palette = load_palette(ART / "palette.json")
        for e in (e for e in entries(load_prompts()) if e["slug"] != "icon"):
            src = a.raw / f"{e['name'].replace('/', '-')}_00001_.png"
            dst = out / f"{e['name']}.webp"
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(process_wide(src.read_bytes(), palette))
        print(f"re-processed into {out}")
        return 0
    if not a.comfy or a.round is None:
        ap.error("--comfy and --round are required")
    out = a.out or Path(DEFAULT_OUT.format(round=a.round))
    try:
        todo = asyncio.run(make(ComfyClient(a.comfy, poll_s=1.0), out, a.round, a.only, a.size, wide=a.wide))
    except ComfyError as e:
        print(f"ComfyUI: {e}", file=sys.stderr)
        return 1
    print(f"{len(todo)} images in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
