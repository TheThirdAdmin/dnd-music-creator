import asyncio
import logging
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import datetime
import hashlib

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from .bundle import MAX_IMPORT_BYTES as MAX_UPLOAD_BYTES, BundleError, export_zip, import_zip
from .caster import Caster
from .config import Settings
from .director import Director
from .evictor import Plan
from .freetext import Rules, load_rules, text_to_scene
from .genclient import GeneratorClient
from .library import Library
from .mixer import Engine
from .models import STARTS, TEMPOS, DEFAULT_QUALITY, Scene
from .auth import COOKIE, DmGate, load_password, password_ok, session_token
from .prompts import MAX_WORDS, scene_prompt
from .scheduler import NEXT_FLOOR_S, NEXT_TARGET_S, PLAYING_FLOOR_S, PLAYING_TARGET_S, Scheduler
from .starter import art_for, icon_slugs, import_starter
from .streamer import AudioPipeline, Broadcaster
from .votes import Votes, VoteError
from .vocab import Vocab, VocabError, check_scenes, load_vocab

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
WATCHDOG_S = 10


class SceneIn(BaseModel):
    name: str
    location: str
    mood: str
    tempo: str = "medium"
    choir: bool = False
    start: str = "build"
    extra: str = Field("", max_length=200)
    prompt: str = Field("", max_length=800)
    icon: str = Field("", max_length=40)

    def to_scene(self, **overrides) -> Scene:
        return Scene(**{**self.model_dump(), **overrides})


class TextIn(BaseModel):
    text: str = Field(max_length=300)


class ApplyIn(BaseModel):
    scene_id: int | None = None
    pool_id: int | None = None
    scene: SceneIn | None = None
    fade_s: float = Field(3.0, ge=0, le=15)


class FadeIn(BaseModel):
    fade_s: float = Field(3.0, ge=0, le=15)


class LoginIn(BaseModel):
    password: str = Field("", max_length=100)


class QueueIn(BaseModel):
    ids: list[int]


class CastIn(BaseModel):
    targets: list[str]


class VolumeIn(BaseModel):
    volume: float = Field(ge=0, le=1)


class RatingIn(BaseModel):
    rating: str = Field(pattern="^(normal|fav|banned)$")


class PlayersIn(BaseModel):
    count: int = Field(ge=0, le=20)


class VoteIn(BaseModel):
    token: str = Field(max_length=64)
    track_id: int
    vote: str


class MoreIn(BaseModel):
    count: int = Field(ge=1, le=8)


class SettingsIn(BaseModel):
    quality: int | None = Field(None, ge=1, le=3)
    library_cap_gb: int | None = Field(None, ge=1, le=50)


@dataclass
class Services:
    settings: Settings
    lib: Library
    vocab: Vocab
    engine: Engine
    director: Director
    scheduler: Scheduler
    caster: Caster
    rules: Rules | None = None
    broadcaster: Broadcaster | None = None
    pipeline: AudioPipeline | None = None
    last_plan: Plan | None = None
    votes: Votes = field(default_factory=Votes)

    def enforce_cap(self) -> Plan:
        """Evict until under the cap; favourites, the playing track and each pool's last tracks are kept."""
        playing = self.director.now_playing()
        self.last_plan = self.lib.evict(self.lib.cap_bytes(), playing["track_id"] if playing else None)
        if self.last_plan.over_cap:
            log.warning("library over its cap: favourites and protected tracks use more than %d bytes",
                        self.lib.cap_bytes())
        return self.last_plan


def check_library(lib: Library, vocab: Vocab) -> None:
    """Saved scenes must fit vocab.yaml (VocabError otherwise). An unsaved Custom scene that doesn't is deleted."""
    check_scenes(vocab, lib.list_scenes())
    custom = lib.get_custom_scene()
    if custom is not None:
        try:
            check_scenes(vocab, [custom])
        except VocabError:
            lib.delete_scene(custom.id)


def build_services(settings: Settings, cast_backend=None, deck_factory=None) -> Services:
    lib = Library(settings.data_dir)
    vocab = load_vocab(settings.data_dir)
    rules = load_rules(settings.data_dir)
    check_library(lib, vocab)
    if settings.starter_dir is not None:
        import_starter(lib, vocab, settings.starter_dir)
    engine = Engine()
    director = Director(lib, engine, deck_factory=deck_factory)
    scheduler = Scheduler(lib, vocab, [GeneratorClient(u) for u in settings.generator_urls], director)
    if cast_backend is None:
        from .caster import PyChromecastBackend
        cast_backend = PyChromecastBackend()
    caster = Caster(cast_backend, lib, f"{settings.public_url}/stream.mp3")
    services = Services(settings, lib, vocab, engine, director, scheduler, caster, rules)
    scheduler.on_added = services.enforce_cap
    services.votes.players = lib.get_state("players", 0)
    return services


def scene_out(svc: Services, scene: Scene) -> dict:
    pool = svc.lib.pool_for(scene_prompt(scene, svc.vocab), scene_id=scene.id)
    return {**asdict(scene), "pool_id": pool.id, "ready": svc.lib.track_count(pool.id),
            "art": art_for(scene)}


def queue_ids(svc: Services) -> list[int]:
    """The queued scene ids that still exist (deleted scenes drop out). The first entry is the scene playing."""
    saved = {sc.id for sc in svc.lib.list_scenes()}
    return [i for i in svc.lib.get_state("queue", []) if i in saved]


def settle_queue(svc: Services) -> list[int]:
    """Once 'Next scene' has actually started the new scene (maybe after waiting for its first track),
    drop the entries before it, i.e. the card that was playing."""
    ids = queue_ids(svc)
    adv = svc.lib.get_state("queue_advance")   # {"id": scene to start, "drop": entries to drop once it plays}
    if adv is not None:
        scene = svc.lib.get_scene(adv["id"])
        pool = svc.lib.pool_for(scene_prompt(scene, svc.vocab), scene_id=scene.id) if scene else None
        if pool is None or adv["drop"] >= len(ids) or ids[adv["drop"]] != adv["id"]:
            svc.lib.set_state("queue_advance", None)
        elif svc.director.playing_pool_id == pool.id:
            ids = ids[adv["drop"]:]
            svc.lib.set_state("queue_advance", None)
    if ids != svc.lib.get_state("queue", []):
        svc.lib.set_state("queue", ids)
    return ids


def status_out(svc: Services, playing: dict | None, queue: list[dict]) -> list[dict]:
    """Ready music for the playing scene and each queued scene, against the warm-up targets."""
    sched = svc.scheduler
    rows: list[dict] = []
    playing_pool = playing["pool_id"] if playing else None
    nxt = sched.next_queued_pool()
    if playing_pool is not None:
        rows.append({"pool_id": playing_pool, "name": playing["name"], "role": "playing",
                     "target_s": PLAYING_TARGET_S, "floor_s": PLAYING_FLOOR_S})
    for q in queue:
        if q["pool_id"] == playing_pool:
            continue
        is_next = q["pool_id"] == nxt and not any(r["role"] == "next" for r in rows)
        rows.append({"pool_id": q["pool_id"], "name": q["name"], "role": "next" if is_next else "queued",
                     "target_s": NEXT_TARGET_S if is_next else None, "floor_s": NEXT_FLOOR_S if is_next else None})
    for r in rows:
        r["tracks"] = svc.lib.track_count(r["pool_id"])
        r["ready_s"] = round(svc.lib.ready_seconds(r["pool_id"]))
        r["generating"] = sched.in_flight.get(r["pool_id"], 0)
    return rows


AVG_TRACK_BYTES = 3_400_000


def library_out(svc: Services) -> dict:
    lib = svc.lib
    used, count = lib.usage()
    cap = lib.cap_bytes()
    avg = used // count if count else AVG_TRACK_BYTES
    scenes = len(lib.list_scenes())
    groups = [{"pool_id": p.id, "label": p.label or "Unlabelled", "tags": p.tags, "lyrics": p.lyrics,
               "straight": p.straight,
               "tracks": [{"id": t.id, "seed": t.seed, "quality": t.quality, "duration_s": t.duration_s,
                           "plays": t.plays} for t in tracks]} for p, tracks in lib.favourites()]
    banned = []
    for t in lib.banned_tracks():
        pool = lib.get_pool(t.pool_id)
        banned.append({"id": t.id, "pool_id": t.pool_id, "label": (pool.label if pool else None) or "Unlabelled",
                       "seed": t.seed, "duration_s": t.duration_s})
    return {"used_bytes": used, "cap_bytes": cap, "track_count": count, "data_path": svc.settings.host_data_path or str(svc.settings.data_dir),
            "over_cap": bool(svc.last_plan and svc.last_plan.over_cap) or used > cap,
            "cap_warning": cap < scenes * 3 * avg, "favourites": groups, "banned": banned}


def now_playing_out(svc: Services) -> dict | None:
    playing = svc.director.now_playing()
    if playing:
        applied = svc.director.applied
        pool = svc.lib.get_pool(playing["pool_id"])
        scene = svc.lib.get_scene(pool.scene_id) if pool and pool.scene_id else None
        playing["art"] = art_for(scene) if scene else None
        playing["icon"] = scene.icon if scene else ""
        track = svc.lib.get_track(playing["track_id"])
        playing["rating"] = track.rating if track else "normal"
        if applied and applied["pool_id"] == playing["pool_id"]:
            playing["name"] = applied["name"]
        else:
            playing["name"] = (scene.name if scene.saved else "Custom") if scene else "Unknown"
    return playing


def votes_out(svc: Services, playing: dict | None) -> dict:
    track_id = playing["track_id"] if playing else None
    svc.votes.sync(track_id)
    return svc.votes.tally(track_id)


def snapshot(svc: Services) -> dict:
    playing = now_playing_out(svc)
    pending = svc.director.pending_pool_id
    queue = [scene_out(svc, sc) for sc in (svc.lib.get_scene(i) for i in settle_queue(svc)) if sc]
    return {
        "now_playing": playing,
        "pending": {"pool_id": pending} if pending is not None else None,
        "scenes": [scene_out(svc, s) for s in svc.lib.list_scenes()],
        "queue": queue,
        "status": status_out(svc, playing, queue),
        "jobs": {str(p): n for p, n in svc.scheduler.in_flight.items() if n > 0},
        "generators": svc.scheduler.health,
        "disk_low": svc.scheduler.disk_low,
        "paused": svc.scheduler.paused,
        "cast": svc.caster.snapshot(),
        "listeners": svc.broadcaster.listener_count if svc.broadcaster else 0,
        "stream_url": f"{svc.settings.public_url}/stream.mp3",
        "quality": svc.lib.get_state("quality", DEFAULT_QUALITY),
        "library_cap_gb": svc.lib.cap_bytes() // 1024 ** 3,
        "votes": votes_out(svc, playing),
    }


async def snapshot_daily(lib: Library, interval_s: float = 24 * 3600) -> None:
    while True:
        try:
            await asyncio.to_thread(lib.snapshot_db)
        except Exception:
            log.exception("library snapshot")
        await asyncio.sleep(interval_s)


async def watchdog(caster: Caster, interval_s: float = WATCHDOG_S) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(caster.check)
        except Exception:
            log.exception("cast watchdog")


class RevalidatedStatic(StaticFiles):
    """Static files the browser must revalidate (ETag) on every load, so a deploy shows at once."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(build: Callable[[], Services], *, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = build()
        app.state.svc = svc
        if svc.settings.dm_password:
            load_password(svc.settings.data_dir)   # create it now so deploy.sh can show it
        tasks: list[asyncio.Task] = []
        try:
            if start_background:
                svc.broadcaster = Broadcaster(asyncio.get_running_loop())
                svc.pipeline = AudioPipeline(svc.engine, svc.broadcaster).start()
                for c in svc.scheduler.clients:
                    tasks.append(asyncio.create_task(svc.scheduler.worker(c)))
                    tasks.append(asyncio.create_task(svc.scheduler.health_loop(c)))
                tasks.append(asyncio.create_task(watchdog(svc.caster)))
                tasks.append(asyncio.create_task(snapshot_daily(svc.lib)))
                if not svc.scheduler.paused:
                    svc.director.restore()
                if svc.caster.casting:
                    tasks.append(asyncio.create_task(asyncio.to_thread(svc.caster.start, svc.caster.targets)))
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if svc.broadcaster:
                svc.broadcaster.hangup_all()
            if svc.pipeline:
                svc.pipeline.stop()
            for c in svc.scheduler.clients:
                await c.aclose()

    app = FastAPI(title="dnd-music", lifespan=lifespan)
    app.mount("/static", RevalidatedStatic(directory=STATIC, check_dir=False), name="static")

    def svc() -> Services:
        return app.state.svc

    def validated(body: SceneIn, **overrides) -> Scene:
        scene = body.to_scene(**{"prompt": body.prompt.strip(), **overrides})
        if scene.icon and scene.icon not in icon_slugs():
            raise HTTPException(422, f"unknown icon {scene.icon!r}")
        if len(scene.prompt.split()) > MAX_WORDS:
            raise HTTPException(422, f"the prompt is {len(scene.prompt.split())} words; the limit is {MAX_WORDS}")
        try:
            svc().vocab.validate(scene)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return scene

    def dm_password() -> str | None:
        s = getattr(app.state, "svc", None)
        if s is None or not s.settings.dm_password:
            return None
        return load_password(s.settings.data_dir)

    app.add_middleware(DmGate, get_token=lambda: (lambda pw: session_token(pw) if pw else None)(dm_password()))

    @app.get("/login")
    async def login_page():
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    async def login(body: LoginIn):
        pw = dm_password()
        if pw is None or password_ok(body.password, pw):
            r = JSONResponse({"ok": True})
            if pw is not None:
                r.set_cookie(COOKIE, session_token(pw), max_age=30 * 24 * 3600, httponly=True, samesite="lax")
            return r
        await asyncio.sleep(1)
        raise HTTPException(401, "wrong password")

    @app.post("/logout")
    async def logout():
        """A plain form post, so it works whatever app.js the browser has cached."""
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE, httponly=True, samesite="lax")
        return r

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/players")
    async def players_page():
        return FileResponse(STATIC / "players.html")

    def players_state(token: str = "") -> dict:
        s = svc()
        playing = now_playing_out(s)
        tally = votes_out(s, playing)
        return {"voting": tally["players"] > 0, **tally, "track_id": playing["track_id"] if playing else None,
                "name": playing["name"] if playing else None, "skipping": bool(playing) and s.votes.skipped_id == playing["track_id"],
                "my_vote": s.votes.mine(token) if playing else None}

    @app.get("/api/players/state")
    async def get_players_state(token: str = ""):
        return players_state(token)

    @app.post("/api/players/vote")
    async def player_vote(body: VoteIn):
        s = svc()
        playing = now_playing_out(s)
        votes_out(s, playing)
        try:
            s.votes.cast(body.track_id, body.token, body.vote)
        except VoteError as e:
            raise HTTPException(409 if str(e) in ("voting is off", "stale track") else 422, str(e))
        skipped = False
        if s.votes.skip_due(body.track_id):
            s.votes.skipped_id = body.track_id
            s.director.skip()
            skipped = True
        return {**players_state(body.token), "skipped": skipped}

    @app.put("/api/players")
    async def put_players(body: PlayersIn):
        s = svc()
        s.votes.set_players(body.count)
        s.lib.set_state("players", body.count)
        return {"players": body.count}

    @app.get("/api/vocab")
    async def get_vocab():
        v = svc().vocab
        return {"locations": list(v.locations), "moods": list(v.moods), "tempos": list(TEMPOS), "starts": list(STARTS)}

    @app.post("/api/scene-from-text")
    async def scene_from_text(body: TextIn):
        s = svc()
        try:
            scene = text_to_scene(body.text, s.vocab, s.rules)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {**asdict(scene), "art": art_for(scene)}

    @app.get("/api/state")
    async def get_state():
        return snapshot(svc())

    @app.get("/api/scenes")
    async def list_scenes():
        s = svc()
        return [scene_out(s, x) for x in s.lib.list_scenes()]

    @app.post("/api/scenes", status_code=201)
    async def create_scene(body: SceneIn):
        s = svc()
        scene = s.lib.create_scene(validated(body))
        s.scheduler.wake()
        return scene_out(s, scene)

    @app.put("/api/scenes/{scene_id}")
    async def update_scene(scene_id: int, body: SceneIn):
        s = svc()
        old = s.lib.get_scene(scene_id)
        if old is None or not old.saved:
            raise HTTPException(404, "no such scene")
        scene = s.lib.update_scene(validated(body, id=scene_id))
        s.scheduler.wake()
        return scene_out(s, scene)

    @app.delete("/api/scenes/{scene_id}", status_code=204)
    async def delete_scene(scene_id: int):
        svc().lib.delete_scene(scene_id)

    @app.post("/api/apply")
    async def apply(body: ApplyIn):
        s = svc()
        if body.pool_id is not None:
            pool = s.lib.get_pool(body.pool_id)
            if pool is None:
                raise HTTPException(404, "no such pool")
            switched = s.director.apply_pool(pool.id, pool.label or "Favourite", body.fade_s)
            s.scheduler.wake()
            return {"switched": switched, "pending": not switched}
        if body.scene_id is not None:
            scene = s.lib.get_scene(body.scene_id)
            if scene is None:
                raise HTTPException(404, "no such scene")
        elif body.scene is not None:
            scene = s.lib.custom_scene(validated(body.scene, saved=False))
        else:
            raise HTTPException(422, "scene_id or scene is required")
        switched = s.director.apply_scene(scene, s.vocab, body.fade_s)
        s.scheduler.wake()
        return {"switched": switched, "pending": not switched}

    @app.put("/api/queue")
    async def put_queue(body: QueueIn):
        s = svc()
        known = {sc.id for sc in s.lib.list_scenes()}
        if any(i not in known for i in body.ids):
            raise HTTPException(404, "no such scene")
        s.lib.set_state("queue", body.ids)
        return {"ids": settle_queue(s)}

    @app.post("/api/queue/next")
    async def queue_next(body: FadeIn):
        s = svc()
        ids = settle_queue(s)
        if not ids:
            raise HTTPException(409, "the queue is empty")
        head_pool = s.lib.pool_for(scene_prompt(s.lib.get_scene(ids[0]), s.vocab), scene_id=ids[0])
        if s.director.playing_pool_id != head_pool.id:
            drop = 0                 # nothing from the queue is playing yet: start the first entry
        elif len(ids) >= 2:
            drop = 1
        else:
            raise HTTPException(409, "nothing queued after the playing scene")
        target = ids[drop]
        scene = s.lib.get_scene(target)
        s.lib.set_state("queue_advance", {"id": target, "drop": drop})
        switched = s.director.apply_scene(scene, s.vocab, body.fade_s)
        s.scheduler.wake()
        settle_queue(s)
        return {"switched": switched, "pending": not switched}

    @app.post("/api/stop")
    async def stop(body: FadeIn):
        """Stop playing and rendering so the GPU idles; a job already running finishes. Persisted."""
        s = svc()
        s.lib.set_state("paused", True)
        s.director.fadeout(body.fade_s)
        return {"paused": True}

    @app.post("/api/start")
    async def start(body: FadeIn):
        """Resume rendering; if nothing is playing, start the first queued scene."""
        s = svc()
        s.lib.set_state("paused", False)
        s.scheduler.wake()
        if s.director.playing_pool_id is None:
            try:
                await queue_next(body)
            except HTTPException:
                pass   # empty queue: nothing to start
        return {"paused": False}

    @app.post("/api/fadeout")
    async def fadeout(body: FadeIn):
        svc().director.fadeout(body.fade_s)
        return {"ok": True}

    @app.post("/api/skip")
    async def skip():
        svc().director.skip()
        return {"ok": True}

    @app.get("/api/cast/devices")
    async def cast_devices():
        return [asdict(d) for d in await asyncio.to_thread(svc().caster.devices)]

    @app.post("/api/cast")
    async def cast_start(body: CastIn):
        await asyncio.to_thread(svc().caster.start, body.targets)
        return svc().caster.snapshot()

    @app.post("/api/cast/stop")
    async def cast_stop():
        await asyncio.to_thread(svc().caster.stop)
        return svc().caster.snapshot()

    def settings_out() -> dict:
        lib = svc().lib
        return {"quality": lib.get_state("quality", DEFAULT_QUALITY), "library_cap_gb": lib.cap_bytes() // 1024 ** 3}

    @app.get("/api/settings")
    async def get_settings():
        return settings_out()

    @app.put("/api/settings")
    async def put_settings(body: SettingsIn):
        s = svc()
        if body.quality is None and body.library_cap_gb is None:
            raise HTTPException(422, "quality or library_cap_gb is required")
        if body.quality is not None:
            s.lib.set_state("quality", body.quality)
        if body.library_cap_gb is not None:
            s.lib.set_cap_gb(body.library_cap_gb)
            await asyncio.to_thread(s.enforce_cap)
        s.scheduler.wake()
        return settings_out()

    @app.put("/api/tracks/{track_id}/rating")
    async def put_rating(track_id: int, body: RatingIn):
        s = svc()
        if s.lib.get_track(track_id) is None:
            raise HTTPException(404, "no such track")
        track = s.director.rate(track_id, body.rating)
        s.scheduler.wake()
        return {"id": track.id, "rating": track.rating}

    @app.post("/api/tracks/{track_id}/more")
    async def more_like_this(track_id: int, body: MoreIn):
        s = svc()
        track = s.lib.get_track(track_id)
        if track is None:
            raise HTTPException(404, "no such track")
        s.scheduler.queue_more(track.pool_id, body.count)
        return {"queued": body.count, "pool_id": track.pool_id}

    @app.delete("/api/tracks/{track_id}", status_code=204)
    async def delete_track(track_id: int):
        s = svc()
        track = s.lib.get_track(track_id)
        if track is None:
            raise HTTPException(404, "no such track")
        if track.rating != "banned":
            raise HTTPException(409, "only banned tracks can be deleted")
        s.lib.delete_track(track_id)

    @app.get("/api/tracks/{track_id}/audio")
    async def track_audio(track_id: int):
        track = svc().lib.get_track(track_id)
        if track is None or not Path(track.path).is_file():
            raise HTTPException(404, "no such track")
        return FileResponse(track.path, media_type="audio/ogg")

    @app.get("/api/library")
    async def get_library():
        return library_out(svc())

    @app.get("/api/library/export")
    async def library_export(ids: str | None = None):
        s = svc()
        try:
            wanted = [int(x) for x in ids.split(",") if x] if ids else None
        except ValueError:
            raise HTTPException(422, "ids must be comma-separated track ids")
        tmp = s.settings.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        dest = tmp / f"export-{uuid.uuid4().hex}.zip"
        vocab_file = s.settings.data_dir / "vocab.yaml"
        vocab_hash = hashlib.sha256(vocab_file.read_bytes()).hexdigest()[:16] if vocab_file.is_file() else None
        await asyncio.to_thread(export_zip, s.lib, wanted, dest, vocab_hash)
        name = f"dnd-music-favourites-{datetime.date.today():%Y%m%d}.zip"
        return FileResponse(dest, media_type="application/zip", filename=name,
                            background=BackgroundTask(lambda: dest.unlink(missing_ok=True)))

    @app.post("/api/library/import")
    async def library_import(request: Request):
        s = svc()
        tmp = s.settings.data_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        dest = tmp / f"import-{uuid.uuid4().hex}.zip"
        try:
            size = 0
            with open(dest, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "upload too large")
                    f.write(chunk)
            try:
                result = await asyncio.to_thread(import_zip, s.lib, dest, max_bytes=MAX_UPLOAD_BYTES)
            except BundleError as e:
                raise HTTPException(422, str(e))
        finally:
            dest.unlink(missing_ok=True)
        await asyncio.to_thread(s.enforce_cap)
        s.scheduler.wake()
        return result

    @app.post("/api/library/cleanup")
    async def library_cleanup():
        s = svc()
        before = s.lib.usage()[0]
        hashes = {scene_prompt(x, s.vocab).hash for x in s.lib.list_scenes()}
        custom = s.lib.get_custom_scene()
        if custom is not None:
            hashes.add(scene_prompt(custom, s.vocab).hash)
        for pid in (s.director.playing_pool_id, s.director.pending_pool_id):
            pool = s.lib.get_pool(pid) if pid is not None else None
            if pool is not None:
                hashes.add(pool.prompt_hash)
        gone = await asyncio.to_thread(s.lib.cleanup_unreachable, hashes)
        await asyncio.to_thread(s.enforce_cap)
        return {"deleted_tracks": len(gone), "freed_bytes": before - s.lib.usage()[0]}

    @app.post("/api/volume")
    async def volume(body: VolumeIn):
        await asyncio.to_thread(svc().caster.set_volume, body.volume)
        return svc().caster.snapshot()

    @app.get("/stream.mp3")
    async def stream():
        b = svc().broadcaster
        if b is None:
            raise HTTPException(503, "stream not running")

        async def body():
            q = b.subscribe()
            try:
                while (chunk := await q.get()) is not None:
                    yield chunk
            finally:
                b.unsubscribe(q)

        return StreamingResponse(body(), media_type="audio/mpeg",
                                 headers={"Cache-Control": "no-cache, no-store", "icy-name": "D&D music"})

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await sock.send_json(snapshot(svc()))
                await asyncio.sleep(1)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app
