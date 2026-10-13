# Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest            # unit, API and end-to-end tests; needs ffmpeg/ffprobe
.venv/bin/pytest -m gpu     # real-model test; needs a running generator with a GPU at GENERATOR_URL (default http://localhost:8001)
scripts/make_starter.py     # renders the starter pack through a generator
scripts/check_tracks.py     # duration and loudness of audio files
scripts/smoke.sh            # builds the containers, plays a scene, checks the stream
```

The code is in a few small modules:

| Module | What it covers |
|---|---|
| `dndmusic/library.py` | storage |
| `prompts.py`, `vocab.py` | scene to prompt |
| `scheduler.py` + `genclient.py` | generation |
| `ingest.py` | trim, loudness, Opus |
| `mixer.py` + `director.py` | transitions |
| `streamer.py` | the MP3 stream |
| `caster.py` | Chromecast |
| `app.py` | the HTTP API and web page |
| `starter.py` | starter pack import |
| `fakegen/` | the test generator |
| `dndmusic/runpod/` | Runpod backend: `client.py` job API, `service.py` activity and snapshot, `cost.py` estimates, `guard.py` brakes, `balance.py` GraphQL balance, `config.py` `runpod.json` |
| `scripts/runpod_setup.py` | creates or removes the endpoint, template and volume (REST), or prints the manual steps |
| `generator/runpod_worker.py` | the Runpod worker entrypoint: proxies jobs to the normal generator (image target `runpod`) |
| `generator/` | the music generator: `service.py` job queue, `comfy.py` ComfyUI, `modelfiles.py` downloads, `score.py` quality 3, `supervisor.py` start and restart |

**Runpod tests** (`tests/test_runpod_*.py`, `tests/test_app_runpod.py`): all use `httpx.MockTransport` and the recorded replies in `tests/fixtures/runpod/`; nothing calls Runpod. A real run needs an account: `scripts/runpod_setup.py --dry-run` prints the plan for free.

**Publishing the worker image.** `.github/workflows/runpod-image.yml` builds `docker/generator.Dockerfile` (target `runpod`) and pushes `ghcr.io/<owner>/dnd-music-generator:<tag>` when a `v*` tag is pushed to the public GitHub repository, using the repository's `GITHUB_TOKEN`. After the first push, set the package visibility to Public (GitHub profile, Packages, `dnd-music-generator`, Package settings) so Runpod can pull it without credentials. The setup script's default tag is `DEFAULT_IMAGE_TAG` in `scripts/runpod_setup.py` (`m8-rc1`, the only tag published); change it when a new tag is published and test with `docker manifest inspect`. User guide: [Runpod backend](runpod.md).

Work happens on `main`, which is kept free of long-lived feature branches.
