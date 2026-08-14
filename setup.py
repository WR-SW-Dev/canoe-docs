#!/usr/bin/env python3
"""
setup.py -- First-run configuration for the Canoe -> SharePoint sync.

Prompts in the terminal for every configuration value and writes them to a local
secrets file (~/.config/wr-canoe-sync/secrets.env, mode 600), NOT a .env file in the
repo. The macOS login keychain is deliberately avoided: it is locked after an
unattended reboot, which a launchd job (no interactive session) can't unlock. Secret
fields are entered hidden. It then VALIDATES the Microsoft Graph credentials by making
one harmless call (resolving the target SharePoint library), so a bad install fails
here at setup time rather than the following Monday.

Re-runnable: existing secrets-file values are offered as defaults -- press Enter to keep.

Run (after install.sh, so dependencies and the cert key file are in place):
    python setup.py

Nothing is transmitted anywhere except the single Graph validation call. Getting the
credentials onto the machine (encrypted email / password manager) is a human step.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from getpass import getpass

# The sync modules live in "py files/"; make them importable for the Graph validation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py files"))

# Secrets go to a local file (mode 600), NOT the macOS Keychain: the login keychain is
# locked after an unattended reboot, which a launchd job can't unlock.
SECRETS_DIR = os.path.join(os.path.expanduser("~"), ".config", "wr-canoe-sync")
SECRETS_FILE = os.path.join(SECRETS_DIR, "secrets.env")

# (key, prompt, is_secret, default)
GRAPH_FIELDS = [
    ("GRAPH_TENANT_ID", "Entra ID tenant id (GUID)", False, ""),
    ("GRAPH_CLIENT_ID", "App registration client id (GUID)", False, ""),
    ("GRAPH_CERT_THUMBPRINT", "Certificate thumbprint (hex)", False, ""),
    ("GRAPH_CERT_KEY_PATH", "Absolute path to the certificate private key (PEM)", False, ""),
    ("SP_HOSTNAME", "SharePoint host", False, "wakerobinco.sharepoint.com"),
    ("SP_SITE_PATH", "Site path", False, "/sites/Investment"),
    ("SP_LIBRARY", "Document library (drive) name", False, "Documents"),
    ("SP_ROOT_FOLDER", "Folder within the library to write under", False, "Canoe"),
]
CANOE_FIELDS = [
    ("CANOE_CLIENT_ID", "Canoe Client ID", False, ""),
    ("CANOE_CLIENT_SECRET", "Canoe Client Secret", True, ""),
    ("CANOE_USERNAME", "Canoe service-account username (fallback; optional)", False, ""),
    ("CANOE_PASSWORD", "Canoe service-account password (fallback; optional)", True, ""),
    ("CANOE_ORGANIZATION_ID", "Canoe organization id (only if multiple orgs; optional)", False, ""),
]
RESYNC_FIELDS = [
    ("CANOE_RESYNC_SECRET", "Shared secret required immediately before a dashboard Resync",
     True, secrets.token_urlsafe(32)),
]


def read_secrets() -> dict:
    values = {}
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def write_secrets(values: dict) -> None:
    os.makedirs(SECRETS_DIR, mode=0o700, exist_ok=True)
    os.chmod(SECRETS_DIR, 0o700)
    merged = read_secrets()
    merged.update(values)
    with open(SECRETS_FILE, "w") as f:
        for k, v in merged.items():
            f.write(f"{k}={v}\n")
    os.chmod(SECRETS_FILE, 0o600)


def ask(prompt: str, is_secret: bool, current: str) -> str:
    if current:
        prompt += " [Enter keeps current]" if is_secret else f" [Enter keeps: {current}]"
    try:
        entered = getpass(prompt + ": ") if is_secret else input(prompt + ": ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled -- no changes written.")
        sys.exit(1)
    return entered.strip() or current


def prompt_group(title: str, fields) -> dict:
    print(f"\n{title}")
    current_values = read_secrets()
    out = {}
    for key, prompt, is_secret, default in fields:
        current = current_values.get(key) or default
        val = ask(prompt, is_secret, current)
        if val:
            out[key] = val
    return out


def rotate_resync_secret() -> str:
    value = secrets.token_urlsafe(32)
    write_secrets({"CANOE_RESYNC_SECRET": value})
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the Canoe sync")
    parser.add_argument(
        "--rotate-resync-secret",
        action="store_true",
        help="generate, save, and print only a new dashboard Resync secret",
    )
    args = parser.parse_args()
    if args.rotate_resync_secret:
        print("New dashboard Resync secret (save in 1Password):")
        print(f"  {rotate_resync_secret()}")
        return

    print("Canoe -> SharePoint sync -- configuration")
    print(f"Values are stored in {SECRETS_FILE} (mode 600); nothing is written to the repo.")

    values = {}
    values.update(prompt_group("Microsoft Graph / SharePoint:", GRAPH_FIELDS))
    values.update(prompt_group("Canoe API (Client ID + Secret, and/or username + password):", CANOE_FIELDS))
    values.update(prompt_group("Dashboard Resync authorization:", RESYNC_FIELDS))

    have_client = values.get("CANOE_CLIENT_ID") and values.get("CANOE_CLIENT_SECRET")
    have_pw = values.get("CANOE_USERNAME") and values.get("CANOE_PASSWORD")
    if not (have_client or have_pw):
        print("\nERROR: Canoe needs Client ID + Secret, or username + password. Nothing written.")
        sys.exit(1)

    write_secrets(values)
    print(f"\nSaved {len(values)} value(s) to {SECRETS_FILE}.")
    print("\nSave this dashboard Resync secret in 1Password:")
    print(f"  {values['CANOE_RESYNC_SECRET']}")

    # Validate Graph credentials with one harmless call.
    print("\nValidating Microsoft Graph access (resolving the target library)...")
    try:
        from graph_client import GraphClient  # imported after deps installed
        drive_id = GraphClient().verify_access()
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ Graph validation FAILED: {exc}")
        print("  Check the tenant/client id, thumbprint, key path, site path and library, then re-run setup.py.")
        sys.exit(1)
    print(f"✓ Graph access OK -- reached the SharePoint library (drive id {drive_id[:12]}...).")

    print("\nSetup complete. Verify Canoe auth next:")
    print('  cd "py files" && ../.venv/bin/python credentials_check.py')


if __name__ == "__main__":
    main()
