#!/usr/bin/env bash
# Builds and starts conductor + fake generator, creates a scene, checks the MP3 stream.
set -euo pipefail
cd "$(dirname "$0")/.."
export DM_PASSWORD=off DATA_PATH APP_UID APP_GID; DATA_PATH="$(mktemp -d)"; APP_UID="$(id -u)"; APP_GID="$(id -g)"
base=http://localhost:${PORT:-8000}
GENERATOR_PORT=${GENERATOR_PORT:-8001}

trap 'docker compose --profile fake down; rm -rf "$DATA_PATH"' EXIT
docker compose --profile fake up -d --build

fail() {
  echo "smoke FAILED: $1" >&2
  docker compose --profile fake logs --tail 50 >&2 || true
  exit 1
}

up=
for _ in $(seq 1 60); do curl -sf "$base/api/state" >/dev/null && { up=1; break; }; sleep 1; done
[ -n "$up" ] || fail "conductor did not answer $base/api/state within 60 s"

id=$(curl -sf -X POST "$base/api/scenes" -H 'content-type: application/json' \
  -d '{"name":"Smoke","location":"castle hall","mood":"tense"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -sf -X POST "$base/api/apply" -H 'content-type: application/json' -d "{\"scene_id\":$id,\"fade_s\":2}" >/dev/null

playing=
for _ in $(seq 1 90); do
  curl -sf "$base/api/state" | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["now_playing"] else 1)' && { playing=1; break; }
  sleep 1
done
[ -n "$playing" ] || fail "nothing started playing within 90 s"

out=$(mktemp --suffix=.mp3)
curl -s --max-time 5 "$base/stream.mp3" -o "$out" || true
ffprobe -v error -show_entries stream=codec_name,sample_rate -of csv=p=0 "$out" | grep -qx 'mp3,48000'

curl -sf "http://localhost:${GENERATOR_PORT}/health" | python3 -c 'import json,sys; h=json.load(sys.stdin); sys.exit(0 if h["qualities"] == [1, 2, 3] and h["status"] == "ok" else 1)' \
  || fail "generator health lacks the M2 fields"

echo "smoke OK: stream is mp3 at 48 kHz"
