#!/bin/zsh
# One-shot installer for a new machine (macOS).
#
# Prerequisites (see README): Python 3.9+, the OneDrive client signed in to Wake Robin
# with the SharePoint "Canoe" library synced locally, and Canoe API credentials to hand.
#
# Usage:
#   export CANOE_ARCHIVE_DIR="/Users/<you>/Library/CloudStorage/OneDrive-SharedLibraries-WakeRobin/Investment - Documents/Canoe"
#   ./install.sh
#
# What it does:
#   1. Creates the Python virtualenv and installs dependencies.
#   2. Generates the launchd weekly job (Mondays 7am) with the correct paths for THIS machine.
#   3. Prints the remaining manual steps (credentials, verify, load the job).
# It does NOT load the job or touch credentials itself.

set -e
BASE="${0:A:h}"
echo "Repo root : $BASE"

# 1. Virtualenv + dependencies -------------------------------------------------
echo "Creating virtualenv and installing dependencies..."
python3 -m venv "$BASE/.venv"
"$BASE/.venv/bin/pip" install -q --upgrade pip
"$BASE/.venv/bin/pip" install -q -r "$BASE/requirements.txt"
echo "  venv ready: $BASE/.venv"

# 2. Archive location ----------------------------------------------------------
: "${CANOE_ARCHIVE_DIR:?Set CANOE_ARCHIVE_DIR to the local synced SharePoint 'Canoe' path first, then re-run ./install.sh}"
if [ ! -d "$CANOE_ARCHIVE_DIR" ]; then
    echo "ERROR: CANOE_ARCHIVE_DIR does not exist locally:"
    echo "  $CANOE_ARCHIVE_DIR"
    echo "Sync the SharePoint 'Canoe' library via the OneDrive client first."
    exit 1
fi
echo "  archive   : $CANOE_ARCHIVE_DIR"

# 3. Generate the launchd job for this machine --------------------------------
mkdir -p "$HOME/Library/LaunchAgents" "$BASE/logs"
PLIST="$HOME/Library/LaunchAgents/co.wakerobin.canoe.weekly.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>co.wakerobin.canoe.weekly</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$BASE/run_weekly.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CANOE_ARCHIVE_DIR</key>
        <string>$CANOE_ARCHIVE_DIR</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>1</integer>
        <key>Hour</key><integer>7</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>$BASE/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>$BASE/logs/launchd.err.log</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
EOF
echo "  launchd job written: $PLIST"

# 4. Next steps ----------------------------------------------------------------
cat <<NEXT

Install steps complete. Remaining manual steps:

  1. Add Canoe credentials to '$BASE/py files/.env' (or macOS Keychain). See README.
  2. Verify:
       cd "$BASE/py files" && ../.venv/bin/python credentials_check.py
  3. Schedule the weekly job:
       launchctl load -w "$PLIST"
     Test it immediately (optional):
       launchctl start co.wakerobin.canoe.weekly && tail -20 "$BASE/logs/weekly.log"

NEXT
