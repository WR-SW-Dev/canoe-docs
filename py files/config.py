#!/usr/bin/env python3
"""
config.py -- Central configuration for the Canoe -> SharePoint sync.

ALL configuration is read from the environment. On the App Server the values are
kept in a local secrets file (~/.config/wr-canoe-sync/secrets.env, mode 600) written
by setup.py and exported into the environment by the run wrapper (or read here as a
fallback for interactive runs). No values are stored in this repository. See
`.env.example` for the full list of keys.

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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Secrets live in a local file, not the macOS Keychain: the login keychain is locked
# after an unattended reboot, which a launchd job (no interactive session) can't unlock,
# so it would silently read empty values. The file is mode 600, written by setup.py.
SECRETS_FILE = os.path.join(os.path.expanduser("~"), ".config", "wr-canoe-sync", "secrets.env")

# Runtime state (manifest, logs, last-run) MUST live on local disk, never in the repo
# (which may sit in a synced folder). Default to the macOS per-user app-data location.
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "canoe-sync")

_secrets_cache: dict[str, str] | None = None


class ConfigError(RuntimeError):
    pass


def _secrets_file(key: str) -> str | None:
    """Fallback lookup in the local secrets file (KEY=VALUE lines, read once and cached)."""
    global _secrets_cache
    if _secrets_cache is None:
        values: dict[str, str] = {}
        try:
            with open(SECRETS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        except FileNotFoundError:
            pass
        _secrets_cache = values
    return _secrets_cache.get(key)


def get(key: str, default: str | None = None, required: bool = True) -> str | None:
    """Read a config value: environment first, then the local secrets file, then default."""
    val = os.environ.get(key, "").strip()
    if not val:
        val = _secrets_file(key) or ""
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


def data_dir() -> str:
    """Local, non-synced base dir for all runtime state (never the repo/OneDrive)."""
    return get("CANOE_DATA_DIR", default=DEFAULT_DATA_DIR)


def manifest_path() -> str:
    return get("CANOE_MANIFEST_PATH", default=os.path.join(data_dir(), "manifest.json"))


def log_dir() -> str:
    return get("CANOE_LOG_DIR", default=os.path.join(data_dir(), "logs"))


def state_path() -> str:
    return get("CANOE_STATE_PATH", default=os.path.join(data_dir(), "last_sync.json"))
