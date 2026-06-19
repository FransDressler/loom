#!/usr/bin/env bash
# loom-start.sh — bring up every Loom inbox worker + every CONFIGURED & REACHABLE
# channel listener in one go. Idempotent / desired-state: re-run any time.
#
#   inbox workers (always on, --watch):  ingest · tasks · builder
#   channel listeners (--poll timers):   whatsapp · telegram · discord · imessage
#
# A channel listener is enabled only when its `--check` passes (credentials set AND
# the relay/API reachable); otherwise it is DISABLED, so placeholder/offline channels
# never run a failing poll every 2 minutes.
#
# Usage:  ~/loom/deploy/loom-start.sh

set -uo pipefail

LOOM_DIR="${LOOM_DIR:-$HOME/loom}"
ENV_FILE="${LOOM_ENV:-$HOME/.config/loom/env}"
UNIT_DIR="$HOME/.config/systemd/user"
DEPLOY="$LOOM_DIR/deploy"
VENV="$LOOM_DIR/.venv/bin"

# Load the env SAFELY into our environment — `export "KEY=VALUE"` never executes the
# value, so lines like LOOM_BB_CHAT_GUID=iMessage;-;+49… don't get split on ';'.
if [ -f "$ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) export "$line" 2>/dev/null || true ;; esac
  done < "$ENV_FILE"
fi

# Keep workers running after logout (best-effort; no sudo needed on most setups).
loginctl enable-linger "$USER" >/dev/null 2>&1 || true

# Install/refresh all unit files, then reload.
mkdir -p "$UNIT_DIR"
cp -f "$DEPLOY"/loom-*.service "$DEPLOY"/loom-*.timer "$UNIT_DIR"/ 2>/dev/null || true
systemctl --user daemon-reload

started=() ; skipped=()

enable_now() { systemctl --user enable --now "$1" >/dev/null 2>&1; }
disable_now() { systemctl --user disable --now "$1" >/dev/null 2>&1; systemctl --user reset-failed "${1%.timer}.service" >/dev/null 2>&1 || true; }

# --- inbox workers (--watch services) — always on -----------------------------
for w in \
  "loom-ingest.service|ingest worker  (dump folder ~/loom-dump → raw→Wiki)" \
  "loom-tasks.service|tasks worker   (skill queue: deep-research/ingest/…)" \
  "loom-builder.service|builder worker (complaint inbox)"; do
  unit="${w%%|*}" ; label="${w#*|}"
  if enable_now "$unit"; then started+=("$label"); else skipped+=("$label (start failed — journalctl --user -u $unit)"); fi
done

# --- channel listeners: enable only if `--check` passes, else disable ----------
# WhatsApp first makes sure the WAHA session is WORKING before the check.
if [ -n "${LOOM_WA_CHAT_ID:-}" ] && [ -n "${LOOM_WA_URL:-}" ]; then
  base="${LOOM_WA_URL%/}/api/sessions/${LOOM_WA_SESSION:-default}"
  st=$(curl -s -H "X-Api-Key: ${LOOM_WA_API_KEY:-}" "$base" \
       | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
  if [ "$st" != "WORKING" ]; then
    echo "WAHA session is '${st:-unreachable}' — starting it…"
    curl -s -X POST -H "X-Api-Key: ${LOOM_WA_API_KEY:-}" -H "Content-Type: application/json" \
      "$base/start" -d '{}' >/dev/null 2>&1 || true
    sleep 4
  fi
fi

check_channel() {  # $1 cli, $2 timer, $3 label
  if [ -x "$VENV/$1" ] && "$VENV/$1" --check >/dev/null 2>&1; then
    if enable_now "$2"; then started+=("$3"); else skipped+=("$3 (start failed)"); fi
  else
    disable_now "$2"
    skipped+=("$3 (nicht konfiguriert / nicht erreichbar)")
  fi
}

check_channel loom-whatsapp loom-whatsapp.timer "whatsapp listener"
check_channel loom-telegram loom-telegram.timer "telegram listener"
check_channel loom-discord  loom-discord.timer  "discord listener"
check_channel loom-imessage loom-imessage.timer "imessage listener"

# --- fitness (Oura/Strava → Tagestrainingsplan): timer only when authed --------
# `--check` passes once at least one service has OAuth tokens (loom-fitness --auth).
if [ -x "$VENV/loom-fitness" ] && "$VENV/loom-fitness" --check >/dev/null 2>&1; then
  if enable_now loom-fitness.timer; then started+=("fitness timer   (Oura/Strava → Tagesplan & Analysen)"); else skipped+=("fitness timer (start failed)"); fi
else
  disable_now loom-fitness.timer
  skipped+=("fitness timer (nicht autorisiert — loom-fitness --auth oura|strava)")
fi

echo
echo "=== gestartet ==="
if [ ${#started[@]} -eq 0 ]; then echo "  (nichts)"; else printf '  ✅ %s\n' "${started[@]}"; fi
echo "=== übersprungen ==="
if [ ${#skipped[@]} -eq 0 ]; then echo "  (nichts)"; else printf '  –  %s\n' "${skipped[@]}"; fi
echo
echo "=== aktive Timer ==="
systemctl --user list-timers 'loom*' --no-pager 2>/dev/null | grep -E 'NEXT|loom' || echo "(keine)"
echo "=== laufende Worker ==="
systemctl --user --no-pager --plain list-units 'loom*' --type=service --state=running 2>/dev/null | grep -E 'loom-(ingest|tasks|builder)' || echo "(keine)"
