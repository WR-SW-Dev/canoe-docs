#!/bin/zsh
# One-shot installer for a new machine (macOS App Server) -- Canoe -> SharePoint sync.
#
# Prerequisites (see README):
#   - Python 3.9+ and git.
#   - The Entra ID app-registration certificate PRIVATE KEY file placed on this machine.
#   - The Graph + Canoe credential values to hand (entered via setup.py after this).
#
# Usage:
#   ./install.sh
#
# What it does:
#   1. Creates the Python virtualenv and installs dependencies.
#   2. Generates the launchd weekly job (Mondays 7am) that runs run_sync.sh.
# It does NOT load the job or touch credentials -- see the printed next steps.

set -e
BASE="${0:A:h}"
echo "Repo root : $BASE"

echo "Creating virtualenv and installing dependencies..."
python3 -m venv "$BASE/.venv"
"$BASE/.venv/bin/pip" install -q --upgrade pip
"$BASE/.venv/bin/pip" install -q -r "$BASE/requirements.txt"
echo "  venv ready: $BASE/.venv"

# Runtime state (manifest, logs, last-run) lives on LOCAL disk, not in the repo.
DATA_DIR="${CANOE_DATA_DIR:-$HOME/Library/Application Support/canoe-sync}"
mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR/logs"
echo "  data dir  : $DATA_DIR"
PLIST="$HOME/Library/LaunchAgents/co.wakerobin.canoe.sync.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>co.wakerobin.canoe.sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$BASE/run_sync.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>1</integer>
        <key>Hour</key><integer>7</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key><string>$DATA_DIR/logs/launchd.out.log</string>
    <key>StandardErrorPath</key><string>$DATA_DIR/logs/launchd.err.log</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
EOF
echo "  launchd job written: $PLIST"

cat <<NEXT

Install steps complete. Remaining manual steps:

  1. Ensure the app-registration certificate PRIVATE KEY (PEM) is on this machine,
     readable only by this user (chmod 600), and note its absolute path.

  2. Configure credentials -- writes to the macOS Keychain and validates Graph access:
       "$BASE/.venv/bin/python" "$BASE/setup.py"

  3. Verify Canoe auth:
       cd "$BASE/py files" && ../.venv/bin/python credentials_check.py

  4. Schedule the weekly sync:
       launchctl load -w "$PLIST"
     Test it immediately (optional):
       launchctl start co.wakerobin.canoe.sync && tail -30 "$BASE/logs/run_sync.log"

NEXT
