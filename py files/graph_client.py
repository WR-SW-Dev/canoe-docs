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

# Folders under the root that hold tooling output rather than synced Canoe documents.
# list_files() hides them, because its callers (the dashboard's reconcile, canoe_sync
# --export) treat any live file with no manifest entry as an orphan -- and the statement
# tracker's grids, which are generated here rather than pulled from Canoe, would
# otherwise show up as a forever-growing list of orphans. Keep in step with
# statement_tracker.SUBDIR (asserted there).
NON_DOCUMENT_FOLDERS = {"_statement_tracker"}


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

    def list_files(self) -> list[dict]:
        """Recursively list every file actually in <root_folder> in the live library.

        Returns [{"path": <path relative to root_folder>, "name", "size", "item_id",
        "web_url"}]. This is the authoritative "what's really in SharePoint" view --
        use it for inventory/reconciliation rather than a local mirror, which can diverge.

        Tooling-output folders (NON_DOCUMENT_FOLDERS) are excluded: this answers "which
        Canoe documents are in the library", which is what reconciliation compares
        against the manifest.
        """
        return self.list_tree(skip=NON_DOCUMENT_FOLDERS)["files"]

    def list_tree(self, skip: set | None = None) -> dict:
        """Recursively list <root_folder>, returning both files and folders.

        Returns {"files": [...], "folders": [...]}; folder entries carry the same
        keys as files minus "size". Both include "web_url" -- the item's real
        SharePoint URL, which is the only reliable way to build a link (the library's
        URL segment is not its display name: "Documents" lives at /Shared Documents).

        `skip` is a set of top-level folder names not to descend into; the statement
        tracker passes its own output folder so a run never indexes its own outputs.
        """
        drive = self.drive_id()
        root = self._root_folder
        skip = skip or set()
        files: list[dict] = []
        folders: list[dict] = []
        stack = [root] if root else [""]
        while stack:
            folder = stack.pop()
            addr = "root" if not folder else f"root:/{folder}:"
            url = f"{GRAPH}/drives/{drive}/{addr}/children?$top=200&$select=name,size,id,folder,file,webUrl"
            while url:
                r = self._request("GET", url)
                if r.status_code == 404:
                    break  # folder not present yet
                if r.status_code != 200:
                    raise GraphError(f"Listing '{folder}' failed: {r.status_code} {r.text[:200]}")
                data = r.json()
                for item in data.get("value", []):
                    child = f"{folder}/{item['name']}" if folder else item["name"]
                    rel = child[len(root) + 1:] if root and child.startswith(root + "/") else child
                    entry = {"path": rel, "name": item["name"], "item_id": item["id"],
                             "web_url": item.get("webUrl", "")}
                    if "folder" in item:
                        folders.append(entry)
                        if rel not in skip:
                            stack.append(child)
                    else:
                        files.append({**entry, "size": item.get("size", 0)})
                url = data.get("@odata.nextLink")
        return {"files": files, "folders": folders}

    def list_folder(self, rel_folder: str) -> list[dict]:
        """Non-recursive listing of <root_folder>/<rel_folder>; [] if absent.

        Entries carry "name", "path", "item_id", "web_url" and "is_folder".
        """
        drive = self.drive_id()
        folder = "/".join(p for p in [self._root_folder, rel_folder.strip("/")] if p)
        addr = "root" if not folder else f"root:/{folder}:"
        url = f"{GRAPH}/drives/{drive}/{addr}/children?$top=200&$select=name,size,id,folder,file,webUrl"
        out: list[dict] = []
        rel = rel_folder.strip("/")
        while url:
            r = self._request("GET", url)
            if r.status_code == 404:
                return out
            if r.status_code != 200:
                raise GraphError(f"Listing '{folder}' failed: {r.status_code} {r.text[:200]}")
            data = r.json()
            for item in data.get("value", []):
                out.append({"name": item["name"],
                            "path": f"{rel}/{item['name']}" if rel else item["name"],
                            "item_id": item["id"], "web_url": item.get("webUrl", ""),
                            "is_folder": "folder" in item, "size": item.get("size", 0)})
            url = data.get("@odata.nextLink")
        return out

    # -- download -----------------------------------------------------------
    def download(self, rel_path: str) -> bytes | None:
        """Fetch <root_folder>/<rel_path> as bytes; None if it does not exist.

        Used for state a person is expected to edit in SharePoint (the tracker's
        schedule workbook): the live copy must win over any stale local one.
        """
        drive = self.drive_id()
        path = "/".join(p for p in [self._root_folder, rel_path.strip("/")] if p)
        r = self._request("GET", f"{GRAPH}/drives/{drive}/root:/{path}:/content")
        if r.status_code == 404:
            return None
        if r.status_code not in (200, 206):
            raise GraphError(f"Download failed for {path}: {r.status_code} {r.text[:200]}")
        return r.content

    # -- move ---------------------------------------------------------------
    def move(self, rel_path: str, dest_rel_folder: str, new_name: str | None = None) -> None:
        """Move <root_folder>/<rel_path> into <root_folder>/<dest_rel_folder>.

        A PATCH on parentReference relocates the item in place -- no download and
        re-upload, so the item id (and anyone's link to it) survives. `new_name`
        renames as part of the same move; callers that care about collisions pass a
        name they have already checked against the destination listing, because a
        move onto an existing name fails rather than silently overwriting.
        """
        self.ensure_folder(dest_rel_folder)
        drive = self.drive_id()
        src = "/".join(p for p in [self._root_folder, rel_path.strip("/")] if p)
        dest = "/".join(p for p in [self._root_folder, dest_rel_folder.strip("/")] if p)
        r = self._request("GET", f"{GRAPH}/drives/{drive}/root:/{dest}:?$select=id")
        if r.status_code != 200:
            raise GraphError(f"Cannot resolve destination folder '{dest}': {r.status_code} {r.text[:200]}")
        body: dict = {"parentReference": {"id": r.json()["id"]}}
        if new_name:
            body["name"] = new_name
        r = self._request("PATCH", f"{GRAPH}/drives/{drive}/root:/{src}:",
                          headers={"Content-Type": "application/json"}, json=body)
        if r.status_code not in (200, 201):
            raise GraphError(f"Move of '{src}' -> '{dest}' failed: {r.status_code} {r.text[:200]}")

    @property
    def root_folder(self) -> str:
        return self._root_folder

    def rename_root_folder(self, new_name: str) -> str:
        """Rename the configured root folder in place (e.g. Canoe -> Canoe_archive_2026-08-13).

        Used by the dashboard's "resync" action to archive the current library contents
        before a fresh full sync: an in-place PATCH on the folder's driveItem keeps every
        file under the renamed folder (no copy, no re-upload). The next sync recreates the
        original root and writes into it. Returns the new folder name.

        Raises GraphError if no root folder is configured (writing at the drive root cannot
        be renamed) or if the folder does not exist.
        """
        root = self._root_folder
        if not root:
            raise GraphError("No SP_ROOT_FOLDER configured; refusing to rename the drive root.")
        new_name = new_name.strip().strip("/")
        if not new_name or "/" in new_name:
            raise GraphError(f"Invalid new folder name: {new_name!r}")
        drive = self.drive_id()
        r = self._request(
            "PATCH", f"{GRAPH}/drives/{drive}/root:/{root}:",
            headers={"Content-Type": "application/json"},
            json={"name": new_name},
        )
        if r.status_code == 404:
            raise GraphError(f"Root folder '{root}' not found; nothing to archive.")
        if r.status_code not in (200, 201):
            raise GraphError(f"Rename of '{root}' -> '{new_name}' failed: {r.status_code} {r.text[:200]}")
        self._ensured.clear()  # cached folder-existence assumptions no longer hold
        return new_name

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
