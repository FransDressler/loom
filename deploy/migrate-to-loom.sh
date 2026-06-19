#!/usr/bin/env bash
# migrate-to-loom.sh — migrate a live pre-rename (anvil) host to the loom layout.
#
# Renames the runtime dirs and reinstalls the systemd units under their new loom-*
# names. Idempotent and non-destructive: it MOVES (never deletes) and skips anything
# already migrated. The code reads the old anvil locations as a fallback anyway, so
# running this is about cleanliness + getting the new unit names installed.
#
#   ~/loom/deploy/migrate-to-loom.sh
#
# After it finishes, start the workers with:  ~/loom/deploy/loom-start.sh

set -uo pipefail

CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '  %s\n' "$1"; }
move() {  # move OLD NEW  — only if OLD exists and NEW doesn't
  local old="$1" new="$2"
  if [ -e "$old" ] && [ ! -e "$new" ]; then
    mv "$old" "$new"; say "moved $old → $new"
  elif [ -e "$old" ] && [ -e "$new" ]; then
    say "skip  $new already exists (left $old in place)"
  fi
}

echo "Loom migration"

# --- 1. stop + remove the old anvil-* systemd units ---------------------------
echo "1) old systemd units (anvil-*)"
mapfile -t OLD_UNITS < <(systemctl --user list-unit-files 'anvil-*' --no-legend 2>/dev/null | awk '{print $1}')
for u in "${OLD_UNITS[@]:-}"; do
  [ -n "$u" ] || continue
  systemctl --user disable --now "$u" >/dev/null 2>&1 || true
  say "disabled $u"
done
for u in "${OLD_UNITS[@]:-}"; do
  [ -n "$u" ] || continue
  rm -f "$UNIT_DIR/$u"
  systemctl --user reset-failed "${u%.timer}.service" >/dev/null 2>&1 || true
done
systemctl --user daemon-reload 2>/dev/null || true

# --- 2. runtime directories ---------------------------------------------------
echo "2) runtime directories"
move "$CFG/anvil"                  "$CFG/loom"
move "$HOME/.local/state/anvil"    "$HOME/.local/state/loom"
move "$HOME/anvil-dump"            "$HOME/loom-dump"
move "$HOME/anvil-feynman"         "$HOME/loom-feynman"

# --- 3. env file: rename ANVIL_* keys to LOOM_* (back-compat shim reads both, but
#        a clean file is nicer). Keeps a .bak. -------------------------------------
ENV_FILE="$CFG/loom/env"
if [ -f "$ENV_FILE" ] && grep -q '^[[:space:]]*\(export \)\?ANVIL_' "$ENV_FILE"; then
  cp -f "$ENV_FILE" "$ENV_FILE.bak"
  sed -i -E 's/^([[:space:]]*(export )?)ANVIL_/\1LOOM_/' "$ENV_FILE"
  say "rewrote ANVIL_* → LOOM_* in $ENV_FILE (backup: $ENV_FILE.bak)"
fi

# --- 4. install + start the new loom-* units ----------------------------------
echo "3) install + start loom-* units"
if [ -x "$REPO_DIR/deploy/loom-start.sh" ]; then
  "$REPO_DIR/deploy/loom-start.sh"
else
  say "loom-start.sh not found/executable at $REPO_DIR/deploy — run it manually."
fi

echo
echo "✓ Migration done. Verify:  uv run --directory \"$REPO_DIR\" loom doctor"
