#!/usr/bin/env bash
# deploy.sh: install or update dnd-music-creator and run it with Docker Compose.
# Run ./deploy.sh --help for the options. Safe to re-run: it updates the code,
# rebuilds, restarts and keeps your settings and music library.
set -euo pipefail

APP_NAME="dnd-music-creator"
REPO_URL="${DND_REPO_URL:-https://github.com/TheThirdAdmin/dnd-music-creator.git}"
STARTER_URL="${DND_STARTER_URL:-https://github.com/TheThirdAdmin/dnd-music-creator/releases/download/starter-pack/starter-pack.tar.gz}"   # optional tar.gz of the starter music (a release asset); used when git LFS has none
BRANCH="main"
INSTALL_DIR=""
PORT=""
GEN_MODE=""          # test | gpu | cpu | remote
GENERATOR=""         # URL(s) for remote
GPU_ID=""
GPU_COUNT=0
GENERATOR_ONLY=""    # 1 = only the generator runs here
ALL_PROFILES=(--profile fake --profile gpu --profile cpu)
LIBRARY_DIR=""
ASSUME_YES=0
FIREWALL="ask"
DM_PASSWORD=""        # on | off ("" keeps the previous choice, default on)
SHOW_PASSWORD=0
DOCKER=()
COMPOSE=()

if [ -t 1 ]; then
  BOLD=$'\e[1m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; CYAN=$'\e[36m'; RESET=$'\e[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '\n%sError:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Install or update ${APP_NAME} and start it with Docker Compose.

Usage: ./deploy.sh [options]

  --dir DIR          where to install (default: this checkout when deploy.sh is
                     run from inside one, otherwise ~/${APP_NAME})
  --port PORT        port for the web interface and the audio stream (default 8000)
  --gpu ID           run the music generator on this computer's NVIDIA GPU: an index
                     (0, 1, ...) or a UUID from 'nvidia-smi -L'. The default when
                     Docker can use a GPU.
  --cpu-generator    run the music generator on the CPU: slow, and only the lowest
                     quality ("Primary school guitar lesson")
  --generator URL    use a music generator on another computer (comma-separate several)
  --test-generator   use the built-in test generator (test tones; nothing to download)
  --generator-only   run only the music generator here, for the app on another computer
                     (remembered; undo with --with-app). It has no password: use it
                     on a trusted network only.
  --with-app         run the app here too (undoes --generator-only)
  --library DIR      where the music library is kept (default: DIR/data); point it
                     at a library saved by uninstall.sh --preserve-library
  --branch NAME      git branch to deploy (default: main)
  --repo URL         git repository (default: ${REPO_URL})
  --no-firewall      never change firewall rules
  --no-password      turn the DM password off (default: on; players never need it)
  --password         turn the DM password back on
  --show-password    print the DM password and exit
  -y, --yes          answer yes to every question (unattended install)
  -h, --help         show this help
EOF
}

confirm() {  # confirm "Question?": returns 0 for yes. Reads the terminal even when piped.
  [ "$ASSUME_YES" = 1 ] && return 0
  local answer=""
  printf '  %s [y/N] ' "$1"
  read -r answer </dev/tty 2>/dev/null || true
  case "$answer" in [yY] | [yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) INSTALL_DIR="${2:?--dir needs a directory}"; shift 2 ;;
      --port) PORT="${2:?--port needs a number}"; shift 2 ;;
      --gpu) GEN_MODE="gpu"; GPU_ID="${2:?--gpu needs a GPU index or UUID}"; shift 2 ;;
      --cpu-generator) GEN_MODE="cpu"; shift ;;
      --generator) GEN_MODE="remote"; GENERATOR="${2:?--generator needs a URL}"; shift 2 ;;
      --test-generator) GEN_MODE="test"; shift ;;
      --generator-only) GENERATOR_ONLY=1; shift ;;
      --with-app) GENERATOR_ONLY=0; shift ;;
      --library) LIBRARY_DIR="${2:?--library needs a directory}"; shift 2 ;;
      --branch) BRANCH="${2:?--branch needs a name}"; shift 2 ;;
      --repo) REPO_URL="${2:?--repo needs a URL}"; shift 2 ;;
      --no-firewall) FIREWALL="no"; shift ;;
      --no-password) DM_PASSWORD="off"; shift ;;
      --password) DM_PASSWORD="on"; shift ;;
      --show-password) SHOW_PASSWORD=1; shift ;;
      -y | --yes) ASSUME_YES=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done
  if [ -n "$PORT" ]; then
    case "$PORT" in *[!0-9]* | "") die "--port must be a number" ;; esac
  fi
  case "$GPU_ID" in *[!A-Za-z0-9-]*) die "--gpu takes an index (0, 1, ...) or a GPU UUID from nvidia-smi -L" ;; esac
}

pkg_install_cmd() {
  if command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y"
  elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install -y"
  elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S --noconfirm"
  elif command -v zypper >/dev/null 2>&1; then echo "sudo zypper install -y"
  fi
}

need_tool() {  # need_tool COMMAND PACKAGE
  if command -v "$1" >/dev/null 2>&1; then ok "$1"; return; fi
  local pm; pm="$(pkg_install_cmd)"
  if [ -n "$pm" ] && confirm "$1 is missing. Install it now with '$pm $2'?"; then
    case "$pm" in "sudo apt-get"*) sudo apt-get update -qq ;; esac
    # shellcheck disable=SC2086  # $pm is a command line on purpose
    $pm "$2"
    ok "$1 installed"
  else
    die "$1 is required. Install the '$2' package and run deploy.sh again."
  fi
}

check_system() {
  step "Checking your system"
  [ "$(uname -s)" = "Linux" ] || die "deploy.sh supports Linux only: it relies on Docker host networking to find speakers on your network."
  if grep -qi microsoft /proc/version 2>/dev/null; then
    warn "WSL detected: phones and speakers on your network may not reach it, because WSL sits behind Windows networking."
  fi
  need_tool git git
  need_tool curl curl
  command -v ip >/dev/null 2>&1 || warn "'ip' (iproute2) is missing: can't list your network addresses"

  if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<'EOF'

  Docker is not installed. On most Linux distributions the quickest way is:
      curl -fsSL https://get.docker.com | sudo sh
      sudo usermod -aG docker "$USER"      # then log out and back in
  Details: https://docs.docker.com/engine/install/
EOF
    die "install Docker, then run deploy.sh again"
  fi
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  else
    derr="$(docker info 2>&1 | grep -i 'error\|denied' | head -n 1 | cut -c1-160)" || true
    if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
      warn "docker as $USER failed (${derr:-no message}); using sudo (fix: sudo usermod -aG docker $USER, then log in again)"
      DOCKER=(sudo docker)
    else
      die "can't reach Docker${derr:+ ($derr)}. Start it with: sudo systemctl enable --now docker"
    fi
  fi
  ok "Docker $("${DOCKER[@]}" version --format '{{.Server.Version}}' 2>/dev/null || echo '(version unknown)')"

  if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
    COMPOSE=("${DOCKER[@]}" compose)
  elif command -v docker-compose >/dev/null 2>&1 && docker-compose version --short 2>/dev/null | grep -q '^v\?2'; then
    COMPOSE=(docker-compose)
    [ "${DOCKER[0]}" = "sudo" ] && COMPOSE=(sudo docker-compose)
  else
    die "Docker Compose v2 is missing. Install the 'docker-compose-plugin' package (https://docs.docker.com/compose/install/linux/)."
  fi
  ok "Docker Compose $("${COMPOSE[@]}" version --short 2>/dev/null || echo '')"

  if ! "${DOCKER[@]}" buildx version >/dev/null 2>&1; then  # optional: Compose builds faster and caches better with it
    local pm pkg=""; pm="$(pkg_install_cmd)"
    case "$pm" in
      "sudo apt-get"*) for c in docker-buildx docker-buildx-plugin; do
                         apt-cache show "$c" >/dev/null 2>&1 && { pkg="$c"; break; }; done ;;
      "sudo dnf"* | "sudo zypper"*) pkg="docker-buildx-plugin" ;;
      "sudo pacman"*) pkg="docker-buildx" ;;
    esac
    if [ -n "$pkg" ] && confirm "Docker buildx is missing (faster, cached image builds). Install '$pkg' now?"; then
      # shellcheck disable=SC2086  # $pm is a command line on purpose
      { case "$pm" in "sudo apt-get"*) sudo apt-get update -qq ;; esac; $pm "$pkg"; } ||
        warn "couldn't install $pkg; builds will use the classic builder"
    else
      warn "Docker buildx is missing: builds still work, just slower. Install '${pkg:-docker-buildx-plugin}' to speed them up."
    fi
    "${DOCKER[@]}" buildx version >/dev/null 2>&1 && ok "Docker buildx"
  else
    ok "Docker buildx"
  fi

  case "$("${DOCKER[@]}" info --format '{{.OperatingSystem}}' 2>/dev/null || true)" in
    *"Docker Desktop"*)
      warn "Docker Desktop detected: it runs containers in a VM, so speakers may not be found and phones may not connect. Plain Docker Engine works best." ;;
  esac
  if command -v systemctl >/dev/null 2>&1 && ! systemctl is-enabled --quiet docker 2>/dev/null; then
    warn "Docker doesn't start at boot. To bring the app back after a reboot: sudo systemctl enable docker"
  fi
}

resolve_install_dir() {
  if [ -z "$INSTALL_DIR" ]; then
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
    if [ -n "$here" ] && [ -f "$here/docker-compose.yml" ] && [ -d "$here/.git" ]; then
      INSTALL_DIR="$here"
    else
      INSTALL_DIR="$HOME/$APP_NAME"
    fi
  fi
  INSTALL_DIR="$(realpath -m "$INSTALL_DIR")"
}

fetch_code() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    step "Updating the code in $INSTALL_DIR"
    if [ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]; then
      die "$INSTALL_DIR has local changes. Commit or discard them (git -C $INSTALL_DIR status) and run again."
    fi
    git -C "$INSTALL_DIR" fetch --quiet origin "$BRANCH" || die "git fetch failed (check your network and access to $REPO_URL)"
    git -C "$INSTALL_DIR" checkout --quiet "$BRANCH"
    git -C "$INSTALL_DIR" merge --quiet --ff-only "origin/$BRANCH" ||
      die "can't fast-forward $BRANCH in $INSTALL_DIR; sort it out with git there and run again"
  elif [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "$INSTALL_DIR already exists and isn't a $APP_NAME checkout. Pick another place with --dir."
  else
    step "Downloading $APP_NAME into $INSTALL_DIR"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" || die "git clone failed (check the URL and your access)"
  fi
  starter_count() { find "$INSTALL_DIR/starter" -name '*.opus' -size +4k 2>/dev/null | wc -l; }
  if [ "$(starter_count)" -eq 0 ] && [ -n "$STARTER_URL" ]; then
    step "Downloading the starter music"
    if curl -fsSL "$STARTER_URL" | tar -xz -C "$INSTALL_DIR/starter"; then :; else
      warn "starter music download failed ($STARTER_URL)"
    fi
  fi
  if [ "$(starter_count)" -gt 0 ]; then
    ok "starter music: $(starter_count) tracks"
  else
    warn "no starter music: the app will generate music for the starter scenes instead"
  fi
  ok "code: $(git -C "$INSTALL_DIR" log -1 --format='%h %s' | cut -c1-72)"
}

dm_password_file() { echo "$(env_get DATA_PATH)/dm-password"; }

print_password() {  # the app writes the file on its first start; wait a little for it
  local f i; f="$(dm_password_file)"
  for i in 1 2 3 4 5 6 7 8 9 10; do [ -s "$f" ] && break; sleep 1; done
  if [ -s "$f" ]; then
    printf '  %sDM password:%s %s%s%s   (players never need it; show it again with ./deploy.sh --show-password)\n\n' \
      "$BOLD" "$RESET" "$CYAN" "$(cat "$f")" "$RESET"
  else
    warn "the DM password isn't there yet: run ./deploy.sh --show-password in a minute"
  fi
}

env_get() {  # env_get KEY: value from the previous .env, if any
  [ -f "$INSTALL_DIR/.env" ] || return 0
  sed -n "s/^$1=//p" "$INSTALL_DIR/.env" | tail -n 1
}

port_busy() { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }

primary_ip() { ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1; }

lan_ips() {  # the host's IPv4 addresses, primary first, without container/VM bridges
  {
    primary_ip
    ip -4 -o addr show scope global 2>/dev/null |
      awk '$2 !~ /^(docker|br-|veth|virbr|cni|flannel|lxc|podman|vnet)/ { split($4, a, "/"); print a[1] }'
  } | awk 'NF && !seen[$0]++'
}

check_gpu() {  # 0 when Docker can use an NVIDIA GPU; lists the GPUs
  local out
  echo "  Checking that Docker can use an NVIDIA GPU (downloads a small CUDA image the first time)..."
  if out="$("${DOCKER[@]}" run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi -L 2>&1)"; then
    GPU_COUNT="$(grep -c '^GPU ' <<<"$out" || true)"
    ok "Docker can use the GPU:"
    sed 's/^/      /' <<<"$out"
    return 0
  fi
  cat >&2 <<'EOF'

  Docker can't use an NVIDIA GPU here. The GPU music generator needs:
    1. the NVIDIA driver, version 580 or newer (check: nvidia-smi).
       Ubuntu/Kubuntu: sudo ubuntu-drivers install, then reboot.
    2. the NVIDIA Container Toolkit, so containers can use the GPU:
       https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html
       then: sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
EOF
  tail -n 3 <<<"$out" | sed 's/^/  docker: /' >&2
  return 1
}

choose_settings() {
  step "Choosing settings"
  local running=""
  running="$(cd "$INSTALL_DIR" && "${COMPOSE[@]}" "${ALL_PROFILES[@]}" ps -q 2>/dev/null || true)"

  [ -n "$GENERATOR_ONLY" ] || GENERATOR_ONLY="$(env_get GENERATOR_ONLY)"
  GENERATOR_ONLY="${GENERATOR_ONLY:-0}"
  PORT="${PORT:-$(env_get PORT)}"; PORT="${PORT:-8000}"
  if [ -z "$running" ] && [ "$GENERATOR_ONLY" != 1 ] && command -v ss >/dev/null 2>&1 && port_busy "$PORT"; then
    die "port $PORT is already in use by another program. Pick another with --port (e.g. --port 8080)."
  fi
  ok "port $PORT"

  GENERATOR_PORT="$(env_get GENERATOR_PORT)"; GENERATOR_PORT="${GENERATOR_PORT:-$(env_get FAKEGEN_PORT)}"
  GENERATOR_PORT="${GENERATOR_PORT:-8001}"
  if [ -z "$running" ] && command -v ss >/dev/null 2>&1; then
    while port_busy "$GENERATOR_PORT" || [ "$GENERATOR_PORT" = "$PORT" ]; do
      GENERATOR_PORT=$((GENERATOR_PORT + 1))
      [ "$GENERATOR_PORT" -le 8099 ] || die "no free port for the music generator between 8001 and 8099"
    done
  fi

  if [ -z "$GEN_MODE" ]; then
    case "$(env_get COMPOSE_PROFILES)" in
      *fake*) GEN_MODE="test" ;;
      *gpu*) GEN_MODE="gpu" ;;
      *cpu*) GEN_MODE="cpu" ;;
      *)
        GENERATOR="$(env_get GENERATOR_URLS)"
        if [ -n "$GENERATOR" ]; then GEN_MODE="remote"; else GEN_MODE="gpu"; fi ;;
    esac
  fi
  if [ "$GENERATOR_ONLY" = 1 ] && { [ "$GEN_MODE" = "remote" ] || [ "$GEN_MODE" = "test" ]; }; then
    die "--generator-only runs a real music generator here: use it with --gpu ID or --cpu-generator"
  fi
  if [ "$GEN_MODE" = "gpu" ]; then
    if check_gpu; then
      GPU_ID="${GPU_ID:-$(env_get GENERATOR_GPU)}"; GPU_ID="${GPU_ID:-0}"
      [ "$GPU_COUNT" -gt 1 ] && warn "this computer has $GPU_COUNT GPUs; the generator uses GPU $GPU_ID (choose with --gpu)"
    elif confirm "Use the CPU music generator instead (slow, lowest quality only)?"; then
      GEN_MODE="cpu"
    else
      die "fix the GPU setup above and run deploy.sh again, or choose --cpu-generator, --generator URL or --test-generator"
    fi
  fi
  case "$GEN_MODE" in
    test) GENERATOR_URLS="http://localhost:$GENERATOR_PORT"
          ok "music generator: built-in test generator (test tones, port $GENERATOR_PORT on this computer only)" ;;
    gpu) GENERATOR_URLS="http://localhost:$GENERATOR_PORT"
         ok "music generator: Stable Audio 3 on GPU $GPU_ID (port $GENERATOR_PORT)" ;;
    cpu) GENERATOR_URLS="http://localhost:$GENERATOR_PORT"
         ok "music generator: Stable Audio 3 Small on the CPU (port $GENERATOR_PORT)" ;;
    remote) GENERATOR_URLS="$GENERATOR"
            ok "music generator: $GENERATOR_URLS" ;;
  esac
  GENERATOR_BIND="127.0.0.1"
  if [ "$GENERATOR_ONLY" = 1 ]; then
    GENERATOR_BIND="0.0.0.0"
    warn "generator only: other computers can reach it on port $GENERATOR_PORT, without a password"
  fi

  DM_PASSWORD="${DM_PASSWORD:-$(env_get DM_PASSWORD)}"; DM_PASSWORD="${DM_PASSWORD:-on}"
  LIBRARY_DIR="${LIBRARY_DIR:-$(env_get DATA_PATH)}"; LIBRARY_DIR="${LIBRARY_DIR:-$INSTALL_DIR/data}"
  LIBRARY_DIR="$(realpath -m "$LIBRARY_DIR")"
  mkdir -p "$LIBRARY_DIR" 2>/dev/null || die "can't create the library folder $LIBRARY_DIR"
  if [ -n "$(find "$LIBRARY_DIR" -maxdepth 2 ! -user "$(id -u)" -print -quit 2>/dev/null)" ]; then
    warn "some files in $LIBRARY_DIR belong to another user (an older install ran as root)"
    if confirm "Give them back to $(id -un) with sudo chown?"; then
      sudo chown -R "$(id -u):$(id -g)" "$LIBRARY_DIR"
    else
      die "the app runs as $(id -un) and needs to write to $LIBRARY_DIR"
    fi
  fi
  ok "music library: $LIBRARY_DIR"

  mapfile -t IPS < <(lan_ips)
  if [ "${#IPS[@]}" -eq 0 ]; then
    warn "no network address found: only this computer will reach the app, and speakers can't play the stream"
    PUBLIC_URL="http://localhost:$PORT"
  else
    PUBLIC_URL="http://${IPS[0]}:$PORT"
    ok "speakers will fetch the stream from $PUBLIC_URL/stream.mp3"
  fi
}

check_disk() {
  step "Checking disk space"
  local need=1 what="the app" root free_gb
  case "$GEN_MODE" in
    gpu) need=28; what="the app and the GPU generator (image about 12 GB, models 12.7 GB)" ;;
    cpu) need=18; what="the app and the CPU generator (image about 12 GB, models 3.5 GB)" ;;
  esac
  root="$("${DOCKER[@]}" info --format '{{.DockerRootDir}}' 2>/dev/null || true)"; root="${root:-/var/lib/docker}"
  free_gb="$(df -P -BG "$root" 2>/dev/null | awk 'NR == 2 { sub("G", "", $4); print $4 }' || true)"
  if [ -z "$free_gb" ]; then
    warn "can't tell how much space is free in $root"
    return
  fi
  if [ "$free_gb" -ge "$need" ]; then
    ok "$free_gb GB free for Docker ($root); $what needs about $need GB"
    return
  fi
  warn "only $free_gb GB free for Docker ($root); $what needs about $need GB on a first install (less when updating)"
  confirm "Continue anyway?" || die "free some space (docker system prune removes unused images) and run deploy.sh again"
}

write_env() {
  local profiles=""
  case "$GEN_MODE" in test) profiles="fake" ;; gpu) profiles="gpu" ;; cpu) profiles="cpu" ;; esac
  cat >"$INSTALL_DIR/.env" <<EOF
# Written by deploy.sh on $(date '+%Y-%m-%d %H:%M'). Re-run ./deploy.sh (see --help) to change these.
PORT=$PORT
PUBLIC_URL=$PUBLIC_URL
GENERATOR_URLS=$GENERATOR_URLS
GENERATOR_PORT=$GENERATOR_PORT
GENERATOR_BIND=$GENERATOR_BIND
GENERATOR_GPU=$GPU_ID
GENERATOR_ONLY=$GENERATOR_ONLY
DATA_PATH=$LIBRARY_DIR
APP_UID=$(id -u)
APP_GID=$(id -g)
DM_PASSWORD=$DM_PASSWORD
COMPOSE_PROFILES=$profiles
EOF
}

record_rule() {  # remember a firewall rule this script added, so uninstall.sh can remove it
  grep -qxF "$1" "$INSTALL_DIR/.deploy-state" 2>/dev/null || echo "$1" >>"$INSTALL_DIR/.deploy-state"
}

configure_firewall() {
  step "Checking the firewall"
  local fw=""
  if command -v ufw >/dev/null 2>&1 && systemctl is-active --quiet ufw 2>/dev/null; then
    fw="ufw"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    fw="firewalld"
  fi
  if [ -z "$fw" ]; then
    ok "no active firewall found: other devices can connect"
    return
  fi
  local ports=("$PORT/tcp") mdns=1 p
  local need="port $PORT/tcp (web interface and stream) and 5353/udp (finding speakers)"
  if [ "$GENERATOR_ONLY" = 1 ]; then
    ports=("$GENERATOR_PORT/tcp"); mdns=0
    need="port $GENERATOR_PORT/tcp (the music generator; it has no password)"
  fi
  if [ "$FIREWALL" = "no" ]; then
    warn "$fw is active and --no-firewall was given. Other computers need $need open."
    return
  fi
  if ! confirm "$fw is active. Open $need for your network? (uses sudo)"; then
    warn "firewall unchanged: other devices may not reach this computer"
    return
  fi
  case "$fw" in
    ufw)
      for p in "${ports[@]}"; do
        if ! sudo ufw status | grep -qE "^${p}[[:space:]]"; then
          sudo ufw allow "$p" comment "$APP_NAME" >/dev/null && record_rule "ufw $p"
        fi
      done
      if [ "$mdns" = 1 ] && ! sudo ufw status | grep -qE "^5353/udp[[:space:]]"; then
        sudo ufw allow 5353/udp comment "$APP_NAME speaker discovery" >/dev/null && record_rule "ufw 5353/udp"
      fi
      ;;
    firewalld)
      for p in "${ports[@]}"; do
        if ! sudo firewall-cmd --quiet --permanent --query-port="$p"; then
          sudo firewall-cmd --quiet --permanent --add-port="$p" && record_rule "firewalld port $p"
        fi
      done
      if [ "$mdns" = 1 ] && ! sudo firewall-cmd --quiet --permanent --query-service=mdns; then
        sudo firewall-cmd --quiet --permanent --add-service=mdns && record_rule "firewalld service mdns"
      fi
      sudo firewall-cmd --quiet --reload
      ;;
  esac
  ok "$fw: ${ports[*]}$([ "$mdns" = 1 ] && echo ' and speaker discovery (mDNS)') allowed"
}

generator_service() {
  case "$GEN_MODE" in test) echo fake-generator ;; gpu) echo generator ;; cpu) echo generator-cpu ;; esac
}

generator_status() {  # generator_status SERVICE: one line about the real generator's state
  local h="" st="" i
  for i in $(seq 1 30); do
    h="$(curl -sf "http://localhost:$GENERATOR_PORT/health" || true)"
    [ -n "$h" ] && break
    sleep 2
  done
  if [ -z "$h" ]; then
    warn "the music generator doesn't answer on port $GENERATOR_PORT yet (logs: ${COMPOSE[*]} logs -f $1)"
    return
  fi
  st="$(sed -n 's/.*"status": *"\([^"]*\)".*/\1/p' <<<"$h")"
  case "$st" in
    ok) ok "music generator ready" ;;
    downloading*) ok "music generator: $st. The first start downloads its models (12.7 GB with a GPU, 3.5 GB without); the starter music plays meanwhile." ;;
    *) ok "music generator: ${st:-starting} (logs: ${COMPOSE[*]} logs -f $1)" ;;
  esac
}

start_app() {
  step "Building and starting the containers (the first build takes a few minutes; the generator image is about 12 GB)"
  cd "$INSTALL_DIR"
  local gen wanted=() unwanted=() s
  gen="$(generator_service)"
  [ "$GENERATOR_ONLY" = 1 ] || wanted+=(conductor)
  [ -n "$gen" ] && wanted+=("$gen")
  for s in conductor fake-generator generator generator-cpu; do
    case " ${wanted[*]} " in *" $s "*) ;; *) unwanted+=("$s") ;; esac
  done
  if [ "${#unwanted[@]}" -gt 0 ]; then
    "${COMPOSE[@]}" "${ALL_PROFILES[@]}" rm -sf "${unwanted[@]}" >/dev/null 2>&1 || true
  fi
  "${COMPOSE[@]}" up -d --build --remove-orphans "${wanted[@]}"
  if [ "$GENERATOR_ONLY" != 1 ]; then
    local base="http://localhost:$PORT" i
    for i in $(seq 1 90); do
      curl -sf "$base/api/players/state" >/dev/null && break
      [ "$i" = 90 ] && {
        "${COMPOSE[@]}" logs --tail 40 >&2 || true
        die "the app didn't answer on $base within 90 s (logs above)"
      }
      sleep 1
    done
    ok "web interface answers on $base"
    if [ "${#IPS[@]}" -gt 0 ] && ! curl -sf "http://${IPS[0]}:$PORT/api/players/state" >/dev/null; then
      warn "the app doesn't answer on http://${IPS[0]}:$PORT; Docker host networking may not be working here"
    fi
  fi
  case "$gen" in generator | generator-cpu) generator_status "$gen" ;; esac
}

find_speakers() {
  local names="" i jar pwf; jar="$(mktemp)"; pwf="$(dm_password_file)"
  if [ "$DM_PASSWORD" != "off" ] && [ -s "$pwf" ]; then   # the speaker list is behind the DM password
    curl -sf -c "$jar" -X POST -H 'Content-Type: application/json' \
      -d "{\"password\":\"$(cat "$pwf")\"}" "http://localhost:$PORT/api/login" >/dev/null || true
  fi
  for i in 1 2 3 4 5 6; do
    names="$(curl -sf -b "$jar" "http://localhost:$PORT/api/cast/devices" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 || true)"
    [ -n "$names" ] && break
    sleep 2
  done
  rm -f "$jar"
  if [ -n "$names" ]; then
    ok "speakers found: $(echo "$names" | paste -sd ',' - | sed 's/,/, /g')"
  else
    warn "no Chromecast or Google speakers found yet (they must be switched on and on the same network; they can take a minute to show up)"
  fi
}

print_summary() {
  local url_lan="" ip
  if [ "$GENERATOR_ONLY" = 1 ]; then
    printf '\n%sThe music generator is running.%s\n\n' "$BOLD$GREEN" "$RESET"
    echo "  On the computer that runs the app, connect it with:"
    for ip in "${IPS[@]}"; do printf '      %s./deploy.sh --generator http://%s:%s%s\n' "$CYAN" "$ip" "$GENERATOR_PORT" "$RESET"; done
    echo
    echo "  It has no password: anyone on your network can use it. Keep it on a trusted network."
    cat <<EOF

  Installed: $INSTALL_DIR
  Manage:    cd $INSTALL_DIR
             ${COMPOSE[*]} logs -f $(generator_service)   (watch the logs)
             ./deploy.sh                  (update and restart)
             ./deploy.sh --with-app       (run the app here too)
             ./uninstall.sh --keep-models (remove, keep the downloaded models)
EOF
    return
  fi
  [ "${#IPS[@]}" -gt 0 ] && url_lan="http://${IPS[0]}:$PORT"
  printf '\n%s%s is running.%s\n\n' "$BOLD$GREEN" "$APP_NAME" "$RESET"
  printf '  %sOn this computer:%s\n      http://localhost:%s\n\n' "$BOLD" "$RESET" "$PORT"
  if [ -n "$url_lan" ]; then
    printf '  %sOn your phone, tablet or laptop%s (connected to the same network or Wi-Fi):\n' "$BOLD" "$RESET"
    for ip in "${IPS[@]}"; do printf '      %shttp://%s:%s%s\n' "$CYAN" "$ip" "$PORT" "$RESET"; done
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet avahi-daemon 2>/dev/null; then
      printf '      http://%s.local:%s   (works on most phones and Macs)\n' "$(hostname)" "$PORT"
    fi
    echo
    if command -v qrencode >/dev/null 2>&1; then
      echo "  Or scan this with your phone's camera:"
      qrencode -t ANSIUTF8 -m 2 "$url_lan" | sed 's/^/    /'
    else
      echo "  Tip: install 'qrencode' and run deploy.sh again to get a QR code to scan with your phone."
    fi
    echo
  fi
  if [ "$DM_PASSWORD" = "off" ]; then
    printf '  %sDM password:%s off (anyone with the link can open the DM page; turn it on with ./deploy.sh --password)\n\n' "$BOLD" "$RESET"
  else
    print_password
  fi
  cat <<EOF
  ${BOLD}Using it${RESET}
    - On a phone, open the link and choose "Add to Home Screen" for an app-like icon.
    - Settings (⚙): tick a Google/Chromecast speaker or speaker group, then press Cast.
    - Scenes: add scenes to the queue, then press Next scene. The fade slider sets how gently music changes.
    - Any media player (e.g. VLC) can also play ${url_lan:-http://localhost:$PORT}/stream.mp3

EOF
  case "$GEN_MODE" in
    test)
      echo "  ${YELLOW}Music generator: the built-in TEST generator plays test tones, not real music.${RESET}"
      echo "  For real music: ./deploy.sh --gpu 0 (NVIDIA GPU) or ./deploy.sh --cpu-generator" ;;
    gpu) echo "  Music generator: Stable Audio 3 on GPU $GPU_ID. Pick the quality in Settings (⚙) → Music quality." ;;
    cpu) echo "  Music generator: Stable Audio 3 Small on the CPU (\"Primary school guitar lesson\" quality)." ;;
    remote) echo "  Music generator: $GENERATOR_URLS" ;;
  esac
  echo "  Powered by Stability AI."
  cat <<EOF

  Library:   $LIBRARY_DIR   (your music: back this folder up)
  Installed: $INSTALL_DIR
  Manage:    cd $INSTALL_DIR
             ${COMPOSE[*]} logs -f        (watch the logs)
             ./deploy.sh                  (update and restart)
             ./uninstall.sh --preserve-library --keep-models   (remove, keep your music and models)
EOF
}

main() {
  parse_args "$@"
  if [ "$SHOW_PASSWORD" = 1 ]; then
    resolve_install_dir
    [ "$(env_get DM_PASSWORD)" = "off" ] && die "the DM password is off (turn it on with ./deploy.sh --password)"
    f="$(dm_password_file)"
    [ -s "$f" ] || die "no DM password yet: start the app first (./deploy.sh)"
    cat "$f"; exit 0
  fi
  check_system
  resolve_install_dir
  fetch_code
  choose_settings
  check_disk
  write_env
  configure_firewall
  start_app
  [ "$GENERATOR_ONLY" = 1 ] || find_speakers
  print_summary
}

main "$@"; exit $?
