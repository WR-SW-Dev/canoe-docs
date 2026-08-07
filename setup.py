#!/usr/bin/env python3
"""
setup.py -- First-run credential setup for Canoe Document Automation.

Prompts in the terminal for the Canoe API credentials and writes them to
`py files/.env` with owner-only permissions (chmod 600). Secret fields are entered
hidden (not echoed to the screen). Nothing is transmitted anywhere -- the values are
written only to this machine's secrets file.

Run:
    python setup.py

This does NOT email or send credentials. Getting the credentials onto the machine
(e.g. via encrypted email or a password manager) is a separate, human step.
"""

import getpass
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "py files", ".env")

# (key, prompt, required-ish, is_secret)
FIELDS = [
    ("CANOE_CLIENT_ID", "Canoe Client ID", True, False),
    ("CANOE_CLIENT_SECRET", "Canoe Client Secret", True, True),
    ("CANOE_USERNAME", "Service-account username (fallback auth; optional)", False, False),
    ("CANOE_PASSWORD", "Service-account password (fallback auth; optional)", False, True),
    ("CANOE_ORGANIZATION_ID", "Organization ID (only if your login has multiple orgs; optional)", False, False),
]


def ask(prompt: str, is_secret: bool) -> str:
    try:
        value = getpass.getpass(prompt + ": ") if is_secret else input(prompt + ": ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled -- no changes written.")
        sys.exit(1)
    return value.strip()


def main() -> None:
    print("Canoe API credential setup")
    print("This writes only to this machine's secrets file; nothing is sent anywhere.\n")

    if os.path.exists(ENV_PATH):
        resp = input(f"{ENV_PATH} already exists. Overwrite? [y/N]: ").strip().lower()
        if resp not in ("y", "yes"):
            print("Left existing file unchanged.")
            return

    print("Enter the Client ID + Secret, and/or a service-account username + password.")
    print("Press Enter to skip an optional field.\n")

    values: dict[str, str] = {}
    for key, prompt, _required, is_secret in FIELDS:
        val = ask(prompt, is_secret)
        if val:
            values[key] = val

    have_client = values.get("CANOE_CLIENT_ID") and values.get("CANOE_CLIENT_SECRET")
    have_pw = values.get("CANOE_USERNAME") and values.get("CANOE_PASSWORD")
    if not (have_client or have_pw):
        print("\nERROR: need either Client ID + Secret, or username + password. Nothing written.")
        sys.exit(1)

    os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
    # Create with owner-only permissions from the start.
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Canoe API credentials -- written by setup.py. Never commit this file.\n")
        for key in values:
            f.write(f"{key}={values[key]}\n")
    os.chmod(ENV_PATH, 0o600)

    print(f"\nSaved {len(values)} value(s) to {ENV_PATH} (permissions: owner read/write only).")
    print("Keys written: " + ", ".join(values.keys()))
    print('\nNext, verify:  cd "py files" && ../.venv/bin/python credentials_check.py')


if __name__ == "__main__":
    main()
