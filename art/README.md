# art/

Prompts and tools for the M3 scene backgrounds.

- `prompts.yaml`: `style` (suffix for every prompt), `negative`, `sets.<slug>` (4 `{seed, prompt}` per starter scene) and `generic` (12, ids generic-01..12).
- Seeds: set i (1-based, starter order) uses `1000+100*i+j`; generic uses `9001..9012` (2001.. would clash with set 10).
- Rules: subject in the centre 55%; no people or readable text; <= 40 words per prompt; the 4 images of a set differ in time of day, camera or weather, not subject.
- Render and preview with `scripts/make_backgrounds.py`; The reviewer's verdicts go in each round's `notes.txt`, refine with `--only slug:n`.
- Changing a prompt or seed re-renders only that image; record the old seed in the commit message.
- Login picture `dndmusic/static/lock.webp` (1920x1080, lossy WebP q92): `rtx img -W 1920 -H 1080 --seed 12` with the prompt "16-bit pixel art scene, front view, symmetrical composition: a huge ornate wooden treasure chest with gold bands and a golden lock in the exact centre foreground of an orc cave dungeon, a muscular green orc with a club on the left, a horned red ogre holding a flaming torch on the right, glowing green crystals, blazing fire braziers in the corners, stone pillars with carved faces, flagstone floor, vivid colours, no text". Keep the chest centred (the form sits on it).
