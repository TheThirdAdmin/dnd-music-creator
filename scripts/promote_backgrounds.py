#!/usr/bin/env python3
"""Promote approved background rounds into dndmusic/static/bg/ and rebuild manifest.json.

  scripts/promote_backgrounds.py ROUND_DIR [ROUND_DIR ...] [--only slug[:n] generic[:nn] icons[:slug] ...]

Round dirs are in PRECEDENCE order: for each image the last dir that has it wins. The list is the approval.
Round layout: <slug>/<n>.webp, generic/<nn>.webp, icons/<slug>.webp (other files ignored). A webp in any other subdir is an error. Files already in
static/bg that no round supplies are left alone; the manifest is rebuilt from what is on disk (sorted).
Manifest: {"sets": {slug: [paths]}, "generic": [paths], "icons": {slug: path}}, paths relative to /static/bg/."""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BG = ROOT / "dndmusic" / "static" / "bg"
HD = ROOT / "dndmusic" / "static" / "bg-hd"  # 1920x1080 desktop copies, same relative paths as the manifest

from dndmusic.starter import STARTER_SCENES  # noqa: E402

import yaml  # noqa: E402

_EXTRA = set((yaml.safe_load((ROOT / "art" / "prompts.yaml").read_text()).get("extra_sets") or {}))
ALLOWED = {s["slug"] for s in STARTER_SCENES} | _EXTRA | {"generic", "icons"}


def _key(p: Path):
    return (p.parent.name, int(p.stem) if p.stem.isdigit() else p.stem, p.stem)


def collect(rounds: list[Path]) -> dict[str, Path]:
    """rel path ('slug/n.webp') -> source file; later rounds replace earlier."""
    out: dict[str, Path] = {}
    for rd in rounds:
        if not rd.is_dir():
            sys.exit(f"not a directory: {rd}")
        for f in sorted(rd.glob("*/*.webp")):
            if f.parent.name not in ALLOWED:
                sys.exit(f"{f}: '{f.parent.name}' is not a starter slug, generic or icons")
            out[f"{f.parent.name}/{f.name}"] = f
    return out


def wanted(rel: str, only: list[str] | None) -> bool:
    if not only:
        return True
    d, name = rel[:-5].split("/")
    for o in only:
        od, _, on = o.partition(":")
        if od == d and (not on or on == name):
            return True
    return False


def build_manifest(bg: Path = BG) -> dict:
    sets: dict[str, list[str]] = {}
    generic: list[str] = []
    icons: dict[str, str] = {}
    for d in sorted(p for p in bg.iterdir() if p.is_dir()):
        files = sorted(d.glob("*.webp"), key=_key)
        rels = [f"{d.name}/{f.name}" for f in files]
        if d.name == "generic":
            generic = rels
        elif d.name == "icons":
            icons = {f.stem: r for f, r in zip(files, rels)}
        elif rels:
            sets[d.name] = rels
    return {"sets": sets, "generic": generic, "icons": icons}


def write_manifest(m: dict, bg: Path = BG) -> None:
    lines = ['{"sets": {']
    items = [f'  {json.dumps(k)}: {json.dumps(v)}' for k, v in m["sets"].items()]
    lines.append(",\n".join(items))
    lines.append(' },\n "generic": ' + json.dumps(m["generic"]) + ",")
    lines.append(' "icons": {' + ",\n    ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in m["icons"].items()) + "}}")
    (bg / "manifest.json").write_text("\n".join(lines) + "\n")
    json.loads((bg / "manifest.json").read_text())


def main(argv=None, bg: Path = BG) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rounds", nargs="+", type=Path, metavar="ROUND_DIR")
    ap.add_argument("--only", nargs="+", metavar="SLUG[:N]")
    ap.add_argument("--hd", action="store_true", help="promote a --wide round into static/bg-hd (no manifest change)")
    a = ap.parse_args(argv)
    if a.hd:
        bg = HD
    added = replaced = same = 0
    for rel, src in sorted(collect(a.rounds).items()):
        if not wanted(rel, a.only):
            continue
        dst = bg / rel
        if dst.exists():
            if dst.read_bytes() == src.read_bytes():
                same += 1
                continue
            replaced += 1
        else:
            added += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    if not a.hd:
        write_manifest(build_manifest(bg), bg)
    files = list(bg.glob("*/*.webp"))
    total = sum(f.stat().st_size for f in files)
    print(f"added {added}, replaced {replaced}, unchanged {same}; {len(files)} files, {total / 1024:.0f} KB total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
