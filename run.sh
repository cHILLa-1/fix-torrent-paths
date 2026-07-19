#!/usr/bin/env bash
#
# run.sh
#
# Installs the dependencies for fix_torrent_paths.py and runs the script
# interactively (dry run or live fix, optionally restricted to specific
# categories).
#
# Usage:
#   chmod +x run.sh
#   ./run.sh
#
# Credentials can be set beforehand via environment variables, in which
# case you won't be asked for them:
#   export QBIT_HOST=192.168.178.22:8082
#   export QBIT_USER=admin
#   export QBIT_PASS='xxx'
#   export QBIT_CONTAINER_ROOT=/data/torrents
#   export QBIT_LOCAL_ROOT=/mnt/user/share_media/torrents
#   export QBIT_ROOT_MAP='/data/cross-seed=/mnt/user/share_media/cross-seed'
#   export QBIT_SKIP_SOURCE_PREFIXES='/data/cross-seed'
#   export QBIT_EXTRA_SCAN_DIRS='/mnt/user/share_media/torrents/orphaned_data'
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/fix_torrent_paths.py"

# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------

info()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m%s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

ask() {
    # ask "Prompt text" "Default"  -> prints the input (or default) to stdout
    local prompt="$1"
    local default="${2:-}"
    local reply
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default]: " reply || true
        echo "${reply:-$default}"
    else
        read -r -p "$prompt: " reply || true
        echo "$reply"
    fi
}

ask_secret() {
    local prompt="$1"
    local reply
    read -r -s -p "$prompt: " reply || true
    echo "" >&2
    echo "$reply"
}

# --------------------------------------------------------------------------
# Load .env file (if present)
# --------------------------------------------------------------------------
# Expected variables in it, e.g.:
#   QBIT_HOST=192.168.178.22:8082
#   QBIT_USER=admin
#   QBIT_PASS=secret
#   QBIT_CONTAINER_ROOT=/data/torrents
#   QBIT_LOCAL_ROOT=/mnt/user/share_media/torrents
#
# Search order:
#   1. Path passed as the first argument: ./run.sh /path/to/my.env
#   2. "$SCRIPT_DIR/.env"
#   3. If (2) doesn't exist: any *.env file in the script folder
#      (e.g. fix.env, qbit.env, ...). If there are multiple matches, you
#      are asked which one to use.
ENV_FILE=""

if [ -n "${1:-}" ]; then
    ENV_FILE="$1"
    if [ ! -f "$ENV_FILE" ]; then
        err "Specified env file not found: $ENV_FILE"
        exit 1
    fi
elif [ -f "$SCRIPT_DIR/.env" ]; then
    ENV_FILE="$SCRIPT_DIR/.env"
else
    # Search for any *.env files in the script folder (e.g. fix.env)
    mapfile -t candidates < <(find "$SCRIPT_DIR" -maxdepth 1 -type f -name "*.env" | sort)
    if [ "${#candidates[@]}" -eq 1 ]; then
        ENV_FILE="${candidates[0]}"
        info "Found: $(basename "$ENV_FILE") (will be used automatically)"
    elif [ "${#candidates[@]}" -gt 1 ]; then
        echo ""
        info "Multiple .env files found:"
        for i in "${!candidates[@]}"; do
            echo "  $((i+1))) $(basename "${candidates[$i]}")"
        done
        sel="$(ask "Which one to use? (number)" "1")"
        idx=$((sel-1))
        if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#candidates[@]}" ]; then
            ENV_FILE="${candidates[$idx]}"
        else
            err "Invalid selection."
            exit 1
        fi
    fi
fi

if [ -n "$ENV_FILE" ]; then
    info "Reading credentials from: $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    warn "No .env file found in folder $SCRIPT_DIR - asking interactively."
fi

# --------------------------------------------------------------------------
# 1) Check / install prerequisites
# --------------------------------------------------------------------------

if [ ! -f "$PY_SCRIPT" ]; then
    err "fix_torrent_paths.py was not found next to run.sh ($PY_SCRIPT)."
    err "Please put both files in the same folder."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 was not found. Please install Python 3, then run again."
    exit 1
fi

info "Checking/installing Python dependencies ..."
if python3 -c "import requests" >/dev/null 2>&1; then
    info "  'requests' is already installed."
else
    if python3 -m pip install --user requests >/dev/null 2>&1; then
        info "  'requests' installed (--user)."
    elif python3 -m pip install --user --break-system-packages requests >/dev/null 2>&1; then
        info "  'requests' installed (--user --break-system-packages)."
    else
        err "Could not install 'requests' automatically."
        err "Please run manually: pip install requests --break-system-packages"
        exit 1
    fi
fi

# --------------------------------------------------------------------------
# 2) Determine credentials / path mapping (environment variables take precedence)
# --------------------------------------------------------------------------

echo ""
info "=== qBittorrent credentials ==="

QBIT_HOST="${QBIT_HOST:-}"
if [ -z "$QBIT_HOST" ]; then
    QBIT_HOST="$(ask "qBittorrent host:port (e.g. 192.168.178.22:8082)")"
fi

QBIT_USER="${QBIT_USER:-}"
if [ -z "$QBIT_USER" ]; then
    QBIT_USER="$(ask "Username" "admin")"
fi

QBIT_PASS="${QBIT_PASS:-}"
if [ -z "$QBIT_PASS" ]; then
    QBIT_PASS="$(ask_secret "Password (input hidden)")"
fi

echo ""
info "=== qBittorrent setup: local or Docker? ==="

QBIT_CONTAINER_ROOT="${QBIT_CONTAINER_ROOT:-}"
QBIT_LOCAL_ROOT="${QBIT_LOCAL_ROOT:-}"

if [ -n "$QBIT_CONTAINER_ROOT" ] && [ -n "$QBIT_LOCAL_ROOT" ]; then
    info "Container/local root already set via environment variable/.env - using that path mapping."
else
    warn "If qBittorrent runs in Docker and this script does NOT run in the same"
    warn "mount namespace, the API returns container paths (e.g. /data/torrents)"
    warn "that need to be translated into the path reachable locally for this script."
    warn "If qBittorrent instead runs locally on the same system/in the same mount"
    warn "namespace as this script, no mapping is needed."

    setup_mode="$(ask "Does qBittorrent run in Docker (with different mounts than this script), or locally/in the same mount namespace? (docker/local)" "local")"
    case "$(echo "$setup_mode" | tr '[:upper:]' '[:lower:]')" in
        d|docker)
            QBIT_CONTAINER_ROOT="$(ask "Container path (e.g. /data/torrents)" "")"
            QBIT_LOCAL_ROOT="$(ask "Local path (e.g. /mnt/user/share_media/torrents)" "")"
            ;;
        *)
            info "Local mode: assuming the paths reported by the API are reachable 1:1 locally for this script. No path mapping needed."
            QBIT_CONTAINER_ROOT=""
            QBIT_LOCAL_ROOT=""
            ;;
    esac
fi

export QBIT_HOST QBIT_USER QBIT_PASS QBIT_CONTAINER_ROOT QBIT_LOCAL_ROOT
export QBIT_ROOT_MAP="${QBIT_ROOT_MAP:-}"
export QBIT_SKIP_SOURCE_PREFIXES="${QBIT_SKIP_SOURCE_PREFIXES:-}"
export QBIT_EXTRA_SCAN_DIRS="${QBIT_EXTRA_SCAN_DIRS:-}"

PATH_ARGS=()
if [ -n "$QBIT_CONTAINER_ROOT" ] && [ -n "$QBIT_LOCAL_ROOT" ]; then
    PATH_ARGS=(--container-root "$QBIT_CONTAINER_ROOT" --local-root "$QBIT_LOCAL_ROOT")
fi

# --------------------------------------------------------------------------
# 3) Menu
# --------------------------------------------------------------------------

echo ""
info "=== What would you like to do? ==="
echo "  1) Dry run - all categories (nothing gets changed)"
echo "  2) Dry run - specific categorie(s) only"
echo "  3) Run fix - all categories (actually moves & rechecks!)"
echo "  4) Run fix - specific categorie(s) only"
echo "  5) Cancel"
echo ""

choice="$(ask "Choice (1-5)" "1")"

CATEGORY_ARGS=()
case "$choice" in
    2|4)
        cats="$(ask "Categorie(s), comma-separated (e.g. short-seed,sonarr-cartoon)")"
        if [ -n "$cats" ]; then
            CATEGORY_ARGS=(--categories "$cats")
        fi
        ;;
esac

RUN_ARGS=(-v "${PATH_ARGS[@]}" "${CATEGORY_ARGS[@]}")

case "$choice" in
    1|2)
        RUN_ARGS+=(--dry-run)
        ;;
    3|4)
        echo ""
        warn "WARNING: files will now actually be moved and torrents rechecked!"
        confirm="$(ask "Really continue? (yes/no)" "no")"
        if [ "$confirm" != "yes" ]; then
            info "Cancelled."
            exit 0
        fi
        ;;
    5)
        info "Cancelled."
        exit 0
        ;;
    *)
        err "Invalid choice."
        exit 1
        ;;
esac

echo ""
info "Starting: python3 fix_torrent_paths.py ${RUN_ARGS[*]}"
echo ""

python3 "$PY_SCRIPT" "${RUN_ARGS[@]}"
