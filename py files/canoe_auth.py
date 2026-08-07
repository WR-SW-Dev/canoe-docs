#!/usr/bin/env python3
"""
canoe_auth.py -- Canoe Intelligence OAuth authentication with automatic fallback.

Tries client-credentials first. If the tenant has not enabled that endpoint
(404), falls back to password grant using the service account user credentials.

Resolution order for secrets: environment variables, macOS Keychain, .env file.
Tokens are cached in memory only and auto-refreshed when they expire.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Auto-load .env from the project root
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load key=value pairs from a .env file in the project root into os.environ."""
    project_root = Path(__file__).resolve().parent
    dotenv_path = project_root / ".env"
    if not dotenv_path.exists():
        return
    try:
        with open(dotenv_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass

_load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_URL_CLIENT_CREDS = "https://api.canoesoftware.com/oauth/token/client-credentials"
TOKEN_URL_PASSWORD = "https://api.canoesoftware.com/v1/tokens"
BASE_URL = "https://api.canoesoftware.com"
_TOKEN_REFRESH_BUFFER_SECONDS = 300


# ---------------------------------------------------------------------------
# Keychain helpers (macOS)
# ---------------------------------------------------------------------------


def _keychain_get(service: str, account: str) -> Optional[str]:
    """Read a password from the macOS Keychain. Returns None if not found."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    return None


def _load_secret(name: str, env_var: str, keychain_service: str, keychain_account: str) -> str:
    """Resolve a secret from env var first, then macOS Keychain."""
    value = os.environ.get(env_var, "").strip()
    if value:
        return value
    value = _keychain_get(keychain_service, keychain_account)
    if value:
        return value
    raise RuntimeError(
        f"Cannot find secret '{name}'. "
        f"Set the {env_var} environment variable, store it in the macOS Keychain "
        f"(security add-generic-password -s {keychain_service} -a {keychain_account} -w '<value>'), "
        f"or add it to .env in the project root."
    )


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------


@dataclass
class TokenCache:
    """Thread-unsafe in-memory token cache for a single process."""

    access_token: Optional[str] = None
    expires_at: float = 0.0
    # Track which flow worked so we can reuse it consistently.
    flow: str = "unknown"  # "client_credentials" or "password"

    def is_valid(self) -> bool:
        """True if we hold a token that won't expire within the refresh buffer."""
        return bool(self.access_token) and time.time() < (self.expires_at - _TOKEN_REFRESH_BUFFER_SECONDS)


_token_cache = TokenCache()


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------


def get_client_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret)."""
    client_id = _load_secret(
        name="Canoe Client ID",
        env_var="CANOE_CLIENT_ID",
        keychain_service="canoe-api",
        keychain_account="client_id",
    )
    client_secret = _load_secret(
        name="Canoe Client Secret",
        env_var="CANOE_CLIENT_SECRET",
        keychain_service="canoe-api",
        keychain_account="client_secret",
    )
    return client_id, client_secret


def get_password_credentials() -> tuple[str, str]:
    """Return (username, password) for the password-grant flow."""
    username = _load_secret(
        name="Canoe Username",
        env_var="CANOE_USERNAME",
        keychain_service="canoe-api",
        keychain_account="username",
    )
    password = _load_secret(
        name="Canoe Password",
        env_var="CANOE_PASSWORD",
        keychain_service="canoe-api",
        keychain_account="password",
    )
    return username, password


# ---------------------------------------------------------------------------
# Token requests
# ---------------------------------------------------------------------------


def _request_token(url: str, payload: dict) -> dict:
    """Send a token request and return parsed JSON. Raises on non-200."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Token request failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token response missing 'access_token': {data}")
    return data


def fetch_token_client_credentials(client_id: str, client_secret: str) -> dict:
    """Request token via client-credentials grant."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return _request_token(TOKEN_URL_CLIENT_CREDS, payload)


def fetch_token_password(username: str, password: str, organization_id: str = "") -> dict:
    """Request token via password grant."""
    payload = {
        "username": username,
        "password": password,
    }
    if organization_id:
        payload["organization_id"] = organization_id
    return _request_token(TOKEN_URL_PASSWORD, payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_token() -> dict:
    """
    Return a token response dict.
    Tries the last successful flow first, then the alternative.
    """
    global _token_cache

    # If we already have a working flow, try it first.
    preferred_flow = _token_cache.flow if _token_cache.flow != "unknown" else None

    errors: list[str] = []

    flows = []
    if preferred_flow == "client_credentials":
        flows.append(("client_credentials", lambda: _try_client_credentials()))
        flows.append(("password", lambda: _try_password()))
    elif preferred_flow == "password":
        flows.append(("password", lambda: _try_password()))
        flows.append(("client_credentials", lambda: _try_client_credentials()))
    else:
        # No prior preference: try client_credentials first, then password.
        flows.append(("client_credentials", lambda: _try_client_credentials()))
        flows.append(("password", lambda: _try_password()))

    for flow_name, attempt in flows:
        try:
            data = attempt()
            _token_cache.flow = flow_name
            return data
        except RuntimeError as exc:
            errors.append(f"{flow_name}: {exc}")
            continue

    # If both failed, report all errors.
    print("ERROR: All authentication flows failed.")
    for err in errors:
        print(f"  {err}")
    print("\nHints:")
    print("  - If client_credentials returned 404: Canoe may not have enabled it for your tenant.")
    print("  - If password grant failed: verify the service account user credentials in .env.")
    sys.exit(1)


def _try_client_credentials() -> dict:
    client_id, client_secret = get_client_credentials()
    return fetch_token_client_credentials(client_id, client_secret)


def _try_password() -> dict:
    username, password = get_password_credentials()
    org_id = os.environ.get("CANOE_ORGANIZATION_ID", "").strip()
    return fetch_token_password(username, password, org_id)


def get_valid_token() -> str:
    """Return a valid Bearer access token. Caches in memory and auto-refreshes."""
    global _token_cache
    if _token_cache.is_valid():
        return _token_cache.access_token

    data = fetch_token()
    expires_in = data.get("expires_in", 24 * 3600)
    _token_cache.access_token = data["access_token"]
    _token_cache.expires_at = time.time() + int(expires_in)
    return _token_cache.access_token


def get_auth_headers() -> dict[str, str]:
    """Return headers dict ready to pass to requests calls."""
    token = get_valid_token()
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def api_get(path: str, params: dict | None = None) -> dict:
    """Generic authenticated GET. Returns JSON body."""
    url = urllib.parse.urljoin(BASE_URL + "/", path.lstrip("/"))
    resp = requests.get(url, headers=get_auth_headers(), params=params, timeout=60)
    if resp.status_code == 401:
        global _token_cache
        _token_cache.access_token = None
        _token_cache.expires_at = 0.0
        raise RuntimeError(
            "401 Unauthorized from Canoe -- credentials may be misconfigured. "
            "Token has been invalidated; next call will re-authenticate."
        )
    resp.raise_for_status()
    return resp.json()


def api_get_bytes(path: str, params: dict | None = None) -> bytes:
    """Authenticated GET that returns raw bytes (for document downloads)."""
    url = urllib.parse.urljoin(BASE_URL + "/", path.lstrip("/"))
    resp = requests.get(url, headers=get_auth_headers(), params=params, timeout=120)
    if resp.status_code == 401:
        global _token_cache
        _token_cache.access_token = None
        _token_cache.expires_at = 0.0
        raise RuntimeError("401 Unauthorized during download -- credentials may be misconfigured.")
    resp.raise_for_status()
    return resp.content
