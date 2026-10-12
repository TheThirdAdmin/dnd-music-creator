"""Export and import of favourites as a zip: manifest.json plus the Opus files."""
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from .ingest import RejectedAudio, check_audio
from .library import Library
from .models import QUALITIES, Prompt

VERSION = 1
MAX_MANIFEST_BYTES = 8 << 20
MAX_IMPORT_BYTES = 2 << 30
MEMBER = re.compile(r"^files/[A-Za-z0-9_.-]+\.opus$")


class BundleError(ValueError):
    """The zip is not an acceptable favourites bundle."""


def export_zip(lib: Library, track_ids: list[int] | None, dest: Path, vocab_hash: str | None) -> int:
    """Write favourites (all, or those of track_ids) to dest; returns the number of tracks."""
    entries, n = [], 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as z:
        for pool, tracks in lib.favourites():
            for t in tracks:
                if track_ids is not None and t.id not in track_ids:
                    continue
                if not Path(t.path).is_file():
                    continue
                n += 1
                name = f"files/t{n:04d}.opus"
                z.write(t.path, name)
                entries.append({"file": name, "tags": pool.tags, "lyrics": pool.lyrics, "straight": pool.straight,
                                "label": pool.label, "seed": t.seed, "quality": t.quality,
                                "duration_s": t.duration_s, "rating": "fav", "source": t.generator})
        z.writestr("manifest.json", json.dumps({"version": VERSION, "vocab_hash": vocab_hash, "tracks": entries},
                                               indent=1))
    return n


def _text(v, limit: int, field: str) -> str:
    if not isinstance(v, str) or len(v) > limit:
        raise BundleError(f"bad manifest field {field}")
    return v


def import_zip(lib: Library, src: Path, *, max_bytes: int = MAX_IMPORT_BYTES,
               check: Callable[[Path], float] = check_audio) -> dict:
    """Add the bundle's tracks as favourites. Returns {added, duplicates, rejected, pools_created}."""
    try:
        z = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as e:
        raise BundleError(f"not a zip file: {e}")
    with z:
        infos = {i.filename: i for i in z.infolist()}
        if sum(i.file_size for i in infos.values()) > max_bytes:
            raise BundleError("bundle is too large")
        mi = infos.get("manifest.json")
        if mi is None or mi.file_size > MAX_MANIFEST_BYTES:
            raise BundleError("manifest.json missing or too large")
        try:
            manifest = json.loads(z.read(mi))
        except ValueError as e:
            raise BundleError(f"manifest.json unreadable: {e}")
        if not isinstance(manifest, dict) or manifest.get("version") != VERSION:
            raise BundleError(f"unsupported bundle version {manifest.get('version') if isinstance(manifest, dict) else None!r}")
        entries = manifest.get("tracks")
        if not isinstance(entries, list):
            raise BundleError("manifest has no track list")
        known = lib.ensure_hashes()
        result = {"added": 0, "duplicates": 0, "rejected": [], "pools_created": 0}
        tmp_root = lib.data_dir / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        for e in entries:
            name = e.get("file") if isinstance(e, dict) else None
            label_name = name if isinstance(name, str) else "?"

            def reject(reason: str) -> None:
                result["rejected"].append({"file": label_name[:80], "reason": reason})

            if not isinstance(name, str) or not MEMBER.match(name) or name not in infos:
                reject("bad file name")
                continue
            try:
                tags, lyrics = _text(e.get("tags"), 4000, "tags"), _text(e.get("lyrics", ""), 4000, "lyrics")
                label = e.get("label")
                label = _text(label, 100, "label") if label is not None else None
                straight = bool(e.get("straight", False))
                seed = e.get("seed") if isinstance(e.get("seed"), int) and not isinstance(e.get("seed"), bool) else None
                quality = e.get("quality") if e.get("quality") in QUALITIES else None
            except BundleError as err:
                reject(str(err))
                continue
            with tempfile.TemporaryDirectory(dir=tmp_root) as d:
                part = Path(d) / "t.opus"
                with z.open(infos[name]) as f, open(part, "wb") as out:
                    shutil.copyfileobj(f, out)
                try:
                    duration = check(part)
                except RejectedAudio as err:
                    reject(str(err))
                    continue
                digest = hashlib.sha256(part.read_bytes()).hexdigest()
                if digest in known:
                    result["duplicates"] += 1
                    continue
                prompt = Prompt(tags, lyrics, straight)
                pool = lib.find_pool(prompt.hash)
                if pool is None:
                    result["pools_created"] += 1
                pool = lib.pool_for(prompt, kind="scene", scene_id=None)
                dest = lib.new_track_path()
                shutil.move(part, dest)
            track = lib.add_track(pool.id, dest, duration, seed, "import", quality=quality)
            lib.set_track_hash(track.id, digest)
            lib.set_rating(track.id, "fav")
            if label:
                lib.set_pool_label(pool.id, label)
            known.add(digest)
            result["added"] += 1
    return result
