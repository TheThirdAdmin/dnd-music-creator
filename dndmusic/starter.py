"""The bundled starter pack: scene list (spec) and first-start import."""

import json
import logging
import re
import shutil
from pathlib import Path

import yaml

from .library import Library
from .models import Scene
from .prompts import scene_prompt
from .vocab import Vocab

log = logging.getLogger(__name__)
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
SCENE_FIELDS = ("name", "location", "mood", "tempo", "choir", "start", "extra", "prompt", "icon")
OPTIONAL_FIELDS = ("extra", "prompt", "icon")   # written to starter.yaml only when set


def is_lfs_pointer(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(len(LFS_POINTER)) == LFS_POINTER


def import_starter(lib: Library, vocab: Vocab, starter_dir: Path) -> int:
    """At startup: create each starter scene the library doesn't have yet (by name) and attach its tracks to the
    scene's current pool (by today's vocab). A scene is offered once: one the user deleted stays deleted. Existing
    scenes are never touched. Never generates. Returns the number of tracks imported."""
    index = Path(starter_dir) / "starter.yaml"
    if not index.exists():
        return 0
    spec = yaml.safe_load(index.read_text(encoding="utf-8")) or {}
    source = f"starter:{spec.get('model', 'unknown')}"
    scenes = spec.get("scenes") or []
    offered = set(lib.get_state("starter_scenes") or [])
    have = {sc.name for sc in lib.list_scenes()}
    todo = [e for e in scenes if e["name"] not in offered]
    if not todo:
        return 0
    imported = pointers = created = 0
    for entry in todo:
        offered.add(entry["name"])
        if entry["name"] in have:
            continue
        scene = Scene(**{k: entry[k] for k in SCENE_FIELDS if k in entry})
        try:
            vocab.validate(scene)
        except ValueError as e:
            log.warning("starter scene %r skipped: %s", entry["name"], e)
            offered.discard(entry["name"])
            continue
        scene = lib.create_scene(scene)
        created += 1
        pool = lib.pool_for(scene_prompt(scene, vocab), scene_id=scene.id)
        for t in entry.get("tracks") or []:
            src = Path(starter_dir) / t["file"]
            if not src.exists():
                log.warning("starter track %s is missing", src.name)
                continue
            if is_lfs_pointer(src):
                pointers += 1
                continue
            dest = lib.new_track_path()
            shutil.copyfile(src, dest)
            lib.add_track(pool.id, dest, float(t["duration_s"]), t.get("seed"), source, quality=spec.get("quality"))
            imported += 1
    if pointers:
        log.warning("%d starter tracks were skipped: they are Git LFS pointer files, not audio. Install git-lfs and "
                    "run 'git lfs pull' in the install folder before the first start; the app generates music for "
                    "these scenes instead.", pointers)
    lib.set_state("starter_scenes", sorted(offered))
    lib.set_state("starter_imported", True)
    log.info("starter pack: %d new scenes, %d tracks", created, imported)
    return imported


STARTER_SCENES = [
    {"slug": "tavern-night", "name": "Tavern night", "location": "tavern", "mood": "calm", "tempo": "medium",
     "choir": False, "start": "build"},
    {"slug": "town", "name": "Village (Peaceful)", "location": "town", "mood": "calm", "tempo": "slow",
     "choir": False, "start": "build", "icon": "town",
     "prompt": "Folk music for a peaceful medieval village. Warm and tranquil, slow. A solo wooden flute plays a "
               "gentle, rolling melody, with soft acoustic lute providing warm chords underneath. Close-miked "
               "sound. Instrumental."},
    {"slug": "village-troubled", "name": "Village (Troubled)", "location": "town", "mood": "tense", "tempo": "slow",
     "choir": False, "start": "build", "icon": "town",
     "prompt": "Folk music for a troubled medieval village. Tense and melancholy, slow. A solo viol or cello plays "
               "a sorrowful, lingering melody, with dissonant acoustic guitar and low drones pulsing anxiously "
               "underneath. Close-miked sound. Instrumental."},
    {"slug": "forest-travel", "name": "Forest travel", "location": "forest road", "mood": "calm", "tempo": "medium",
     "choir": False, "start": "build"},
    {"slug": "mountain-pass", "name": "Mountain pass", "location": "mountain pass", "mood": "tense", "tempo": "slow",
     "choir": False, "start": "build"},
    {"slug": "at-sea", "name": "At sea", "location": "at sea", "mood": "calm", "tempo": "medium",
     "choir": False, "start": "straight"},
    {"slug": "dungeon", "name": "Dungeon", "location": "dungeon", "mood": "tense", "tempo": "slow",
     "choir": False, "start": "build"},
    {"slug": "crypt", "name": "Crypt", "location": "crypt", "mood": "dread", "tempo": "slow",
     "choir": False, "start": "build"},
    {"slug": "ancient-ruins", "name": "Ancient ruins", "location": "ancient ruins", "mood": "mysterious",
     "tempo": "slow", "choir": False, "start": "build"},
    {"slug": "combat", "name": "Combat", "location": "battlefield", "mood": "combat", "tempo": "fast",
     "choir": True, "start": "straight"},
    {"slug": "boss-fight", "name": "Boss fight", "location": "castle hall", "mood": "boss fight", "tempo": "fast",
     "choir": True, "start": "straight"},
    {"slug": "victory", "name": "Victory", "location": "castle hall", "mood": "triumphant", "tempo": "medium",
     "choir": True, "start": "build"},
    {"slug": "bardcore", "name": "Bardcore", "location": "bardcore", "mood": "calm", "tempo": "medium",
     "choir": False, "start": "build"},
    {"slug": "city", "name": "City", "location": "city", "mood": "lively", "tempo": "medium",
     "choir": False, "start": "straight", "icon": "city",
     "prompt": "Orchestral music for a busy medieval marketplace. Lively and bustling, fast. Woodwinds play a "
               "playful, winding melody, with plucked strings and frame drums driving the energetic rhythm "
               "underneath. Spacious hall sound. Instrumental."},
    {"slug": "sea-battle", "name": "Sea battle", "location": "sea battle", "mood": "combat", "tempo": "fast",
     "choir": True, "start": "straight"},
    {"slug": "cathedral", "name": "Cathedral", "location": "cathedral", "mood": "calm", "tempo": "slow",
     "choir": False, "start": "build"},
    {"slug": "funeral", "name": "Funeral", "location": "temple", "mood": "sad", "tempo": "slow",
     "choir": True, "start": "build"},
    {"slug": "funny-combat", "name": "Funny combat", "location": "circus", "mood": "farcical", "tempo": "fast",
     "choir": False, "start": "build"},
    {"slug": "sexy-battle", "name": "Sexy battle", "location": "nightclub", "mood": "seductive", "tempo": "medium",
     "choir": False, "start": "build"},
]


def icon_slugs() -> set[str]:
    """Every icon / background set a scene may pick: the starter scenes' plus any picker-only set in the manifest."""
    slugs = {s.get("icon", s["slug"]) for s in STARTER_SCENES}   # a starter scene may borrow another's art
    try:
        m = json.loads((Path(__file__).parent / "static" / "bg" / "manifest.json").read_text())
        slugs |= set(m.get("sets", {})) | set(m.get("icons", {}))
    except (OSError, ValueError):
        pass
    return slugs


def art_for(scene: Scene) -> str | None:
    """Slug of the icon / background set that suits the scene (starter slugs are matched by name, location and mood), or None (generic)."""
    slugs = icon_slugs()
    if scene.icon in slugs:    # the DM picked it
        return scene.icon
    if scene.prompt.strip():
        return None
    own = re.sub(r"[^a-z0-9]+", "-", (scene.name or "").lower()).strip("-")
    if own in slugs:
        return own
    same = [s for s in STARTER_SCENES if s["location"] == scene.location]
    for s in same:
        if s["mood"] == scene.mood:
            return s.get("icon", s["slug"])
    return same[0].get("icon", same[0]["slug"]) if same else None
