#!/bin/zsh
# Weekly Canoe -> SharePoint sync. Invoked by launchd (Mondays 7am) on the App Server.
#
# Reads configuration from the macOS Keychain (service "canoe-app", written by setup.py),
# exports it into the environment, and runs canoe_sync.py. Self-locating, so it works
# from any clone location.
#
# Runtime state (manifest, logs, last-run) lives under CANOE_DATA_DIR on LOCAL disk --
# never in the repo or a synced folder.

BASE="${0:A:h}"
VENV="$BASE/.venv/bin/python"

# Bridge the Keychain secret store into the environment (canoe_sync reads the env).
KEYS=(
  GRAPH_TENANT_ID GRAPH_CLIENT_ID GRAPH_CERT_THUMBPRINT GRAPH_CERT_KEY_PATH
  SP_HOSTNAME SP_SITE_PATH SP_LIBRARY SP_ROOT_FOLDER
  CANOE_CLIENT_ID CANOE_CLIENT_SECRET CANOE_USERNAME CANOE_PASSWORD CANOE_ORGANIZATION_ID
  CANOE_DATA_DIR CANOE_MANIFEST_PATH CANOE_LOG_DIR CANOE_STATE_PATH
)
for k in $KEYS; do
  v=$(security find-generic-password -s canoe-app -a "$k" -w 2>/dev/null) && export $k="$v"
done

# Local, non-synced data directory (default: macOS app-support).
DATA_DIR="${CANOE_DATA_DIR:-$HOME/Library/Application Support/canoe-sync}"
export CANOE_DATA_DIR="$DATA_DIR"
mkdir -p "$DATA_DIR/logs"
LOG="$DATA_DIR/logs/run_sync.log"

cd "$BASE/py files" || exit 1
echo "" >> "$LOG"
echo "===== sync run: $(date) =====" >> "$LOG"
"$VENV" canoe_sync.py >> "$LOG" 2>&1
code=$?
echo "sync exit code: $code" >> "$LOG"
exit $code
