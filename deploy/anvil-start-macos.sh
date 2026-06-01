#!/usr/bin/env bash
# anvil-start-macos.sh — macOS counterpart to anvil-start.sh (which is systemd-only).
#
# Brings up the same set as the Linux script, but as plain background processes
# (no systemd/launchd needed): the inbox WORKERS (ingest · tasks · builder, each
# --watch) plus the iMessage channel LISTENER (a --poll loop), creates the vault
# inbox folders, and keeps the Mac awake (caffeinate) while they run.
#
# Idempotent / desired-state: re-run any time — already-running parts are left alone.
# pids + logs live under $ANVIL_STATE_DIR (default ~/.local/state/anvil).
#
#   anvil-start-macos.sh            # create folders + start everything
#   anvil-start-macos.sh stop       # stop everything it started
#   anvil-start-macos.sh status     # show what's running
#   anvil-start-macos.sh restart

set -uo pipefail

ANVIL_DIR="${ANVIL_DIR:-$HOME/anvil-brain}"
ENV_FILE="${ANVIL_ENV:-$HOME/.config/anvil/env}"

# Load the env exactly like ~/.zshrc does — SOURCED, so quotes are honored and the
# ';' separators in ANVIL_BB_CHAT_GUID are not split.
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

STATE_DIR="${ANVIL_STATE_DIR:-$HOME/.local/state/anvil}"
RUN_DIR="$STATE_DIR/run"
LOG_DIR="$STATE_DIR/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

# iMessage poll cadence (seconds) for the listener loop.
POLL_EVERY="${ANVIL_IMESSAGE_POLL_EVERY:-15}"

# Everything this script manages (order matters for a tidy status/stop).
MANAGED="caffeinate ingest tasks builder imessage"

# --- vault inbox folders -------------------------------------------------------
VAULT="${ANVIL_VAULT:-$HOME/ANVIL}"

make_folders() {
  mkdir -p \
    "$VAULT/${ANVIL_INBOX_DIR:-inbox}" \
    "$VAULT/${ANVIL_TASKS_DIR:-tasks}" \
    "$VAULT/${ANVIL_REPORTS_DIR:-reports}" \
    "$VAULT/${ANVIL_BUILDER_INBOX_DIR:-builder-inbox}"/{todo,working,done} \
    "$VAULT/${ANVIL_TASK_QUEUE_DIR:-agent-tasks}"/{todo,working,done} \
    "${ANVIL_INGEST_DIR:-$HOME/anvil-dump}"
}

# --- process management --------------------------------------------------------
pid_of() { local f="$RUN_DIR/$1.pid"; [ -f "$f" ] && cat "$f" 2>/dev/null; }
alive()  { local p; p="$(pid_of "$1")"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

start_bg() {  # $1 = name ; rest = command (run from ANVIL_DIR, detached)
  local name="$1"; shift
  if alive "$name"; then echo "  • $name läuft schon (pid $(pid_of "$name"))"; return; fi
  ( cd "$ANVIL_DIR" && nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 & echo $! >"$RUN_DIR/$name.pid" )
  echo "  ✅ $name gestartet (pid $(pid_of "$name"))  → $LOG_DIR/$name.log"
}

stop_one() {
  local name="$1" p; p="$(pid_of "$name")"
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    pkill -P "$p" 2>/dev/null || true   # children first (uv -> python, loop -> uv)
    kill "$p" 2>/dev/null || true
    echo "  ⏹  $name gestoppt (pid $p)"
  fi
  rm -f "$RUN_DIR/$name.pid"
}

cmd_stop()   { for w in $MANAGED; do stop_one "$w"; done; }
cmd_status() {
  for w in $MANAGED; do
    if alive "$w"; then echo "  ✅ $w (pid $(pid_of "$w"))"; else echo "  –  $w (gestoppt)"; fi
  done
}

cmd_start() {
  echo "== Inbox-Ordner anlegen unter $VAULT =="
  make_folders && echo "  ✅ inbox/ tasks/ reports/ builder-inbox/{todo,working,done} agent-tasks/{todo,working,done} + Dump ${ANVIL_INGEST_DIR:-$HOME/anvil-dump}"

  echo "== Mac wach halten =="
  start_bg caffeinate caffeinate -dimsu

  echo "== Inbox-Worker (--watch) =="
  start_bg ingest  uv run anvil-ingest  --watch
  start_bg tasks   uv run anvil-tasks   --watch
  start_bg builder uv run anvil-builder --watch

  echo "== iMessage-Listener =="
  if ( cd "$ANVIL_DIR" && uv run anvil-imessage --check ) >/dev/null 2>&1; then
    start_bg imessage bash -c "cd '$ANVIL_DIR'; while true; do uv run anvil-imessage --poll; sleep $POLL_EVERY; done"
  else
    echo "  –  imessage übersprungen: BlueBubbles nicht erreichbar / nicht konfiguriert (prüfe: anvil-imessage --check)"
  fi

  echo
  echo "Logs:    tail -f $LOG_DIR/*.log"
  echo "Status:  $0 status"
  echo "Stop:    $0 stop"
}

case "${1:-start}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_stop; sleep 1; cmd_start ;;
  *) echo "usage: $0 [start|stop|status|restart]" >&2; exit 2 ;;
esac
