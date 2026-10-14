# How it works

```text
 Phone / laptop ──web page + live status──►  conductor (Docker, this computer)
                                              ├─ library    scenes, tracks, settings (SQLite + audio files)
                                              ├─ scheduler  asks the generator for more tracks, most urgent first
                                              ├─ mixer      crossfades tracks in real time
                                              ├─ stream     one continuous MP3 stream at /stream.mp3
                                              └─ caster     points your Chromecast/Google speakers at the stream
                                                    │ HTTP job API
                                                    ▼
                                              music generator (Docker, a computer with an NVIDIA GPU): Stable Audio 3 via ComfyUI
```

- **Scenes become prompts.** A scene becomes a short English description (about 40 words, at most 70) put together from `vocab.yaml` in your library folder: the location, the mood, the tempo, optional choir and straight-in sentences, and your extra words. You can tune the text without touching code. New tracks are trimmed, straight-in scenes lose their slow opening, and loudness is matched to -16 LUFS in two passes.
- **The library fills itself.** The same prompt always maps to the same *pool* of tracks. The scheduler keeps every pool topped up, in this order:
  1. the scene you just applied;
  2. the scene that's playing, so it doesn't repeat;
  3. three tracks for every saved scene.

  Every track is loudness-matched, so switching scenes doesn't jump in volume.
- **Smooth transitions.** The mixer keeps two "decks". Apply starts an equal-power crossfade, which avoids the dip in the middle that a plain crossfade has. When a track nears its end, the next track from the same scene crossfades in over 8 s, so a scene can play all night without repeating the same track back to back.
- **One stream for everything.** The mix is encoded once into an MP3 stream. Speakers, browsers and VLC all play the same stream, and a new listener gets the last couple of seconds straight away so it starts quickly.
- **Speakers stay on.** A watchdog checks the chosen speakers every 10 s. If one stopped, or someone cast something else to it, the app starts the stream again. A speaker that fails or cannot be found shows in the red banner at the top of the page. An unreachable speaker never blocks Stop, Cast or the other speakers: device calls are serialised per device and the state lock is not held while waiting on one. A speaker whose Cast listener hangs (seen on an LG soundbar) usually needs a power cycle.
- **The generator is replaceable.** The app talks to any generator through four simple HTTP calls: submit a job, check it, fetch the audio, and a health check. The built-in test generator (`--test-generator`) implements them with tones. A third implementation is [Runpod](runpod.md): `dndmusic/runpod/` submits each job to a serverless endpoint over Runpod's HTTP API, which runs the same generator image, and the conductor adds cost estimates, a balance read and spending brakes (`guard.py`) around it. With Runpod there is no generator container on your computer.
- **The generator.** A small web service in its own container. It starts ComfyUI inside the container and asks it to render with Stable Audio 3, one track at a time. It downloads the models on first start and checks them. After a minute without work it frees the graphics card's memory, so the card can do other things between tracks.

**Favourites and the size cap.** A track rated `fav` pins its pool (`pool.pinned`, `pool.label`): the prompt is kept even when the scene is deleted or edited. After every new track, and when the cap changes, `evictor.plan` picks tracks to delete: banned first, then played tracks by least recently played, then never-played ones; favourites, the playing track, each pool's last 2 plays and last 2 tracks are protected. `bundle.py` writes and reads the favourites zip (manifest version 1, de-duplicated by file hash and prompt hash). `dnd-music.db` is copied daily to `data/backup/`.

**Player voting.** `votes.py` keeps votes in memory for the playing track only (one per device token, reset when the track changes). `POST /api/players/vote` skips the track when `dislikes * 2 > players` (`state.players`, 0 = off). The `/players` page polls `GET /api/players/state` every 2 s.

Everything the app keeps lives in the library folder (`~/dnd-music-creator/data` by default): `dnd-music.db`, the audio in `tracks/`, and `vocab.yaml`.
