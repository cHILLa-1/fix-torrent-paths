#!/usr/bin/env bash
#
# run.sh
#
# Installiert die Abhaengigkeiten fuer fix_torrent_paths.py und fuehrt das
# Script interaktiv aus (Dry-Run oder scharfer Fix, optional auf einzelne
# Kategorien beschraenkt).
#
# Nutzung:
#   chmod +x run.sh
#   ./run.sh
#
# Zugangsdaten koennen vorab per Umgebungsvariablen gesetzt werden, dann
# wird nicht danach gefragt:
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
# Hilfsfunktionen
# --------------------------------------------------------------------------

info()  { printf '\033[1;34m%s\033[0m\n' "$*"; }
warn()  { printf '\033[1;33m%s\033[0m\n' "$*"; }
err()   { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

ask() {
    # ask "Prompt-Text" "Default"  -> gibt Eingabe (oder Default) auf stdout aus
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
# .env Datei laden (falls vorhanden)
# --------------------------------------------------------------------------
# Erwartete Variablen darin, z.B.:
#   QBIT_HOST=192.168.178.22:8082
#   QBIT_USER=admin
#   QBIT_PASS=geheim
#   QBIT_CONTAINER_ROOT=/data/torrents
#   QBIT_LOCAL_ROOT=/mnt/user/share_media/torrents
#
# Suchreihenfolge:
#   1. Als erstes Argument uebergebener Pfad: ./run.sh /pfad/zu/meiner.env
#   2. "$SCRIPT_DIR/.env"
#   3. Falls (2) nicht existiert: irgendeine *.env Datei im Script-Ordner
#      (z.B. fix.env, qbit.env, ...). Bei mehreren Treffern wird nachgefragt.
ENV_FILE=""

if [ -n "${1:-}" ]; then
    ENV_FILE="$1"
    if [ ! -f "$ENV_FILE" ]; then
        err "Angegebene env-Datei nicht gefunden: $ENV_FILE"
        exit 1
    fi
elif [ -f "$SCRIPT_DIR/.env" ]; then
    ENV_FILE="$SCRIPT_DIR/.env"
else
    # Nach beliebigen *.env Dateien im Script-Ordner suchen (z.B. fix.env)
    mapfile -t candidates < <(find "$SCRIPT_DIR" -maxdepth 1 -type f -name "*.env" | sort)
    if [ "${#candidates[@]}" -eq 1 ]; then
        ENV_FILE="${candidates[0]}"
        info "Gefunden: $(basename "$ENV_FILE") (wird automatisch verwendet)"
    elif [ "${#candidates[@]}" -gt 1 ]; then
        echo ""
        info "Mehrere .env-Dateien gefunden:"
        for i in "${!candidates[@]}"; do
            echo "  $((i+1))) $(basename "${candidates[$i]}")"
        done
        sel="$(ask "Welche verwenden? (Nummer)" "1")"
        idx=$((sel-1))
        if [ "$idx" -ge 0 ] && [ "$idx" -lt "${#candidates[@]}" ]; then
            ENV_FILE="${candidates[$idx]}"
        else
            err "Ungueltige Auswahl."
            exit 1
        fi
    fi
fi

if [ -n "$ENV_FILE" ]; then
    info "Lese Zugangsdaten aus: $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    warn "Keine .env-Datei im Ordner $SCRIPT_DIR gefunden - frage interaktiv ab."
fi

# --------------------------------------------------------------------------
# 1) Voraussetzungen pruefen / installieren
# --------------------------------------------------------------------------

if [ ! -f "$PY_SCRIPT" ]; then
    err "fix_torrent_paths.py wurde nicht neben run.sh gefunden ($PY_SCRIPT)."
    err "Bitte beide Dateien in denselben Ordner legen."
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 wurde nicht gefunden. Bitte Python 3 installieren, dann erneut ausfuehren."
    exit 1
fi

info "Pruefe/installiere Python-Abhaengigkeiten ..."
if python3 -c "import requests" >/dev/null 2>&1; then
    info "  'requests' ist bereits installiert."
else
    if python3 -m pip install --user requests >/dev/null 2>&1; then
        info "  'requests' installiert (--user)."
    elif python3 -m pip install --user --break-system-packages requests >/dev/null 2>&1; then
        info "  'requests' installiert (--user --break-system-packages)."
    else
        err "Konnte 'requests' nicht automatisch installieren."
        err "Bitte manuell ausfuehren: pip install requests --break-system-packages"
        exit 1
    fi
fi

# --------------------------------------------------------------------------
# 2) Zugangsdaten / Pfad-Mapping ermitteln (Env-Variablen haben Vorrang)
# --------------------------------------------------------------------------

echo ""
info "=== qBittorrent Zugangsdaten ==="

QBIT_HOST="${QBIT_HOST:-}"
if [ -z "$QBIT_HOST" ]; then
    QBIT_HOST="$(ask "qBittorrent Host:Port (z.B. 192.168.178.22:8082)")"
fi

QBIT_USER="${QBIT_USER:-}"
if [ -z "$QBIT_USER" ]; then
    QBIT_USER="$(ask "Benutzername" "admin")"
fi

QBIT_PASS="${QBIT_PASS:-}"
if [ -z "$QBIT_PASS" ]; then
    QBIT_PASS="$(ask_secret "Passwort (Eingabe unsichtbar)")"
fi

echo ""
info "=== qBittorrent-Setup: lokal oder Docker? ==="

QBIT_CONTAINER_ROOT="${QBIT_CONTAINER_ROOT:-}"
QBIT_LOCAL_ROOT="${QBIT_LOCAL_ROOT:-}"

if [ -n "$QBIT_CONTAINER_ROOT" ] && [ -n "$QBIT_LOCAL_ROOT" ]; then
    info "Container-/Lokal-Root bereits per Umgebungsvariable/.env gesetzt - dieses Pfad-Mapping wird verwendet."
else
    warn "Laeuft qBittorrent in Docker und dieses Script NICHT im selben Mount-"
    warn "Namespace, liefert die API Container-Pfade (z.B. /data/torrents), die"
    warn "auf den fuer dieses Script lokal erreichbaren Pfad umgerechnet werden muessen."
    warn "Laeuft qBittorrent dagegen lokal auf demselben System/im selben Mount-"
    warn "Namespace wie dieses Script, ist kein Mapping noetig."

    setup_mode="$(ask "Laeuft qBittorrent in Docker (mit anderen Mounts als dieses Script) oder lokal/im selben Mount-Namespace? (docker/lokal)" "lokal")"
    case "$(echo "$setup_mode" | tr '[:upper:]' '[:lower:]')" in
        d|docker)
            QBIT_CONTAINER_ROOT="$(ask "Container-Pfad (z.B. /data/torrents)" "")"
            QBIT_LOCAL_ROOT="$(ask "Lokaler Pfad (z.B. /mnt/user/share_media/torrents)" "")"
            ;;
        *)
            info "Lokaler Modus: es wird angenommen, dass die von der API gemeldeten Pfade 1:1 lokal fuer dieses Script erreichbar sind. Kein Pfad-Mapping noetig."
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
# 3) Menue
# --------------------------------------------------------------------------

echo ""
info "=== Was moechtest du tun? ==="
echo "  1) Dry-Run - alle Kategorien (nichts wird veraendert)"
echo "  2) Dry-Run - nur bestimmte Kategorie(n)"
echo "  3) Fix ausfuehren - alle Kategorien (verschiebt & rechecked wirklich!)"
echo "  4) Fix ausfuehren - nur bestimmte Kategorie(n)"
echo "  5) Abbrechen"
echo ""

choice="$(ask "Auswahl (1-5)" "1")"

CATEGORY_ARGS=()
case "$choice" in
    2|4)
        cats="$(ask "Kategorie(n), kommagetrennt (z.B. short-seed,sonarr-cartoon)")"
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
        warn "ACHTUNG: Es werden jetzt wirklich Dateien verschoben und Torrents gerecheckt!"
        confirm="$(ask "Wirklich fortfahren? (ja/nein)" "nein")"
        if [ "$confirm" != "ja" ]; then
            info "Abgebrochen."
            exit 0
        fi
        ;;
    5)
        info "Abgebrochen."
        exit 0
        ;;
    *)
        err "Ungueltige Auswahl."
        exit 1
        ;;
esac

echo ""
info "Starte: python3 fix_torrent_paths.py ${RUN_ARGS[*]}"
echo ""

python3 "$PY_SCRIPT" "${RUN_ARGS[@]}"
