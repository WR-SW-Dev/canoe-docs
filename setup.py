#!/usr/bin/env python3
"""
setup.py -- Credential setup for Canoe Document Automation.

Prompts in the terminal for the Canoe API credentials (and, optionally, the
SMTP settings that let the statement tracker email its weekly digest) and
writes them to `py files/.env` with owner-only permissions (chmod 600).
Secret fields are entered hidden (not echoed to the screen). Nothing is
transmitted anywhere -- the values are written only to this machine's
secrets file.

Safe to re-run: if `.env` already exists its values are kept as defaults --
press Enter at a prompt to keep the current value, so you can add the digest
settings later without re-entering the API credentials.

Run:
    python setup.py

This does NOT email or send credentials. Getting the credentials onto the machine
(e.g. via encrypted email or a password manager) is a separate, human step.
"""

from __future__ import annotations

import getpass
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, "py files", ".env")

# (key, prompt, is_secret)
FIELDS = [
    ("CANOE_CLIENT_ID", "Canoe Client ID", False),
    ("CANOE_CLIENT_SECRET", "Canoe Client Secret", True),
    ("CANOE_USERNAME", "Service-account username (fallback auth; optional)", False),
    ("CANOE_PASSWORD", "Service-account password (fallback auth; optional)", True),
    ("CANOE_ORGANIZATION_ID", "Organization ID (only if your login has multiple orgs; optional)", False),
]

# Optional: lets statement_tracker.py email its weekly digest. The mailbox
# needs "Authenticated SMTP" enabled in the M365 admin center.
DIGEST_FIELDS = [
    ("CANOE_DIGEST_TO", "Digest recipients, comma-separated (optional)", False),
    ("CANOE_SMTP_USER", "SMTP mailbox / username (optional)", False),
    ("CANOE_SMTP_PASS", "SMTP password (optional)", True),
    ("CANOE_SMTP_HOST", "SMTP host (optional; default smtp.office365.com)", False),
    ("CANOE_SMTP_PORT", "SMTP port (optional; default 587)", False),
    ("CANOE_DIGEST_FROM", "Digest From address (optional; defaults to SMTP user)", False),
]


def load_existing(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    values[key.strip()] = val.strip().strip("\"'")
    except OSError:
        pass
    return values


def ask(prompt: str, is_secret: bool, current: str | None) -> str:
    if current:
        prompt += " [Enter keeps current value]" if is_secret else f" [Enter keeps: {current}]"
    try:
        value = getpass.getpass(prompt + ": ") if is_secret else input(prompt + ": ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled -- no changes written.")
        sys.exit(1)
    return value.strip() or (current or "")


def main() -> None:
    print("Canoe API credential setup")
    print("This writes only to this machine's secrets file; nothing is sent anywhere.\n")

    values = load_existing(ENV_PATH)
    if values:
        print(f"{ENV_PATH} exists -- current values are kept unless you type a new one.\n")

    print("Canoe API -- enter the Client ID + Secret, and/or a service-account username + password.")
    print("Press Enter to skip an optional field (or keep its current value).\n")
    for key, prompt, is_secret in FIELDS:
        val = ask(prompt, is_secret, values.get(key))
        if val:
            values[key] = val

    print("\nWeekly digest email (optional -- leave blank to skip; the digest is still")
    print("written to the archive folder either way).\n")
    for key, prompt, is_secret in DIGEST_FIELDS:
        val = ask(prompt, is_secret, values.get(key))
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
