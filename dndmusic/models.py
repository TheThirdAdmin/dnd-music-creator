import hashlib
from dataclasses import dataclass, field

TEMPOS = ("slow", "medium", "fast")
STARTS = ("build", "straight")
RATINGS = ("normal", "fav", "banned")
QUALITIES = (1, 2, 3)
DEFAULT_QUALITY = 2


@dataclass
class Scene:
    name: str
    location: str
    mood: str
    tempo: str = "medium"
    choir: bool = False
    start: str = "build"
    extra: str = ""
    prompt: str = ""   # advanced mode: the whole music prompt, written by the DM; replaces the vocab text
    icon: str = ""     # starter-scene slug picked by the DM: its icon and background set are used
    saved: bool = True
    id: int | None = None


@dataclass(frozen=True)
class Prompt:
    tags: str
    lyrics: str = ""
    straight: bool = field(default=False, compare=False)

    @property
    def hash(self) -> str:
        return hashlib.sha256(f"{self.tags}\n--\n{self.lyrics}".encode()).hexdigest()[:16]


@dataclass
class Pool:
    id: int
    kind: str
    scene_id: int | None
    tags: str
    lyrics: str
    prompt_hash: str
    straight: bool = False
    pinned: bool = False
    label: str | None = None


@dataclass
class Track:
    id: int
    pool_id: int
    path: str
    duration_s: float
    seed: int | None
    generator: str
    rating: str = "normal"
    plays: int = 0
    last_played: float | None = None
    quality: int | None = None
