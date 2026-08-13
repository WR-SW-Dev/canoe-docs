#!/usr/bin/env python3
"""
canoe_sync.py -- Sync Canoe documents to a SharePoint library via Microsoft Graph.

Flow
----
1. Discover documents from Canoe metadata (GET /v1/documents/data), incrementally
   since the last successful run (full on the first run).
2. For each document not already in the manifest (keyed on the Canoe document id):
   download its bytes (GET /v1/documents/{id}) and upload them to SharePoint under
   <root>/<Fund>/<Year>/<Category>/<name>, then record it in the manifest.
3. Write a dated run log (documents fetched / skipped / uploaded, and any errors with
   the document id attached) and exit non-zero if any document failed.

Idempotent: a rerun skips everything already in the manifest and uploads with
"replace" semantics, so it never duplicates a document in the library.

There is no email/Teams notification here by design.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import requests

import canoe_auth
import config
from graph_client import GraphClient, GraphError
from manifest import Manifest

DATA_URL = "https://api.canoesoftware.com/v1/documents/data"
DOC_URL = "https://api.canoesoftware.com/v1/documents"
UNKNOWN_FUND = "Unknown Investment"


def _sanitize(value: str) -> str:
    value = (value or "").strip()
    for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        value = value.replace(ch, "-")
    return value.strip().strip(".") or "unknown"


def setup_logging(log_dir: str) -> str:
    os.makedirs(log_dir, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(log_dir, f"canoe_sync_{day}.log")
    handlers = [logging.FileHandler(path), logging.StreamHandler(sys.stdout)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return path


# -- state (incremental window) ------------------------------------------------
def load_since() -> str | None:
    p = config.state_path()
    if os.path.exists(p):
        try:
            return json.load(open(p)).get("last_run_iso")
        except (OSError, ValueError):
            return None
    return None


def save_since(iso: str) -> None:
    p = config.state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"last_run_iso": iso}, open(p, "w"), indent=2)


# -- Canoe discovery + download ------------------------------------------------
def _canoe_get(url: str, params: dict | None = None, timeout: int = 180) -> requests.Response:
    """GET the Canoe API, honouring Retry-After on 429/503 and retrying transient 5xx."""
    for attempt in range(1, 6):
        resp = requests.get(url, headers=canoe_auth.get_auth_headers(), params=params or {}, timeout=timeout)
        if resp.status_code == 200:
            return resp
        if resp.status_code in (429, 503):
            wait = int(resp.headers.get("Retry-After", 10 * attempt))
            time.sleep(max(1, wait))
            continue
        if resp.status_code in (500, 502, 504) and attempt < 5:
            time.sleep(5 * attempt)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"GET {url} failed after retries")


def discover(since_date: str | None) -> list[dict]:
    """All document metadata records in the window, de-duplicated by id."""
    params = {}
    if since_date:
        params["file_upload_time_start"] = since_date
    seen, out, page = set(), [], 1
    while page <= 1000:
        resp = _canoe_get(DATA_URL, {**params, "page": page, "limit": 100})
        body = resp.json()
        recs = body.get("data") if isinstance(body, dict) else body
        new = [r for r in (recs or []) if r.get("id") and r["id"] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r["id"])
        out.extend(new)
        page += 1
    return out


def download_bytes(doc_id: str) -> bytes:
    """GET /v1/documents/{id} -> file bytes (rate-limit aware via _canoe_get)."""
    return _canoe_get(f"{DOC_URL}/{doc_id}").content


def write_local(root: str, folder: str, filename: str, data: bytes) -> None:
    path = os.path.join(root, *folder.split("/"), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def target_path(rec: dict) -> tuple[str, str]:
    allocs = rec.get("allocations") or []
    a = allocs[0] if allocs else {}
    raw = (a.get("investment") or a.get("account") or a.get("entity") or "").strip()
    fund = _sanitize(raw) if (raw and raw != "--" and _sanitize(raw) != "unknown") else UNKNOWN_FUND
    data_date = a.get("data_date") or ""
    year = data_date[:4] if len(data_date) >= 4 and data_date[:4].isdigit() else "Undated"
    category = _sanitize(rec.get("category") or "Uncategorized")
    name = rec.get("name") or os.path.splitext(rec.get("original_file_name") or rec["id"])[0]
    ext = rec.get("file_type") or ".pdf"
    if not ext.startswith("."):
        ext = "." + ext
    filename = _sanitize(name) + ext
    return f"{fund}/{year}/{category}", filename


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync Canoe documents to SharePoint via Graph.")
    ap.add_argument("--full", action="store_true", help="Ignore the incremental window; consider all documents.")
    ap.add_argument("--dry-run", action="store_true", help="Discover and report, but download/upload nothing.")
    ap.add_argument("--local-dest", default=None,
                    help="Write files to this LOCAL folder instead of uploading to SharePoint "
                         "(e.g. ~/Desktop/Canoe). No Graph/certificate needed; uses its own manifest in the folder.")
    ap.add_argument("--since", default=None, help="Override the incremental window (ISO date, e.g. 2026-01-01).")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N documents (testing / controlled runs).")
    args = ap.parse_args()

    log_path = setup_logging(config.log_dir())
    run_start = datetime.now(timezone.utc)
    logging.info("=== canoe_sync start (log: %s) ===", log_path)

    local_dest = os.path.abspath(os.path.expanduser(args.local_dest)) if args.local_dest else None
    try:
        if local_dest:
            os.makedirs(local_dest, exist_ok=True)
            manifest = Manifest(os.path.join(local_dest, "_sync_manifest.json"))
            graph = None
            logging.info("LOCAL mode: writing to %s (no SharePoint upload)", local_dest)
        else:
            manifest = Manifest(config.manifest_path())
            graph = None if args.dry_run else GraphClient()
            if graph:
                logging.info("Graph access OK (drive %s)", graph.verify_access())
    except (config.ConfigError, GraphError) as exc:
        logging.error("Startup failed: %s", exc)
        sys.exit(1)

    if args.since:
        since_date = args.since[:10]
    elif args.full:
        since_date = None
    else:
        since = load_since()
        since_date = since[:10] if since else None
    logging.info("Discovery mode: %s", f"incremental since {since_date}" if since_date else "FULL")

    try:
        records = discover(since_date)
    except Exception as exc:
        logging.error("Discovery failed: %s", exc)
        sys.exit(1)
    logging.info("Documents fetched from Canoe: %d", len(records))
    if args.limit:
        records = records[:args.limit]
        logging.info("Limited to %d documents.", len(records))

    uploaded = skipped = errors = 0
    used = manifest.used_paths()   # collision-safe naming across runs
    for rec in records:
        doc_id = rec["id"]
        if manifest.has(doc_id):
            skipped += 1
            continue
        folder, filename = target_path(rec)
        # Distinct documents can share a name (e.g. three Blackstone fact sheets).
        # Disambiguate so none overwrites another in the library.
        full = f"{folder}/{filename}"
        if full in used:
            root, ext = os.path.splitext(filename)
            i = 2
            while f"{folder}/{root} ({i}){ext}" in used:
                i += 1
            filename = f"{root} ({i}){ext}"
            full = f"{folder}/{filename}"
        used.add(full)
        if args.dry_run:
            logging.info("[dry-run] would upload %s -> %s/%s", doc_id, folder, filename)
            uploaded += 1
            continue
        try:
            data = download_bytes(doc_id)
            if local_dest:
                write_local(local_dest, folder, filename, data)
            else:
                graph.upload(data, folder, filename)
            manifest.record(doc_id, f"{folder}/{filename}", datetime.now(timezone.utc).isoformat(), len(data))
            uploaded += 1
            logging.info("%s %s (%d bytes) -> %s/%s", "wrote" if local_dest else "uploaded",
                         doc_id, len(data), folder, filename)
        except Exception as exc:  # noqa: BLE001 -- log every failure with the doc id, keep going
            errors += 1
            logging.error("FAILED doc_id=%s (%s/%s): %s", doc_id, folder, filename, exc)

    # Advance the shared incremental marker only for a real Graph run (not local/dry).
    if not args.dry_run and not local_dest and errors == 0:
        save_since(run_start.isoformat())

    logging.info(
        "=== done: fetched=%d skipped=%d uploaded=%d errors=%d manifest_total=%d ===",
        len(records), skipped, uploaded, errors, manifest.count(),
    )
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
