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

# Refresh the two trackers (Canoe metadata only -- no document bodies) and upload their
# grids to <SP_ROOT_FOLDER>/_statement_tracker/ and /_k1_tracker/ in the same library.
# Both run after the document sync so their links resolve against what was just uploaded.
#
# Deliberately not gated on the sync's exit code and deliberately not folded into this
# script's: a tracker reads Canoe metadata, so it still produces a correct grid when an
# upload failed, and a tracker failure must not mark the document sync as broken. Each
# exit code is logged separately, and neither tracker can stop the other from running.
#
# Skipped for modes that upload nothing to SharePoint -- publishing a grid from a
# --dry-run would contradict the flag, and the others write somewhere else entirely.
skip_tracker=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|--seed|--local-dest|--local-dest=*|--export|--export=*) skip_tracker=1 ;;
  esac
done

if (( skip_tracker )); then
  echo "--- statement tracker: skipped (no-upload mode: $*) ---" >> "$LOG"
  echo "--- k-1 tracker: skipped (no-upload mode: $*) ---" >> "$LOG"
else
  echo "--- statement tracker: $(date) ---" >> "$LOG"
  "$VENV" statement_tracker.py --graph >> "$LOG" 2>&1
  echo "tracker exit code: $?" >> "$LOG"

  # K-1s arrive seasonally rather than weekly, so most runs are a no-op refresh; it
  # rides the weekly cadence anyway so the grid is never more than a week stale during
  # filing season. Its own metadata cache and staging dir (CANOE_K1_TRACKER_DIR) keep
  # it independent of the statement tracker's.
  echo "--- k-1 tracker: $(date) ---" >> "$LOG"
  "$VENV" k1_tracker.py --graph >> "$LOG" 2>&1
  echo "k-1 tracker exit code: $?" >> "$LOG"
fi

exit $code
