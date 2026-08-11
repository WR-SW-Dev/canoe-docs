#!/usr/bin/env python3
"""
config.py -- Central configuration for the Canoe -> SharePoint sync.

ALL configuration is read from the environment. On the App Server the values are
kept in the macOS Keychain by setup.py and exported into the environment by the
run wrapper (or read here as a Keychain fallback for interactive runs). No values
are stored in this repository. See `.env.example` for the full list of keys.

Keys
----
Microsoft Graph / SharePoint (app-only certificate auth):
  GRAPH_TENANT_ID          Entra ID (Azure AD) tenant id (GUID)
  GRAPH_CLIENT_ID          App registration (application/client) id (GUID)
  GRAPH_CERT_THUMBPRINT    Certificate thumbprint (hex, no spaces)
  GRAPH_CERT_KEY_PATH      Absolute path to the certificate PRIVATE KEY file (PEM)
  SP_HOSTNAME              SharePoint host, e.g. wakerobinco.sharepoint.com
  SP_SITE_PATH             Server-relative site path, e.g. /sites/Investment
  SP_LIBRARY               Document library (drive) name, e.g. Documents
  SP_ROOT_FOLDER           Folder within the library to write under (default: Canoe)

Runtime:
  CANOE_MANIFEST_PATH      Manifest JSON path (default: <repo>/.state/manifest.json)
  CANOE_LOG_DIR            Directory for per-run logs (default: <repo>/logs)

Canoe API credentials are read separately by canoe_auth.py
(CANOE_CLIENT_ID / CANOE_CLIENT_SECRET, or CANOE_USERNAME / CANOE_PASSWORD).
"""

from __future__ import annotations

import os
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYCHAIN_SERVICE = "canoe-app"


class ConfigError(RuntimeError):
    pass


def _keychain(key: str) -> str | None:
    """Fallback lookup in the macOS Keychain (service 'canoe-app', account=<KEY>)."""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", key, "-w"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    return None


def get(key: str, default: str | None = None, required: bool = True) -> str | None:
    """Read a config value: environment first, then Keychain, then default."""
    val = os.environ.get(key, "").strip()
    if not val:
        val = _keychain(key) or ""
    if not val:
        if default is not None:
            return default
        if required:
            raise ConfigError(
                f"Missing configuration '{key}'. Set it in the environment or run setup.py. "
                f"See .env.example."
            )
        return None
    return val


# Convenience accessors -------------------------------------------------------

def graph() -> dict:
    return {
        "tenant_id": get("GRAPH_TENANT_ID"),
        "client_id": get("GRAPH_CLIENT_ID"),
        "thumbprint": get("GRAPH_CERT_THUMBPRINT"),
        "key_path": get("GRAPH_CERT_KEY_PATH"),
    }


def sharepoint() -> dict:
    return {
        "hostname": get("SP_HOSTNAME"),
        "site_path": get("SP_SITE_PATH"),
        "library": get("SP_LIBRARY"),
        "root_folder": get("SP_ROOT_FOLDER", default="Canoe"),
    }


def manifest_path() -> str:
    return get("CANOE_MANIFEST_PATH", default=os.path.join(REPO_ROOT, ".state", "manifest.json"))


def log_dir() -> str:
    return get("CANOE_LOG_DIR", default=os.path.join(REPO_ROOT, "logs"))
