#!/usr/bin/env python3
"""
graph_client.py -- Upload files to a SharePoint document library via Microsoft Graph.

Authentication is APP-ONLY using a certificate (MSAL confidential client). There is
no user context and no interactive sign-in: a token is acquired for the
`https://graph.microsoft.com/.default` scope using the app registration's private
key. The app registration should be scoped (Sites.Selected) so it can write only to
the one Canoe site.

Upload mechanics:
  * Files <= 4 MB      -> simple PUT to the item's /content.
  * Files > 4 MB       -> a Graph upload session, sent in ~10 MiB chunks.
  * HTTP 429 / 503     -> honour the Retry-After header and back off (bounded retries).

This module raises GraphError on failure; callers decide how to log and exit.
"""

from __future__ import annotations

import os
import time
import requests

import config

try:
    import msal
except ImportError as exc:  # pragma: no cover
    raise SystemExit("msal is required: pip install -r requirements.txt") from exc

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]
SIMPLE_MAX = 4 * 1024 * 1024                 # 4 MB: simple PUT ceiling
CHUNK = 320 * 1024 * 32                       # ~10 MiB, a multiple of 320 KiB (Graph requirement)
MAX_RETRIES = 5
DEFAULT_BACKOFF = 10


class GraphError(RuntimeError):
    pass


class GraphClient:
    def __init__(self):
        g = config.graph()
        self._tenant = g["tenant_id"]
        self._client_id = g["client_id"]
        self._thumbprint = g["thumbprint"]
        key_path = g["key_path"]
        if not os.path.exists(key_path):
            raise GraphError(f"Certificate private key not found at GRAPH_CERT_KEY_PATH: {key_path}")
        with open(key_path, "r") as f:
            private_key = f.read()
        self._app = msal.ConfidentialClientApplication(
            client_id=self._client_id,
            authority=f"https://login.microsoftonline.com/{self._tenant}",
            client_credential={"thumbprint": self._thumbprint, "private_key": private_key},
        )
        self._drive_id = None
        self._root_folder = config.sharepoint()["root_folder"].strip("/")
        self._ensured = set()

    # -- auth ---------------------------------------------------------------
    def _token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=SCOPE)
        if "access_token" not in result:
            raise GraphError(
                f"Token acquisition failed: {result.get('error')}: {result.get('error_description')}"
            )
        return result["access_token"]

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._token()}"}
        if extra:
            h.update(extra)
        return h

    # -- generic request with Retry-After backoff ---------------------------
    def _request(self, method: str, url: str, *, auth: bool = True, headers: dict | None = None, **kwargs) -> requests.Response:
        base_headers = dict(headers or {})
        for attempt in range(1, MAX_RETRIES + 1):
            send_headers = dict(base_headers)
            if auth:
                send_headers["Authorization"] = f"Bearer {self._token()}"
            try:
                resp = requests.request(method, url, headers=send_headers, timeout=120, **kwargs)
            except requests.exceptions.RequestException as exc:
                wait = DEFAULT_BACKOFF * attempt
                if attempt == MAX_RETRIES:
                    raise GraphError(f"{method} {url} failed after retries: {exc}") from exc
                time.sleep(wait)
                continue
            if resp.status_code in (429, 503):
                wait = int(resp.headers.get("Retry-After", DEFAULT_BACKOFF * attempt))
                if attempt == MAX_RETRIES:
                    raise GraphError(f"{method} {url}: throttled ({resp.status_code}) after {MAX_RETRIES} retries")
                time.sleep(max(1, wait))
                continue
            return resp
        raise GraphError(f"{method} {url}: exhausted retries")

    # -- drive resolution ---------------------------------------------------
    def drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id
        sp = config.sharepoint()
        site_url = f"{GRAPH}/sites/{sp['hostname']}:{sp['site_path']}"
        r = self._request("GET", site_url)
        if r.status_code != 200:
            raise GraphError(f"Cannot resolve site {sp['site_path']}: {r.status_code} {r.text[:200]}")
        site_id = r.json()["id"]
        r = self._request("GET", f"{GRAPH}/sites/{site_id}/drives")
        if r.status_code != 200:
            raise GraphError(f"Cannot list drives: {r.status_code} {r.text[:200]}")
        for d in r.json().get("value", []):
            if d.get("name") == sp["library"]:
                self._drive_id = d["id"]
                return self._drive_id
        names = [d.get("name") for d in r.json().get("value", [])]
        raise GraphError(f"Library '{sp['library']}' not found. Available drives: {names}")

    def verify_access(self) -> str:
        """One harmless call used by setup.py to validate credentials. Returns the drive id."""
        return self.drive_id()

    # -- folders ------------------------------------------------------------
    def ensure_folder(self, rel_folder: str) -> None:
        """Create nested folders under the configured root folder if absent."""
        full = "/".join(p for p in [self._root_folder, rel_folder.strip("/")] if p)
        if full in self._ensured:
            return
        drive = self.drive_id()
        segments = [s for s in full.split("/") if s]
        parent = ""
        for seg in segments:
            parent_addr = "root" if not parent else f"root:/{parent}:"
            r = self._request(
                "POST", f"{GRAPH}/drives/{drive}/{parent_addr}/children",
                headers={"Content-Type": "application/json"},
                json={"name": seg, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )
            if r.status_code not in (201, 409):  # created, or already exists
                raise GraphError(f"Cannot create folder '{seg}' under '{parent}': {r.status_code} {r.text[:200]}")
            parent = f"{parent}/{seg}" if parent else seg
        self._ensured.add(full)

    # -- upload -------------------------------------------------------------
    def upload(self, data: bytes, rel_folder: str, filename: str) -> None:
        """Upload bytes to <root_folder>/<rel_folder>/<filename>, replacing any existing item."""
        self.ensure_folder(rel_folder)
        drive = self.drive_id()
        path = "/".join(p for p in [self._root_folder, rel_folder.strip("/"), filename] if p)
        if len(data) <= SIMPLE_MAX:
            self._simple_put(drive, path, data)
        else:
            self._session_upload(drive, path, data)

    def _simple_put(self, drive: str, path: str, data: bytes) -> None:
        url = f"{GRAPH}/drives/{drive}/root:/{path}:/content?@microsoft.graph.conflictBehavior=replace"
        r = self._request("PUT", url, headers={"Content-Type": "application/octet-stream"}, data=data)
        if r.status_code not in (200, 201):
            raise GraphError(f"Upload failed for {path}: {r.status_code} {r.text[:200]}")

    def _session_upload(self, drive: str, path: str, data: bytes) -> None:
        create = self._request(
            "POST", f"{GRAPH}/drives/{drive}/root:/{path}:/createUploadSession",
            headers={"Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        if create.status_code not in (200, 201):
            raise GraphError(f"Cannot create upload session for {path}: {create.status_code} {create.text[:200]}")
        upload_url = create.json()["uploadUrl"]
        total = len(data)
        start = 0
        while start < total:
            end = min(start + CHUNK, total) - 1
            chunk = data[start:end + 1]
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            }
            # The upload URL is pre-authenticated -- do NOT attach the bearer token.
            r = self._request("PUT", upload_url, auth=False, headers=headers, data=chunk)
            if r.status_code in (200, 201):
                return
            if r.status_code != 202:
                raise GraphError(f"Chunk upload failed for {path} at byte {start}: {r.status_code} {r.text[:200]}")
            start = end + 1
