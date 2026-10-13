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
| `GENERATOR_MODE` | Remembered generator choice (`gpu`, `cpu`, `remote`, `test` or `runpod`); read by `deploy.sh` only. With `runpod`, `GENERATOR_URLS` is empty. |
| `COMPOSE_PROFILES` | `gpu`, `cpu` or `fake` for the generator on this computer; empty when it runs elsewhere. |

## Runpod

The Runpod endpoint and key normally live in `runpod.json` in the library folder (written by setup or Settings > Runpod), not in `.env`. See [Runpod backend](runpod.md). These variables exist too:

| Variable | Meaning |
|---|---|
| `RUNPOD_API_KEY` | Restricted key. Read by the conductor process; wins over `runpod.json` and is never written to it. The stock `docker-compose.yml` does not pass it into the container: use Settings > Runpod. |
| `RUNPOD_ENDPOINT_ID` | Endpoint id, same rules as the key. |
| `RUNPOD_API_BASE` | Job API base (default `https://api.runpod.ai/v2`). Tests and demos only. |
| `RUNPOD_IMAGE` | Generator image the setup script puts in the template (default `ghcr.io/thethirdadmin/dnd-music-generator:v0.1.0`); `deploy.sh --runpod` passes it through. A name without a tag gets the default tag. |
| `RUNPOD_MGMT_BASE` | Runpod management API for the setup script (default `https://rest.runpod.io/v1`). Tests and demos only. |
| `RUNPOD_SETUP_DRY_RUN` | `1`: the setup prints its plan and price and creates nothing. |

To change how locations, moods and tempos are described to the music model, edit `vocab.yaml` in the library folder, then run `docker compose restart conductor` in the install folder. A prompt is cut to 70 words (your extra words are always kept), and changed text makes new track pools: the old tracks stay, but scenes with new text get new music.
