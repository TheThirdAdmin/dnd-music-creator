# Configuration

`deploy.sh` writes the settings to `.env` in the install folder. Re-run `./deploy.sh` with options rather than editing it by hand.

| Setting | Meaning |
|---|---|
| `PORT` | Web page and stream port (default 8000). |
| `PUBLIC_URL` | Address speakers use to fetch the stream, e.g. `http://192.168.1.20:8000`. |
| `GENERATOR_URLS` | Music generator address(es), comma-separated. |
| `GENERATOR_PORT` | Generator port on this computer (default 8001). |
| `GENERATOR_GPU` | Which GPU the generator uses: index or UUID. |
| `GENERATOR_BIND` | `127.0.0.1`, or `0.0.0.0` with `--generator-only`. |
| `GENERATOR_ONLY` | `1` when only the generator runs here. |
| `DATA_PATH` | Library folder. Its size cap is set in Settings in the web page (1-50 GB), not here. |
| `COMPOSE_PROFILES` | `gpu`, `cpu` or `fake` for the generator on this computer; empty when it runs elsewhere. |

To change how locations, moods and tempos are described to the music model, edit `vocab.yaml` in the library folder, then run `docker compose restart conductor` in the install folder. A prompt is cut to 70 words (your extra words are always kept), and changed text makes new track pools: the old tracks stay, but scenes with new text get new music.
