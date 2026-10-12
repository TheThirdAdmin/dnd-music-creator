#!/usr/bin/env python3
"""Render the starter pack, or a vocab preview, through a generator's job API.

  scripts/make_starter.py --generator http://localhost:8001 --out DIR           16 scenes x 3 tracks
  scripts/make_starter.py --generator URL --out DIR --only combat-2 crypt-1     redo those tracks (new seeds)
  scripts/make_starter.py --generator URL --out DIR --preview                   one track per location

Tracks go through the conductor's ingest (trim, straight-in cut, -16 LUFS, Opus) as DIR/<slug>-<n>.opus.
DIR/starter.yaml lists them in the format dndmusic.starter imports; DIR/PROMPTS.txt lists the prompts.
A run skips tracks that already exist, so it can be repeated after an interruption."""
import argparse
import asyncio
import random
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dndmusic.genclient import GeneratorClient, GeneratorError  # noqa: E402
from dndmusic.ingest import RejectedAudio, ingest  # noqa: E402
from dndmusic.models import Prompt, Scene  # noqa: E402
from dndmusic.prompts import scene_prompt  # noqa: E402
from dndmusic.starter import OPTIONAL_FIELDS, SCENE_FIELDS, STARTER_SCENES  # noqa: E402
from dndmusic.vocab import Vocab  # noqa: E402

MODEL = "stable_audio_3_medium"


def preview_scenes(vocab: Vocab) -> list[dict]:
    """One scene per location: the first starter scene there, otherwise calm and medium."""
    first: dict[str, dict] = {}
    for s in STARTER_SCENES:
        first.setdefault(s["location"], s)
    return [first.get(loc) or {"slug": loc.replace(" ", "-"), "name": loc.capitalize(), "location": loc,
                               "mood": "calm", "tempo": "medium", "choir": False, "start": "build"}
            for loc in vocab.locations]


async def render_one(client: GeneratorClient, prompt: Prompt, duration_s: int, seed: int, quality: int,
                     out: Path, poll_s: float) -> float:
    job = await client.submit(prompt, duration_s, seed, quality)
    while (st := await client.status(job)).status in ("queued", "running"):
        await asyncio.sleep(poll_s)
    if st.status != "done":
        raise GeneratorError(f"job {st.status}: {st.error}")
    return await asyncio.to_thread(ingest, await client.audio(job), out, straight=prompt.straight)


def _complete_scene_tracks(slug: str, kept: list[dict], tracks: int, out_dir: Path, old_tracks: dict) -> list[dict]:
    """Build complete track list for a scene: kept tracks + existing later tracks not in kept."""
    result = list(kept)
    # Add any existing later tracks (by position) that aren't already in kept
    kept_files = {t["file"] for t in kept}
    for m in range(1, tracks + 1):
        name = f"{slug}-{m}.opus"
        if name not in kept_files and name in old_tracks and (out_dir / name).exists():
            result.append(old_tracks[name])
    return result


def _write_index(index_path: Path, model: str, quality: int, out_scenes: list[dict],
                 current_scene: dict | None, old_spec: dict) -> None:
    """Build and write starter.yaml from finished scenes, in-progress scene, and old untouched scenes."""
    scenes = out_scenes[:]
    if current_scene is not None:
        scenes.append(current_scene)
    # Preserve old scenes from old_spec that we haven't touched (by name)
    processed_names = {s.get("name") for s in scenes}
    for old_scene in (old_spec or {}).get("scenes", []):
        if old_scene.get("name") not in processed_names:
            scenes.append(old_scene)
    spec = {"model": model, "quality": quality, "scenes": scenes}
    index_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")


async def make(client: GeneratorClient, vocab: Vocab, out_dir: Path, scenes: list[dict], *, tracks: int,
               quality: int, only: set[str] | None = None, rng: random.Random | None = None, poll_s: float = 2.0,
               index: bool = True, log=print) -> dict:
    rng = rng or random.Random()
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "starter.yaml"
    old_spec = yaml.safe_load(index_path.read_text()) if index_path.exists() else {}
    old_tracks = {t["file"]: t for s in (old_spec or {}).get("scenes", []) for t in s.get("tracks", [])}
    prompts, out_scenes = [], []

    # Build all prompts upfront (known before rendering starts)
    for entry in scenes:
        scene = Scene(**{k: v for k, v in entry.items() if k in SCENE_FIELDS})
        vocab.validate(scene)
        prompt = scene_prompt(scene, vocab)
        prompts.append(f"{entry['slug']}: {prompt.tags}")

    # Write PROMPTS.txt before rendering
    (out_dir / "PROMPTS.txt").write_text("\n\n".join(prompts) + "\n", encoding="utf-8")

    for entry_idx, entry in enumerate(scenes):
        scene = Scene(**{k: v for k, v in entry.items() if k in SCENE_FIELDS})
        vocab.validate(scene)
        prompt = scene_prompt(scene, vocab)
        kept = []
        for n in range(1, tracks + 1):
            name = f"{entry['slug']}-{n}.opus"
            redo = only is not None and name[:-5] in only
            if not redo and name in old_tracks and (out_dir / name).exists():
                kept.append(old_tracks[name])
                if index:
                    fields = {k: getattr(scene, k) for k in SCENE_FIELDS if k not in OPTIONAL_FIELDS or getattr(scene, k)}
                    complete_tracks = _complete_scene_tracks(entry['slug'], kept, tracks, out_dir, old_tracks)
                    current_scene = {**fields, "tracks": complete_tracks}
                    _write_index(index_path, MODEL, quality, out_scenes, current_scene, old_spec)
                continue
            if only is not None and not redo:
                continue
            seed = rng.getrandbits(31)
            lo, hi = vocab.duration_s
            log(f"{name}: rendering, seed {seed}")
            try:
                duration = await render_one(client, prompt, rng.randint(lo, hi), seed, quality, out_dir / name, poll_s)
            except RejectedAudio as e:
                log(f"{name}: rejected ({e}); run again to retry")
                if redo and name in old_tracks and (out_dir / name).exists():
                    kept.append(old_tracks[name])
                continue
            except GeneratorError as e:
                log(f"{name}: generator error ({e}); run again to retry")
                if redo and name in old_tracks and (out_dir / name).exists():
                    kept.append(old_tracks[name])
                continue
            kept.append({"file": name, "seed": seed, "duration_s": round(duration, 1)})
            # Write index after every track to survive mid-scene interruptions (before log in case log raises)
            if index:
                fields = {k: getattr(scene, k) for k in SCENE_FIELDS if k not in OPTIONAL_FIELDS or getattr(scene, k)}
                complete_tracks = _complete_scene_tracks(entry['slug'], kept, tracks, out_dir, old_tracks)
                current_scene = {**fields, "tracks": complete_tracks}
                _write_index(index_path, MODEL, quality, out_scenes, current_scene, old_spec)
            log(f"{name}: {duration:.0f} s")

        fields = {k: getattr(scene, k) for k in SCENE_FIELDS if k not in OPTIONAL_FIELDS or getattr(scene, k)}
        out_scenes.append({**fields, "tracks": kept})

    # Final write with complete spec
    spec = {"model": MODEL, "quality": quality, "scenes": out_scenes}
    if index:
        # Preserve old scenes not touched by this run
        processed_names = {s.get("name") for s in out_scenes}
        for old_scene in (old_spec or {}).get("scenes", []):
            if old_scene.get("name") not in processed_names:
                spec["scenes"].append(old_scene)
        index_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return spec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generator", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tracks", type=int, default=3)
    ap.add_argument("--quality", type=int, default=2, choices=(1, 2, 3))
    ap.add_argument("--only", nargs="+", metavar="SLUG-N")
    ap.add_argument("--preview", action="store_true", help="one track per location, no starter.yaml")
    ap.add_argument("--vocab", type=Path, default=ROOT / "dndmusic" / "vocab_default.yaml")
    a = ap.parse_args()
    vocab = Vocab.from_dict(yaml.safe_load(a.vocab.read_text(encoding="utf-8")))
    client = GeneratorClient(a.generator.rstrip("/"), timeout=120)
    scenes = preview_scenes(vocab) if a.preview else STARTER_SCENES
    asyncio.run(make(client, vocab, a.out, scenes, tracks=1 if a.preview else a.tracks, quality=a.quality,
                     only=set(a.only) if a.only else None, index=not a.preview))
    return 0


if __name__ == "__main__":
    sys.exit(main())
