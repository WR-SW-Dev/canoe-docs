#!/bin/zsh
# Weekly incremental pull of new Canoe documents. Invoked by launchd (Mondays 7am).
# Pulls only documents uploaded since the last successful run (tracked in the state file).
#
# Portable: locates itself, so it works from any clone location. The only per-machine
# value is CANOE_ARCHIVE_DIR (the local path of the synced SharePoint "Canoe" folder),
# which install.sh bakes into the launchd job's environment.

BASE="${0:A:h}"                       # directory containing this script (the repo root)
PYDIR="$BASE/py files"
VENV="$BASE/.venv/bin/python"
STATE="$PYDIR/.canoe_last_run.json"
LOG="$BASE/logs/weekly.log"

: "${CANOE_ARCHIVE_DIR:?Set CANOE_ARCHIVE_DIR to the local path of the synced SharePoint 'Canoe' folder}"
DEST="$CANOE_ARCHIVE_DIR"

mkdir -p "$BASE/logs"
cd "$PYDIR" || exit 1
echo "" >> "$LOG"
echo "===== weekly run: $(date) =====" >> "$LOG"
"$VENV" canoe_bulk_download.py \
    --dest "$DEST" \
    --organize year-category \
    --since auto \
    --state "$STATE" >> "$LOG" 2>&1
echo "download exit code: $?" >> "$LOG"

# Refresh the statement tracker (metadata-only) after the document pull.
echo "--- statement tracker: $(date) ---" >> "$LOG"
"$VENV" statement_tracker.py --dest "$DEST" >> "$LOG" 2>&1
echo "tracker exit code: $?" >> "$LOG"
