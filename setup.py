#!/usr/bin/env python3
"""
setup.py -- First-run configuration for the Canoe -> SharePoint sync.

Prompts in the terminal for every configuration value and writes them to the
macOS **Keychain** (the App Server's secret store), NOT a .env file. Secret fields
are entered hidden. It then VALIDATES the Microsoft Graph credentials by making one
harmless call (resolving the target SharePoint library), so a bad install fails here
at setup time rather than the following Monday.

Re-runnable: existing Keychain values are offered as defaults -- press Enter to keep.

Run (after install.sh, so dependencies and the cert key file are in place):
    python setup.py

Nothing is transmitted anywhere except the single Graph validation call. Getting the
credentials onto the machine (encrypted email / password manager) is a human step.
"""

from __future__ import annotations

import os
import subprocess
import sys
from getpass import getpass

# The sync modules live in "py files/"; make them importable for the Graph validation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py files"))

KEYCHAIN_SERVICE = "canoe-app"

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


def kc_get(key: str) -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def kc_set(key: str, value: str) -> None:
    subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", key, "-w", value],
        check=True, capture_output=True,
    )


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
    out = {}
    for key, prompt, is_secret, default in fields:
        current = kc_get(key) or default
        val = ask(prompt, is_secret, current)
        if val:
            out[key] = val
    return out


def main() -> None:
    print("Canoe -> SharePoint sync -- configuration")
    print(f"Values are stored in the macOS Keychain (service '{KEYCHAIN_SERVICE}'); nothing is written to disk in the repo.")

    values = {}
    values.update(prompt_group("Microsoft Graph / SharePoint:", GRAPH_FIELDS))
    values.update(prompt_group("Canoe API (Client ID + Secret, and/or username + password):", CANOE_FIELDS))

    have_client = values.get("CANOE_CLIENT_ID") and values.get("CANOE_CLIENT_SECRET")
    have_pw = values.get("CANOE_USERNAME") and values.get("CANOE_PASSWORD")
    if not (have_client or have_pw):
        print("\nERROR: Canoe needs Client ID + Secret, or username + password. Nothing written.")
        sys.exit(1)

    for key, val in values.items():
        kc_set(key, val)
    print(f"\nSaved {len(values)} value(s) to the Keychain.")

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
