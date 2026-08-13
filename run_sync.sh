#!/bin/zsh
# Weekly Canoe -> SharePoint sync. Invoked by launchd (Mondays 7am) on the App Server.
#
# Reads configuration from a local secrets file (~/.config/wr-canoe-sync/secrets.env,
# mode 600, written by setup.py), exports it into the environment, and runs canoe_sync.py.
# Self-locating, so it works from any clone location.
#
# Deliberately NOT the macOS Keychain: the login keychain is locked after an unattended
# reboot, which this job (no interactive session) can't unlock -> silent empty secrets.
#
# Runtime state (manifest, logs, last-run) lives under CANOE_DATA_DIR on LOCAL disk --
# never in the repo or a synced folder.

BASE="${0:A:h}"
VENV="$BASE/.venv/bin/python"

# Bridge the local secrets file into the environment (canoe_sync reads the env).
# Read line-by-line rather than `source`/eval, so a secret value is never shell-evaluated
# (a value containing $(...) or backticks would otherwise execute as code).
SECRETS_FILE="$HOME/.config/wr-canoe-sync/secrets.env"
if [[ -f "$SECRETS_FILE" ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    export "$k=$v"
  done < "$SECRETS_FILE"
fi

# Local, non-synced data directory (default: macOS app-support).
DATA_DIR="${CANOE_DATA_DIR:-$HOME/Library/Application Support/canoe-sync}"
export CANOE_DATA_DIR="$DATA_DIR"
mkdir -p "$DATA_DIR/logs"
LOG="$DATA_DIR/logs/run_sync.log"

cd "$BASE/py files" || exit 1
echo "" >> "$LOG"
echo "===== sync run: $(date) =====" >> "$LOG"
"$VENV" canoe_sync.py "$@" >> "$LOG" 2>&1
code=$?
echo "sync exit code: $code" >> "$LOG"
exit $code
