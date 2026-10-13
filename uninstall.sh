#!/usr/bin/env bash
# uninstall.sh: stop dnd-music-creator and remove it: containers, images, the code,
# and any firewall rules deploy.sh added. The music library is deleted too, unless
# you pass --preserve-library. Runpod resources (endpoint, template, volume) live in
# your Runpod account: interactively it offers to delete them first (scripts/runpod_setup.py
# --teardown); with -y it only tells you their ids. Run ./uninstall.sh --help for the options.
set -euo pipefail

APP_NAME="dnd-music-creator"
INSTALL_DIR=""
PRESERVE=0
PRESERVE_DEST=""
ASSUME_YES=0
KEEP_MODELS=0
DOCKER=()
COMPOSE=()

if [ -t 1 ]; then
  BOLD=$'\e[1m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; RESET=$'\e[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die()  { printf '\n%sError:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Stop ${APP_NAME} and remove it: containers, images, code and the firewall
rules deploy.sh added.

Usage: ./uninstall.sh [options]

  --preserve-library [DIR]  keep your generated music. The library is moved to DIR
                            (default: ~/${APP_NAME}-library-<date>); a library that
                            already lives outside the install folder stays where it is.
                            Reinstall later with: ./deploy.sh --library DIR
  --keep-models             keep the downloaded music models (a Docker volume of up to
                            12.7 GB), so reinstalling into a folder with the same name
                            doesn't download them again
  --dir DIR                 the installation to remove (default: this checkout,
                            otherwise ~/${APP_NAME})
  -y, --yes                 don't ask for confirmation
  -h, --help                show this help

Runpod endpoint, template and network volume (if runpod_setup.py made them) stay in
your Runpod account and the volume keeps billing: a normal run offers to delete them
first (needs a temporary All-access key); -y never does, it lists them at the end.

Without --preserve-library the music library is deleted; without --keep-models the models too.
EOF
}

ANSWER=""
ask() {  # ask "Question?" EXPECTED: returns 0 when the user types EXPECTED (the answer is left in $ANSWER)
  [ "$ASSUME_YES" = 1 ] && return 0
  ANSWER=""
  printf '  %s ' "$1" >&2   # read -p would be silenced by the redirect below, which only hides "no tty" errors
  { read -r ANSWER </dev/tty; } 2>/dev/null || true
  [ "$ANSWER" = "$2" ]
}

mismatch() {  # mismatch EXPECTED: what was typed and what was needed
  printf "You typed '%s'; expected '%s'. " "$ANSWER" "$1"
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --preserve-library)
        PRESERVE=1
        if [ $# -gt 1 ] && [ "${2#-}" = "$2" ]; then PRESERVE_DEST="$2"; shift; fi
        shift ;;
      --keep-models) KEEP_MODELS=1; shift ;;
      --dir) INSTALL_DIR="${2:?--dir needs a directory}"; shift 2 ;;
      -y | --yes) ASSUME_YES=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done
}

resolve_install_dir() {
  if [ -z "$INSTALL_DIR" ]; then
    local here
    here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
    if [ -n "$here" ] && [ -f "$here/docker-compose.yml" ] && [ -f "$here/deploy.sh" ]; then
      INSTALL_DIR="$here"
    else
      INSTALL_DIR="$HOME/$APP_NAME"
    fi
  fi
  INSTALL_DIR="$(realpath -m "$INSTALL_DIR")"
  [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -f "$INSTALL_DIR/deploy.sh" ] ||
    die "no $APP_NAME installation found in $INSTALL_DIR (use --dir)"
  case "$INSTALL_DIR" in
    / | /home | /root | /usr | /opt | /var | "$HOME") die "refusing to remove $INSTALL_DIR" ;;
  esac
}

env_get() {
  [ -f "$INSTALL_DIR/.env" ] || return 0
  sed -n "s/^$1=//p" "$INSTALL_DIR/.env" | tail -n 1
}

find_docker() {
  command -v docker >/dev/null 2>&1 || { warn "Docker isn't installed: skipping containers and images"; return; }
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
  else
    warn "Docker isn't running: skipping containers and images"
    return
  fi
  if "${DOCKER[@]}" compose version >/dev/null 2>&1; then
    COMPOSE=("${DOCKER[@]}" compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
    [ "${DOCKER[0]}" = "sudo" ] && COMPOSE=(sudo docker-compose)
  else
    warn "Docker Compose isn't available: skipping containers and images"
  fi
}

runpod_ids() {  # runpod_ids FILE: prints "endpoint template volume" ("-" for unknown); nothing without an endpoint id
  [ -f "$1" ] || return 0
  python3 -I -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    sys.exit(0)
c = d.get("created") or {}
e = c.get("endpoint_id") or d.get("endpoint_id")
if e:
    print(e, c.get("template_id") or "-", c.get("volume_id") or "-")
' "$1" 2>/dev/null || true
}

runpod_leftover_text() {  # ids in "E T V" form -> "endpoint E, template T, volume V" (unknown ones left out)
  local e t v out
  read -r e t v <<<"$1"
  out="endpoint $e"
  [ "$t" != "-" ] && out="$out, template $t"
  [ "$v" != "-" ] && out="$out, volume $v"
  echo "$out"
}

is_library() { [ -f "$1/dnd-music.db" ] || [ -d "$1/tracks" ]; }

main() {
  parse_args "$@"
  resolve_install_dir
  find_docker

  local project="" volume=""
  if [ "${#COMPOSE[@]}" -gt 0 ]; then
    project="$(cd "$INSTALL_DIR" && "${COMPOSE[@]}" config 2>/dev/null | sed -n 's/^name: //p' | head -n 1 || true)"
    if [ -n "$project" ] && "${DOCKER[@]}" volume inspect "${project}_models" >/dev/null 2>&1; then
      volume="${project}_models"
    fi
  fi

  local library inside=0 tracks=0 size="0" rules=""
  library="$(env_get DATA_PATH)"
  library="$(realpath -m "${library:-$INSTALL_DIR/data}")"
  case "$library/" in "$INSTALL_DIR"/*) inside=1 ;; esac
  if [ -d "$library" ]; then
    tracks="$(find "$library" -name '*.opus' 2>/dev/null | wc -l || true)"; tracks="${tracks:-0}"
    size="$(du -sh "$library" 2>/dev/null | cut -f1 || true)"; size="${size:-?}"
  fi
  [ -f "$INSTALL_DIR/.deploy-state" ] && rules="$(cat "$INSTALL_DIR/.deploy-state")"

  if [ "$PRESERVE" = 1 ] && [ "$inside" = 1 ]; then
    PRESERVE_DEST="$(realpath -m "${PRESERVE_DEST:-$HOME/$APP_NAME-library-$(date +%Y%m%d-%H%M)}")"
    [ -e "$PRESERVE_DEST" ] && die "$PRESERVE_DEST already exists; choose another: --preserve-library DIR"
    case "$PRESERVE_DEST/" in "$INSTALL_DIR"/*) die "the library must be kept outside $INSTALL_DIR" ;; esac
  fi

  step "This will remove $APP_NAME"
  echo "  - its containers and images"
  [ -n "$rules" ] && echo "  - firewall rules deploy.sh added: $(echo "$rules" | paste -sd ',' - | sed 's/,/, /g')"
  echo "  - the installation folder $INSTALL_DIR"
  if [ -n "$volume" ]; then
    if [ "$KEEP_MODELS" = 1 ]; then
      printf '  %sKeeps%s the downloaded music models (Docker volume %s)\n' "$GREEN" "$RESET" "$volume"
    else
      echo "  - the downloaded music models (Docker volume $volume, up to 12.7 GB; keep them with --keep-models)"
    fi
  fi
  if [ "$PRESERVE" = 1 ]; then
    if [ "$inside" = 1 ]; then
      printf '  %sKeeps%s your music library (%s tracks, %s), moved to %s\n' "$GREEN" "$RESET" "$tracks" "$size" "$PRESERVE_DEST"
    else
      printf '  %sKeeps%s your music library (%s tracks, %s) where it is: %s\n' "$GREEN" "$RESET" "$tracks" "$size" "$library"
    fi
  elif is_library "$library"; then
    printf '  %sDeletes%s your music library: %s tracks, %s in %s\n' "$RED" "$RESET" "$tracks" "$size" "$library"
    echo "    (to keep it, cancel and run: ./uninstall.sh --preserve-library)"
  fi
  local rp_ids="" rp_text="" rp_left=""
  rp_ids="$(runpod_ids "$library/runpod.json")"
  if [ -n "$rp_ids" ]; then
    rp_text="$(runpod_leftover_text "$rp_ids")"
    printf '  %sRunpod:%s %s stay in your Runpod account; the network volume bills about $1.05/month (estimate)\n' "$YELLOW" "$RESET" "$rp_text"
  fi
  echo
  if [ "$PRESERVE" = 0 ] && [ "$tracks" -gt 0 ]; then
    ask "Type 'delete' to remove everything including $tracks tracks:" delete || die "$(mismatch delete)Nothing was changed."
  else
    ask "Type 'yes' to continue:" yes || die "$(mismatch yes)Nothing was changed."
  fi

  rp_left="$rp_text"
  if [ -n "$rp_ids" ] && [ "$ASSUME_YES" = 0 ]; then
    if ! ask "Delete these from Runpod first? Needs a temporary All-access key. Type 'yes':" yes; then
      warn "$(mismatch yes)Skipping the Runpod teardown; the uninstall continues."
    else
      if [ "${#COMPOSE[@]}" -eq 0 ]; then
        warn "Docker isn't available here, so the teardown can't run: delete them in the Runpod console"
      else
        step "Deleting the Runpod resources"
        local run=(run --rm -v "$INSTALL_DIR/scripts:/scripts:ro") rc=0
        [ -t 0 ] || run+=(-T)
        (cd "$INSTALL_DIR" && "${COMPOSE[@]}" "${run[@]}" conductor python /scripts/runpod_setup.py --data-dir /data --teardown) || rc=$?
        if [ "$rc" = 0 ] && [ -z "$(runpod_ids "$library/runpod.json")" ]; then
          ok "Runpod resources deleted"
          rp_left=""
        else
          warn "the Runpod teardown did not finish; the uninstall continues"
        fi
      fi
    fi
  fi

  if [ "${#COMPOSE[@]}" -gt 0 ]; then
    step "Stopping and removing containers and images"
    cd "$INSTALL_DIR"
    if [ -d "$library" ] && [ -n "$(find "$library" ! -user "$(id -u)" -print -quit 2>/dev/null)" ]; then
      # An older install ran as root: hand the files back before moving or deleting them.
      "${COMPOSE[@]}" run --rm --no-deps --user 0 --entrypoint chown conductor -R "$(id -u):$(id -g)" /data >/dev/null 2>&1 ||
        warn "couldn't take ownership of the library files; you may need sudo to remove them"
    fi
    local down=(down --rmi local --remove-orphans)
    [ "$KEEP_MODELS" = 1 ] || down+=(--volumes)
    "${COMPOSE[@]}" --profile fake --profile gpu --profile cpu "${down[@]}"
    ok "containers and images removed"
  fi

  if [ -n "$rules" ]; then
    step "Removing firewall rules"
    local rule
    while read -r rule; do
      case "$rule" in
        "ufw "*) sudo ufw delete allow "${rule#ufw }" >/dev/null && ok "ufw: closed ${rule#ufw }" ;;
        "firewalld port "*) sudo firewall-cmd --quiet --permanent --remove-port="${rule#firewalld port }" && ok "firewalld: closed ${rule#firewalld port }" ;;
        "firewalld service "*) sudo firewall-cmd --quiet --permanent --remove-service="${rule#firewalld service }" && ok "firewalld: removed ${rule#firewalld service }" ;;
      esac
    done <<<"$rules"
    if echo "$rules" | grep -q '^firewalld'; then sudo firewall-cmd --quiet --reload; fi
  fi

  step "Removing files"
  if [ "$PRESERVE" = 1 ]; then
    if [ "$inside" = 1 ] && [ -d "$library" ]; then
      mkdir -p "$(dirname "$PRESERVE_DEST")"
      mv "$library" "$PRESERVE_DEST"
      ok "music library kept in $PRESERVE_DEST"
    else
      ok "music library left in $library"
    fi
  elif [ "$inside" = 0 ] && [ -d "$library" ]; then
    if is_library "$library"; then
      rm -rf "$library"
      ok "music library deleted"
    else
      warn "$library doesn't look like a music library; left it alone"
    fi
  fi
  cd /
  rm -rf "$INSTALL_DIR"
  ok "removed $INSTALL_DIR"

  printf '\n%s%s has been uninstalled.%s\n' "$BOLD$GREEN" "$APP_NAME" "$RESET"
  if [ "$PRESERVE" = 1 ]; then
    local kept="$library" kept_tracks=0
    [ "$inside" = 1 ] && kept="$PRESERVE_DEST"
    kept_tracks="$(find "$kept" -name '*.opus' 2>/dev/null | wc -l || true)"; kept_tracks="${kept_tracks:-0}"
    printf '\n%sYour music library is SAFE:%s %s tracks in\n    %s\n' "$BOLD$GREEN" "$RESET" "$kept_tracks" "$kept"
    echo "(The folder $INSTALL_DIR is gone; if your shell was in it, it now looks empty.)"
    echo "To reinstall with your music: git clone the repository, then ./deploy.sh --library $kept"
  fi
  if [ -n "$rp_left" ]; then
    printf '%sRunpod resources were NOT deleted:%s %s. Delete them in the Runpod console (Serverless; Storage) or they keep costing money.\n' "$YELLOW" "$RESET" "$rp_left"
    [ "$PRESERVE" = 1 ] && echo "The ids are in runpod.json in the kept library."
  fi
  if [ "$KEEP_MODELS" = 1 ] && [ -n "$volume" ]; then
    echo "The models stay in Docker volume $volume: a reinstall reuses them as long as the Compose project is still named $project."
    echo "Remove them later with: docker volume rm $volume"
  fi
}

main "$@"; exit $?
