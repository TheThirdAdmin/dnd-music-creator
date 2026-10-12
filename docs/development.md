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
| `generator/` | the music generator: `service.py` job queue, `comfy.py` ComfyUI, `modelfiles.py` downloads, `score.py` quality 3, `supervisor.py` start and restart |

Work happens on `main`, which is kept free of long-lived feature branches.
