#!/usr/bin/env bash
# install.sh — interactive Loom setup.
#
# Installs dependencies, then asks the few choices that actually branch the system
# (document extraction backend, LLM provider) plus the matching API keys, and writes
# everything into a single ~/.config/loom/settings.json (chmod 600). Re-run any time;
# it shows your current choices as defaults.
#
#   ./install.sh
#
# Non-interactive / CI: pre-set the env vars used below (LOOM_VAULT, LOOM_EXTRACTION,
# LOOM_PROVIDER, MATHPIX_APP_ID, MATHPIX_APP_KEY) and pipe `yes ''` in.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/loom"
SETTINGS="$CONFIG_DIR/settings.json"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ask() {  # ask "Prompt" "default" -> echoes answer (default if empty)
  local prompt="$1" def="${2:-}" ans
  if [ -n "$def" ]; then read -rp "$prompt [$def]: " ans; else read -rp "$prompt: " ans; fi
  printf '%s' "${ans:-$def}"
}

banner() {  # big colored LOOM (skipped when NO_COLOR is set or output isn't a tty)
  local c='' r=''
  if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then c=$'\033[1;36m'; r=$'\033[0m'; fi
  printf '%s\n' "$c" \
'   ██╗      ██████╗  ██████╗ ███╗   ███╗' \
'   ██║     ██╔═══██╗██╔═══██╗████╗ ████║' \
'   ██║     ██║   ██║██║   ██║██╔████╔██║' \
'   ██║     ██║   ██║██║   ██║██║╚██╔╝██║' \
'   ███████╗╚██████╔╝╚██████╔╝██║ ╚═╝ ██║' \
"   ╚══════╝ ╚═════╝  ╚═════╝ ╚═╝     ╚═╝${r}"
}

banner
bold "Loom installer"
echo "Repo: $REPO_DIR"
echo

# --- 1. dependencies ----------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ 'uv' not found. Install it first: https://docs.astral.sh/uv/  (then re-run)."
  exit 1
fi
bold "1/4  Installing dependencies (uv sync)…"
uv sync || { echo "✗ uv sync failed."; exit 1; }
echo

# --- read existing settings as defaults (best-effort) -------------------------
prev() { [ -f "$SETTINGS" ] && uv run python -c "import json,sys;print(json.load(open('$SETTINGS')).get('$1','') or '')" 2>/dev/null || true; }
DEF_VAULT="$(prev vault)";       DEF_VAULT="${DEF_VAULT:-${LOOM_VAULT:-$HOME/VAULT}}"
DEF_EXTRACTION="$(prev extraction)"; DEF_EXTRACTION="${DEF_EXTRACTION:-${LOOM_EXTRACTION:-markitdown}}"
DEF_PROVIDER="$(prev provider)"; DEF_PROVIDER="${DEF_PROVIDER:-${LOOM_PROVIDER:-claude-code}}"

# --- 2. vault -----------------------------------------------------------------
bold "2/4  Vault"
echo "  1) Create a new vault   (default)"
echo "  2) Use an existing vault"
NEW_VAULT=0
case "$(ask "Choose 1 or 2" "1")" in
  2)
    VAULT="$(ask "  Path to your existing vault" "$DEF_VAULT")"
    ;;
  *)
    NEW_VAULT=1
    VNAME="$(ask "  Name of the new vault" "VAULT")"
    VPARENT="$(ask "  Create it inside which folder" "$HOME")"
    VPARENT="${VPARENT/#\~/$HOME}"
    VAULT="$VPARENT/$VNAME"
    ;;
esac
VAULT="${VAULT/#\~/$HOME}"

if [ "$NEW_VAULT" = 1 ]; then
  if [ -e "$VAULT" ] && [ -n "$(ls -A "$VAULT" 2>/dev/null)" ]; then
    echo "  ! $VAULT already exists and is not empty — using it as-is (nothing overwritten)."
  else
    # Seed the machine-room queues so workers run immediately (code also creates them lazily).
    mkdir -p "$VAULT"/ops/builder-inbox/{todo,working,done} \
             "$VAULT"/ops/inbox \
             "$VAULT"/ops/tasks/{todo,working,done} \
             "$VAULT"/ops/agent-tasks/{todo,working,done} \
             "$VAULT"/ops/reports "$VAULT"/ops/checkpoints \
             "$VAULT"/.trash "$VAULT"/wissen "$VAULT"/eingang
    echo "  ✓ Created new vault at $VAULT (with ops/ queues, wissen/, eingang/, .trash/)."
  fi
elif [ ! -d "$VAULT" ]; then
  echo "  ! $VAULT does not exist yet — it will be created on first use."
fi
echo

# --- 3. document extraction ---------------------------------------------------
bold "3/4  Document extraction backend"
echo "  1) mathpix    — best OCR for PDFs/images/math (needs a Mathpix API key)"
echo "  2) markitdown — local, free, no key (Office/EPub/audio/links; weaker on scanned math)"
EXTRACTION="$DEF_EXTRACTION"
case "$(ask "Choose 1 or 2" "$([ "$DEF_EXTRACTION" = mathpix ] && echo 1 || echo 2)")" in
  1) EXTRACTION="mathpix" ;;
  2) EXTRACTION="markitdown" ;;
esac
MATHPIX_ID=""; MATHPIX_KEY=""
if [ "$EXTRACTION" = "mathpix" ]; then
  MATHPIX_ID="$(ask "  Mathpix APP_ID" "${MATHPIX_APP_ID:-}")"
  read -rsp "  Mathpix APP_KEY (hidden): " MATHPIX_KEY; echo
  MATHPIX_KEY="${MATHPIX_KEY:-${MATHPIX_APP_KEY:-}}"
fi
echo

# --- 4. LLM provider ----------------------------------------------------------
bold "4/4  LLM provider (who runs the thinking)"
echo "  1) claude-code — default; runs on your Claude Code login, no API key here"
echo "  2) hermes      — drive Loom from Hermes Agent with your own key (e.g. Gemini)"
PROVIDER="$DEF_PROVIDER"
case "$(ask "Choose 1 or 2" "$([ "$DEF_PROVIDER" = hermes ] && echo 2 || echo 1)")" in
  1) PROVIDER="claude-code" ;;
  2) PROVIDER="hermes" ;;
esac
echo

# --- write settings.json ------------------------------------------------------
mkdir -p "$CONFIG_DIR"
VAULT="$VAULT" EXTRACTION="$EXTRACTION" PROVIDER="$PROVIDER" \
MATHPIX_ID="$MATHPIX_ID" MATHPIX_KEY="$MATHPIX_KEY" SETTINGS="$SETTINGS" \
uv run python - <<'PY'
import json, os
data = {
    "vault": os.environ["VAULT"],
    "extraction": os.environ["EXTRACTION"],
    "provider": os.environ["PROVIDER"],
}
if os.environ["EXTRACTION"] == "mathpix":
    data["mathpix"] = {"app_id": os.environ.get("MATHPIX_ID", ""),
                       "app_key": os.environ.get("MATHPIX_KEY", "")}
# Power-user escape hatch: any raw LOOM_* overrides go here, untouched by the installer.
existing = {}
try:
    with open(os.environ["SETTINGS"], encoding="utf-8-sig") as fh:
        existing = json.load(fh)
except (OSError, ValueError):
    pass
for k in ("env", "secrets"):
    if isinstance(existing.get(k), dict):
        data[k] = existing[k]
with open(os.environ["SETTINGS"], "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PY
chmod 600 "$SETTINGS"

bold "✓ Wrote $SETTINGS"
echo "    vault:      $VAULT"
echo "    extraction: $EXTRACTION$([ "$EXTRACTION" = mathpix ] && echo " (key stored)")"
echo "    provider:   $PROVIDER"
echo

# --- next steps ---------------------------------------------------------------
bold "Next steps"
if [ "$PROVIDER" = "claude-code" ]; then
  cat <<EOF
  • In Claude Code:  claude plugin marketplace add "$REPO_DIR"
                     claude plugin install loom@loom
    then call /loom:retrieve, /loom:ingest, …
  • Or just the MCP server:
    claude mcp add -s user loom -- uv run --directory "$REPO_DIR" loom-mcp
EOF
else
  cat <<EOF
  • Point Hermes (with your Gemini/other key configured there) at Loom's MCP server:
        uv run --directory "$REPO_DIR" loom-mcp
    and install the portable skill recipes from "$REPO_DIR/skills/" into Hermes.
    Full wiring: docs/hermes-integration.md
EOF
fi
echo "  • Start background workers/listeners (Linux host): $REPO_DIR/deploy/loom-start.sh"
echo "  • Verify settings any time:  uv run loom doctor"
