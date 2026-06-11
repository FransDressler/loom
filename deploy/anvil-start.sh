#!/usr/bin/env bash
# anvil-start.sh — bring up every ANVIL inbox worker + every CONFIGURED & REACHABLE
# channel listener in one go. Idempotent / desired-state: re-run any time.
#
#   inbox workers (always on, --watch):  ingest · tasks · builder
#   channel listeners (--poll timers):   whatsapp · telegram · discord · imessage
#
# A channel listener is enabled only when its `--check` passes (credentials set AND
# the relay/API reachable); otherwise it is DISABLED, so placeholder/offline channels
# never run a failing poll every 2 minutes.
#
# Usage:  ~/anvil-brain/deploy/anvil-start.sh

set -uo pipefail

ANVIL_DIR="${ANVIL_DIR:-$HOME/anvil-brain}"
ENV_FILE="${ANVIL_ENV:-$HOME/.config/anvil/env}"
UNIT_DIR="$HOME/.config/systemd/user"
DEPLOY="$ANVIL_DIR/deploy"
VENV="$ANVIL_DIR/.venv/bin"

# Load the env SAFELY into our environment — `export "KEY=VALUE"` never executes the
# value, so lines like ANVIL_BB_CHAT_GUID=iMessage;-;+49… don't get split on ';'.
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
cp -f "$DEPLOY"/anvil-*.service "$DEPLOY"/anvil-*.timer "$UNIT_DIR"/ 2>/dev/null || true
systemctl --user daemon-reload

started=() ; skipped=()

enable_now() { systemctl --user enable --now "$1" >/dev/null 2>&1; }
disable_now() { systemctl --user disable --now "$1" >/dev/null 2>&1; systemctl --user reset-failed "${1%.timer}.service" >/dev/null 2>&1 || true; }

# --- inbox workers (--watch services) — always on -----------------------------
for w in \
  "anvil-ingest.service|ingest worker  (dump folder ~/anvil-dump → raw→Wiki)" \
  "anvil-tasks.service|tasks worker   (skill queue: deep-research/ingest/…)" \
  "anvil-builder.service|builder worker (complaint inbox)"; do
  unit="${w%%|*}" ; label="${w#*|}"
  if enable_now "$unit"; then started+=("$label"); else skipped+=("$label (start failed — journalctl --user -u $unit)"); fi
done

# --- channel listeners: enable only if `--check` passes, else disable ----------
# WhatsApp first makes sure the WAHA session is WORKING before the check.
if [ -n "${ANVIL_WA_CHAT_ID:-}" ] && [ -n "${ANVIL_WA_URL:-}" ]; then
  base="${ANVIL_WA_URL%/}/api/sessions/${ANVIL_WA_SESSION:-default}"
  st=$(curl -s -H "X-Api-Key: ${ANVIL_WA_API_KEY:-}" "$base" \
       | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "")
  if [ "$st" != "WORKING" ]; then
    echo "WAHA session is '${st:-unreachable}' — starting it…"
    curl -s -X POST -H "X-Api-Key: ${ANVIL_WA_API_KEY:-}" -H "Content-Type: application/json" \
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

check_channel anvil-whatsapp anvil-whatsapp.timer "whatsapp listener"
check_channel anvil-telegram anvil-telegram.timer "telegram listener"
check_channel anvil-discord  anvil-discord.timer  "discord listener"
check_channel anvil-imessage anvil-imessage.timer "imessage listener"

# --- fitness (Oura/Strava → Tagestrainingsplan): timer only when authed --------
# `--check` passes once at least one service has OAuth tokens (anvil-fitness --auth).
if [ -x "$VENV/anvil-fitness" ] && "$VENV/anvil-fitness" --check >/dev/null 2>&1; then
  if enable_now anvil-fitness.timer; then started+=("fitness timer   (Oura/Strava → Tagesplan & Analysen)"); else skipped+=("fitness timer (start failed)"); fi
else
  disable_now anvil-fitness.timer
  skipped+=("fitness timer (nicht autorisiert — anvil-fitness --auth oura|strava)")
fi

echo
echo "=== gestartet ==="
if [ ${#started[@]} -eq 0 ]; then echo "  (nichts)"; else printf '  ✅ %s\n' "${started[@]}"; fi
echo "=== übersprungen ==="
if [ ${#skipped[@]} -eq 0 ]; then echo "  (nichts)"; else printf '  –  %s\n' "${skipped[@]}"; fi
echo
echo "=== aktive Timer ==="
systemctl --user list-timers 'anvil*' --no-pager 2>/dev/null | grep -E 'NEXT|anvil' || echo "(keine)"
echo "=== laufende Worker ==="
systemctl --user --no-pager --plain list-units 'anvil*' --type=service --state=running 2>/dev/null | grep -E 'anvil-(ingest|tasks|builder)' || echo "(keine)"
