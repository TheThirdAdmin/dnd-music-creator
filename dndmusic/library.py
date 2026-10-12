import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import evictor
from .models import RATINGS, Pool, Prompt, Scene, Track

SCHEMA_M1 = """
CREATE TABLE IF NOT EXISTS scene (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  genre TEXT NOT NULL,
  tones TEXT NOT NULL,
  intensity INTEGER NOT NULL,
  vocals TEXT NOT NULL,
  extra TEXT NOT NULL DEFAULT '',
  saved INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pool (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  scene_id INTEGER,
  pad_id INTEGER,
  x INTEGER,
  y INTEGER,
  tags TEXT NOT NULL,
  lyrics TEXT NOT NULL,
  prompt_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS track (
  id INTEGER PRIMARY KEY,
  pool_id INTEGER NOT NULL REFERENCES pool(id),
  path TEXT NOT NULL,
  duration_s REAL NOT NULL,
  seed INTEGER,
  generator TEXT NOT NULL,
  rating TEXT NOT NULL DEFAULT 'normal',
  plays INTEGER NOT NULL DEFAULT 0,
  last_played REAL
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _migrate_v1(db: sqlite3.Connection, data_dir: Path) -> None:
    """Track paths relative to data_dir, so a library folder can move."""
    for row in db.execute("SELECT id, path FROM track").fetchall():
        p = Path(row["path"])
        if not p.is_absolute():
            continue
        for base in (data_dir, data_dir.resolve()):
            try:
                rel = p.relative_to(base)
            except ValueError:
                continue
            db.execute("UPDATE track SET path=? WHERE id=?", (rel.as_posix(), row["id"]))
            break


SCENE_TABLE = """
CREATE TABLE scene (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  location TEXT NOT NULL,
  mood TEXT NOT NULL,
  tempo TEXT NOT NULL,
  choir INTEGER NOT NULL DEFAULT 0,
  start TEXT NOT NULL DEFAULT 'build',
  extra TEXT NOT NULL DEFAULT '',
  saved INTEGER NOT NULL DEFAULT 1
)"""

GENRE_TO_LOCATION = {
    "High orchestral": "castle hall", "Slavic folk orchestral": "forest road", "Celtic folk": "forest road",
    "Medieval tavern": "tavern", "Dark ambient": "crypt", "Choral/sacred": "temple",
    "Nordic war drums": "battlefield", "Desert/eastern": "desert",
}
TONE_TO_MOOD = {
    "tense": "tense", "dark": "dread", "eerie": "dread", "triumphant": "triumphant", "epic": "triumphant",
    "heroic": "triumphant", "melancholic": "sad", "mysterious": "mysterious", "peaceful": "calm", "playful": "calm",
}


def _m1_tempo(intensity: int) -> str:
    return "slow" if intensity <= 2 else "medium" if intensity == 3 else "fast"


def _migrate_v2(db: sqlite3.Connection, data_dir: Path) -> None:
    """Scenes: genre/tones/intensity/vocals -> location/mood/tempo/choir/start. Adds track.quality, pool.straight.
    Unknown genres become castle hall, unknown or missing tones calm."""
    rows = db.execute("SELECT * FROM scene").fetchall()
    db.execute("ALTER TABLE scene RENAME TO scene_m1")
    db.execute(SCENE_TABLE)
    for r in rows:
        tones = json.loads(r["tones"])
        db.execute("INSERT INTO scene (id, name, location, mood, tempo, choir, start, extra, saved) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   (r["id"], r["name"], GENRE_TO_LOCATION.get(r["genre"], "castle hall"),
                    TONE_TO_MOOD.get(tones[0], "calm") if tones else "calm", _m1_tempo(r["intensity"]),
                    int(r["vocals"] == "choir"), "build", r["extra"], r["saved"]))
    db.execute("DROP TABLE scene_m1")
    db.execute("ALTER TABLE track ADD COLUMN quality INTEGER")
    db.execute("ALTER TABLE pool ADD COLUMN straight INTEGER NOT NULL DEFAULT 0")


def _migrate_v3(db: sqlite3.Connection, data_dir: Path) -> None:
    """M5: pinned pools (a favourite keeps its prompt), pool label, track file hash for import de-duplication."""
    db.execute("ALTER TABLE pool ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    db.execute("ALTER TABLE pool ADD COLUMN label TEXT")
    db.execute("ALTER TABLE track ADD COLUMN sha256 TEXT")
    db.execute("UPDATE pool SET pinned=1 WHERE id IN (SELECT pool_id FROM track WHERE rating='fav')")
    db.execute("UPDATE pool SET label=(SELECT name FROM scene WHERE scene.id=pool.scene_id) WHERE pinned=1")


def _migrate_v4(db: sqlite3.Connection, data_dir: Path) -> None:
    """Scenes can carry a whole hand-written prompt (advanced mode)."""
    db.execute("ALTER TABLE scene ADD COLUMN prompt TEXT NOT NULL DEFAULT ''")


def _migrate_v5(db: sqlite3.Connection, data_dir: Path) -> None:
    """Scenes can pick the icon (and so the background set) of a starter scene."""
    db.execute("ALTER TABLE scene ADD COLUMN icon TEXT NOT NULL DEFAULT ''")


MIGRATIONS = [_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5]
SCHEMA_VERSION = len(MIGRATIONS)


class Library:
    """Scenes, pools, tracks and app state in SQLite; audio files under data_dir."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.tracks_dir = self.data_dir / "tracks"
        self.banned_dir = self.data_dir / "banned"
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        self.banned_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sizes: dict[int, int] = {}
        self._db = sqlite3.connect(self.data_dir / "dnd-music.db", check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA_M1)
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        for n, step in enumerate(MIGRATIONS[version:], start=version + 1):
            self._db.execute("BEGIN")
            try:
                step(self._db, self.data_dir)
                self._db.execute(f"PRAGMA user_version={n}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _rel(self, path: Path | str) -> str:
        p = Path(path)
        try:
            return p.relative_to(self.data_dir).as_posix()
        except ValueError:
            return str(p)

    def _abs(self, stored: str) -> str:
        return str(self.data_dir / stored)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # scenes

    @staticmethod
    def _scene(r: sqlite3.Row) -> Scene:
        return Scene(name=r["name"], location=r["location"], mood=r["mood"], tempo=r["tempo"], choir=bool(r["choir"]),
                     start=r["start"], extra=r["extra"], prompt=r["prompt"], icon=r["icon"], saved=bool(r["saved"]), id=r["id"])

    @staticmethod
    def _scene_args(s: Scene) -> tuple:
        return (s.name, s.location, s.mood, s.tempo, int(s.choir), s.start, s.extra, s.prompt, s.icon, int(s.saved))

    def list_scenes(self) -> list[Scene]:
        return [self._scene(r) for r in self._all("SELECT * FROM scene WHERE saved=1 ORDER BY name COLLATE NOCASE")]

    def get_scene(self, scene_id: int) -> Scene | None:
        r = self._one("SELECT * FROM scene WHERE id=?", (scene_id,))
        return self._scene(r) if r else None

    def create_scene(self, scene: Scene) -> Scene:
        cur = self._exec("INSERT INTO scene (name, location, mood, tempo, choir, start, extra, prompt, icon, saved) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", self._scene_args(scene))
        return self.get_scene(cur.lastrowid)

    def update_scene(self, scene: Scene) -> Scene:
        self._exec("UPDATE scene SET name=?, location=?, mood=?, tempo=?, choir=?, start=?, extra=?, prompt=?, icon=?, saved=? WHERE id=?",
                   (*self._scene_args(scene), scene.id))
        return self.get_scene(scene.id)

    def delete_scene(self, scene_id: int) -> None:
        self._exec("DELETE FROM scene WHERE id=?", (scene_id,))

    def get_custom_scene(self) -> Scene | None:
        r = self._one("SELECT * FROM scene WHERE saved=0")
        return self._scene(r) if r else None

    def custom_scene(self, scene: Scene) -> Scene:
        """The one unsaved 'Custom' scene, overwritten on every custom Apply."""
        scene.saved = False
        r = self._one("SELECT id FROM scene WHERE saved=0")
        if r is None:
            return self.create_scene(scene)
        scene.id = r["id"]
        return self.update_scene(scene)

    # pools

    @staticmethod
    def _pool(r: sqlite3.Row) -> Pool:
        return Pool(r["id"], r["kind"], r["scene_id"], r["tags"], r["lyrics"], r["prompt_hash"], bool(r["straight"]),
                    bool(r["pinned"]), r["label"])

    def pool_for(self, prompt: Prompt, kind: str = "scene", scene_id: int | None = None) -> Pool:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO pool (kind, scene_id, tags, lyrics, prompt_hash, straight) "
                             "VALUES (?,?,?,?,?,?)",
                             (kind, scene_id, prompt.tags, prompt.lyrics, prompt.hash, int(prompt.straight)))
            return self._pool(self._db.execute("SELECT * FROM pool WHERE prompt_hash=?", (prompt.hash,)).fetchone())

    def find_pool(self, prompt_hash: str) -> Pool | None:
        r = self._one("SELECT * FROM pool WHERE prompt_hash=?", (prompt_hash,))
        return self._pool(r) if r else None

    def set_pool_label(self, pool_id: int, label: str) -> None:
        self._exec("UPDATE pool SET label=? WHERE id=? AND label IS NULL", (label, pool_id))

    def ensure_hashes(self) -> set[str]:
        """sha256 of every track file (computed once per track, then stored); used to de-duplicate imports."""
        out: set[str] = set()
        for r in self._all("SELECT id, path, sha256 FROM track"):
            digest = r["sha256"]
            if digest is None:
                try:
                    h = hashlib.sha256()
                    with open(self._abs(r["path"]), "rb") as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            h.update(block)
                except OSError:
                    continue
                digest = h.hexdigest()
                self._exec("UPDATE track SET sha256=? WHERE id=?", (digest, r["id"]))
            out.add(digest)
        return out

    def set_track_hash(self, track_id: int, digest: str) -> None:
        self._exec("UPDATE track SET sha256=? WHERE id=?", (digest, track_id))

    def get_pool(self, pool_id: int) -> Pool | None:
        r = self._one("SELECT * FROM pool WHERE id=?", (pool_id,))
        return self._pool(r) if r else None

    # tracks

    def _track(self, r: sqlite3.Row) -> Track:
        return Track(id=r["id"], pool_id=r["pool_id"], path=self._abs(r["path"]), duration_s=r["duration_s"], seed=r["seed"],
                     generator=r["generator"], rating=r["rating"], plays=r["plays"], last_played=r["last_played"], quality=r["quality"])

    def new_track_path(self) -> Path:
        return self.tracks_dir / f"{uuid.uuid4().hex}.opus"

    def add_track(self, pool_id: int, path: Path | str, duration_s: float, seed: int | None, generator: str,
                  quality: int | None = None) -> Track:
        cur = self._exec("INSERT INTO track (pool_id, path, duration_s, seed, generator, quality) VALUES (?,?,?,?,?,?)",
                         (pool_id, self._rel(path), duration_s, seed, generator, quality))
        return self.get_track(cur.lastrowid)

    def get_track(self, track_id: int) -> Track | None:
        r = self._one("SELECT * FROM track WHERE id=?", (track_id,))
        return self._track(r) if r else None

    def tracks(self, pool_id: int, include_banned: bool = False) -> list[Track]:
        sql = "SELECT * FROM track WHERE pool_id=?" + ("" if include_banned else " AND rating != 'banned'")
        return [self._track(r) for r in self._all(sql + " ORDER BY id", (pool_id,))]

    def track_count(self, pool_id: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM track WHERE pool_id=? AND rating != 'banned'", (pool_id,))["n"]

    def ready_seconds(self, pool_id: int) -> float:
        return self._one("SELECT COALESCE(SUM(duration_s), 0) AS s FROM track WHERE pool_id=? AND rating != 'banned'",
                         (pool_id,))["s"]

    def recent_ids(self, pool_id: int, n: int = 2) -> list[int]:
        rows = self._all("SELECT id FROM track WHERE pool_id=? AND last_played IS NOT NULL "
                         "ORDER BY last_played DESC LIMIT ?", (pool_id, n))
        return [r["id"] for r in rows]

    def fresh_count(self, pool_id: int) -> int:
        recent = set(self.recent_ids(pool_id))
        return sum(1 for t in self.tracks(pool_id) if t.id not in recent)

    def has_fav(self, pool_id: int) -> bool:
        return self._one("SELECT 1 FROM track WHERE pool_id=? AND rating='fav' LIMIT 1", (pool_id,)) is not None

    def record_play(self, track_id: int, now: float | None = None) -> None:
        self._exec("UPDATE track SET plays = plays + 1, last_played=? WHERE id=?",
                   (time.time() if now is None else now, track_id))

    def set_rating(self, track_id: int, rating: str) -> Track:
        if rating not in RATINGS:
            raise ValueError(f"unknown rating {rating!r}")
        track = self.get_track(track_id)
        if track is None:
            raise KeyError(track_id)
        path = Path(track.path)
        target = self.banned_dir if rating == "banned" else self.tracks_dir
        if path.parent != target and path.exists():
            moved = target / path.name
            path.rename(moved)
            path = moved
        with self._lock:
            self._db.execute("UPDATE track SET rating=?, path=? WHERE id=?", (rating, self._rel(path), track_id))
            if rating == "fav":
                self._db.execute(
                    "UPDATE pool SET pinned=1, label=COALESCE(label, (SELECT name FROM scene WHERE scene.id=pool.scene_id)) "
                    "WHERE id=?", (track.pool_id,))
            else:
                self._db.execute("UPDATE pool SET pinned=? WHERE id=?",
                                 (int(self.has_fav(track.pool_id)), track.pool_id))
        return self.get_track(track_id)

    def track_bytes(self, track: Track) -> int:
        size = self._sizes.get(track.id)
        if size is None:
            try:
                size = os.stat(track.path).st_size
            except OSError:
                return 0
            self._sizes[track.id] = size
        return size

    def delete_track(self, track_id: int) -> int:
        """Remove row and file; returns bytes freed (0 if unknown)."""
        track = self.get_track(track_id)
        if track is None:
            return 0
        size = self.track_bytes(track)
        self._sizes.pop(track_id, None)
        Path(track.path).unlink(missing_ok=True)
        self._exec("DELETE FROM track WHERE id=?", (track_id,))
        return size

    def delete_pool(self, pool_id: int) -> None:
        """Delete an unpinned pool with all its tracks."""
        pool = self.get_pool(pool_id)
        if pool is None:
            return
        if pool.pinned:
            raise ValueError("pool is pinned")
        for t in self.tracks(pool_id, include_banned=True):
            self.delete_track(t.id)
        self._exec("DELETE FROM pool WHERE id=?", (pool_id,))

    def favourites(self) -> list[tuple[Pool, list[Track]]]:
        rows = self._all("SELECT * FROM pool WHERE pinned=1 ORDER BY label COLLATE NOCASE, id")
        out = []
        for r in rows:
            favs = [t for t in self.tracks(r["id"]) if t.rating == "fav"]
            if favs:
                out.append((self._pool(r), favs))
        return out

    def banned_tracks(self) -> list[Track]:
        return [self._track(r) for r in self._all("SELECT * FROM track WHERE rating='banned' ORDER BY id")]

    def snapshot_db(self, keep: int = 7) -> Path:
        """Consistent copy of the database (SQLite backup API) into data_dir/backup/, one file per day."""
        out_dir = self.data_dir / "backup"
        out_dir.mkdir(exist_ok=True)
        dest = out_dir / f"dnd-music-{time.strftime('%Y%m%d')}.db"
        part = dest.with_name(dest.name + ".part")
        part.unlink(missing_ok=True)
        target = sqlite3.connect(part)
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()
        os.replace(part, dest)
        for old in sorted(out_dir.glob("dnd-music-*.db"))[:-keep]:
            old.unlink(missing_ok=True)
        return dest

    # size cap

    DEFAULT_CAP_BYTES = 5 * 1024 ** 3

    def _all_tracks(self) -> list[Track]:
        return [self._track(r) for r in self._all("SELECT * FROM track ORDER BY id")]

    def usage(self) -> tuple[int, int]:
        """(audio bytes, track count) over tracks/ and banned/, from the size cache."""
        tracks = self._all_tracks()
        return sum(self.track_bytes(t) for t in tracks), len(tracks)

    def cap_bytes(self) -> int:
        return self.get_state("library_cap_bytes", self.DEFAULT_CAP_BYTES)

    def set_cap_gb(self, gb: int) -> None:
        if not 1 <= gb <= 50:
            raise ValueError("library cap must be 1-50 GB")
        self.set_state("library_cap_bytes", gb * 1024 ** 3)

    def protected_ids(self, playing_track_id: int | None = None) -> set[int]:
        """Never evicted: the playing track, and per pool the last 2 plays and the last 2 tracks."""
        keep = {playing_track_id} if playing_track_id is not None else set()
        by_pool: dict[int, list[Track]] = {}
        for t in self._all_tracks():
            if t.rating != "banned":
                by_pool.setdefault(t.pool_id, []).append(t)
        for tracks in by_pool.values():
            played = sorted((t for t in tracks if t.last_played is not None), key=lambda t: t.last_played)
            keep.update(t.id for t in played[-2:])
            keep.update(t.id for t in tracks[-2:])
        return keep

    def evict(self, cap_bytes: int, playing_track_id: int | None = None) -> evictor.Plan:
        tracks = self._all_tracks()
        items = [evictor.Item(t.id, t.pool_id, t.rating, self.track_bytes(t), t.last_played) for t in tracks]
        result = evictor.plan(items, cap_bytes, self.protected_ids(playing_track_id))
        for track_id in result.delete_ids:
            self.delete_track(track_id)
        return result

    def cleanup_unreachable(self, in_use_hashes: set[str]) -> list[int]:
        """Delete pools that are neither pinned nor in use, with their tracks; returns the deleted track ids."""
        gone: list[int] = []
        for r in self._all("SELECT id, prompt_hash FROM pool WHERE pinned=0"):
            if r["prompt_hash"] in in_use_hashes:
                continue
            gone.extend(t.id for t in self.tracks(r["id"], include_banned=True))
            self.delete_pool(r["id"])
        return gone

    # state

    def get_state(self, key: str, default=None):
        r = self._one("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_state(self, key: str, value) -> None:
        self._exec("INSERT INTO state (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
