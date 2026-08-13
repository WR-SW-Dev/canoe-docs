#!/bin/zsh
# Launch the local admin dashboard for the Canoe -> SharePoint sync.
#
# Same secrets-handling as run_sync.sh: reads the local secrets file
# (~/.config/wr-canoe-sync/secrets.env, mode 600) line-by-line and exports it into the
# environment (never `source`/eval, so a secret value is never shell-evaluated). The
# dashboard needs the Graph config for the Reconcile and Resync actions; the Manifest and
# Run-history views work even without it.
#
# The dashboard binds to 127.0.0.1 only. To reach it from another machine, use an SSH
# tunnel to the App Server (e.g. ssh -L 8765:127.0.0.1:8765 appserver) rather than
# exposing it on the network -- it can trigger a full resync.

BASE="${0:A:h}"
VENV="$BASE/.venv/bin/python"

SECRETS_FILE="$HOME/.config/wr-canoe-sync/secrets.env"
if [[ -f "$SECRETS_FILE" ]]; then
  while IFS='=' read -r k v; do
    [[ -z "$k" || "$k" == \#* ]] && continue
    export "$k=$v"
  done < "$SECRETS_FILE"
fi

DATA_DIR="${CANOE_DATA_DIR:-$HOME/Library/Application Support/canoe-sync}"
export CANOE_DATA_DIR="$DATA_DIR"

cd "$BASE/py files" || exit 1
exec "$VENV" dashboard.py "$@"
