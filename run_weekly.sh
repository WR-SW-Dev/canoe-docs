#!/bin/zsh
# Weekly incremental pull of new Canoe documents. Invoked by launchd (Mondays 7am ET).
# Pulls only documents uploaded since the last successful run (tracked in the state file).

BASE="/Users/jasonbyrne/Library/CloudStorage/OneDrive-WakeRobin/Canoe/Canoe API"
PYDIR="$BASE/py files"
VENV="$BASE/.venv/bin/python"
DEST="/Users/jasonbyrne/Library/CloudStorage/OneDrive-WakeRobin/Canoe/Private Fund Reporting"
STATE="$PYDIR/.canoe_last_run.json"
LOG="$BASE/logs/weekly.log"

mkdir -p "$BASE/logs"
cd "$PYDIR" || exit 1
echo "" >> "$LOG"
echo "===== weekly run: $(date) =====" >> "$LOG"
"$VENV" canoe_bulk_download.py \
    --dest "$DEST" \
    --organize year-category \
    --since auto \
    --state "$STATE" >> "$LOG" 2>&1
echo "exit code: $?" >> "$LOG"
