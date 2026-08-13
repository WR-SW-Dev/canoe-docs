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
import csv
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

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


def append_run_record(record: dict) -> None:
    """Append one run's summary to the structured run-history log (JSON lines).

    This is the single source for the dashboard's run-history view, replacing the old
    hand-maintained run_history.csv. One line per real sync run; append-only so history
    accumulates. Never fatal -- a logging failure must not fail the sync itself.
    """
    try:
        p = config.runs_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 -- run-history is best-effort telemetry
        logging.warning("Could not write run-history record: %s", exc)


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


EARLIEST = date(2015, 1, 1)   # before the earliest document in Canoe
WINDOW_TIMEOUT = 90           # per-window probe timeout; on timeout we subdivide the window


def _fetch_window(start_iso: str, end_iso: str | None) -> list[dict]:
    """Page through one upload-time window. May raise requests.Timeout if the window is too big."""
    params = {"file_upload_time_start": start_iso}
    if end_iso:
        params["file_upload_time_end"] = end_iso
    seen, out, page = set(), [], 1
    while page <= 1000:
        resp = _canoe_get(DATA_URL, {**params, "page": page, "limit": 100}, timeout=WINDOW_TIMEOUT)
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


def _discover_range(start: date, end: date | None, seen: set) -> list[dict]:
    """Fetch [start, end); if the metadata endpoint times out (window too large), halve and recurse.

    Canoe's /v1/documents/data times out on any multi-year span, so a first-run full discovery
    must be chunked. Adaptive halving finds a workable granularity automatically regardless of
    how uploads are distributed over time.
    """
    end_eff = end or (datetime.now(timezone.utc).date() + timedelta(days=1))
    try:
        recs = _fetch_window(start.isoformat(), end.isoformat() if end else None)
    except requests.exceptions.Timeout:
        span = (end_eff - start).days
        if span <= 1:
            raise RuntimeError(f"Canoe metadata timed out even for a single day ({start}); cannot chunk further")
        mid = start + timedelta(days=max(1, span // 2))
        left = _discover_range(start, mid, seen)
        right = _discover_range(mid, end, seen)
        return left + right
    fresh = []
    for r in recs:
        rid = r.get("id")
        if rid and rid not in seen:
            seen.add(rid)
            fresh.append(r)
    return fresh


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def discover(since_date: str | None) -> list[dict]:
    """All document metadata records since `since_date` (or all, chunked monthly to avoid timeouts).

    /v1/documents/data times out on multi-year spans, so we walk month-by-month. Any single
    month that still times out is halved down to days by _discover_range.
    """
    start = date.fromisoformat(since_date[:10]) if since_date else EARLIEST
    today = datetime.now(timezone.utc).date()
    seen, out, cur = set(), [], start
    while cur <= today:
        nxt = _next_month(cur)
        end = None if nxt > today else nxt
        out.extend(_discover_range(cur, end, seen))
        cur = nxt
    logging.info("Discovery walked %s -> %s in monthly windows.", start.isoformat(), today.isoformat())
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
    ap.add_argument("--seed", action="store_true",
                    help="Build manifest.json + last_sync.json for ALL current Canoe docs (no download/upload). "
                         "Placing these on a fresh App Server makes the first sync skip docs already in SharePoint.")
    ap.add_argument("--seed-out", default=None, help="Directory for the seed files (default: the data dir).")
    ap.add_argument("--export", default=None,
                    help="Write a CSV inventory of what's ACTUALLY in the SharePoint library (live, via Graph), "
                         "each row annotated with the Canoe doc_id from the manifest. Requires Graph config.")
    args = ap.parse_args()

    log_path = setup_logging(config.log_dir())
    run_start = datetime.now(timezone.utc)
    logging.info("=== canoe_sync start (log: %s) ===", log_path)

    if args.seed:
        out = os.path.abspath(os.path.expanduser(args.seed_out)) if args.seed_out else config.data_dir()
        os.makedirs(out, exist_ok=True)
        logging.info("SEED mode: building manifest for ALL current Canoe documents -> %s", out)
        try:
            records = discover(None)
        except Exception as exc:
            logging.error("Seed discovery failed: %s", exc)
            sys.exit(1)
        logging.info("Documents discovered: %d", len(records))
        data, used = {}, set()
        for rec in records:
            folder, filename = target_path(rec)
            full = f"{folder}/{filename}"
            if full in used:
                root, ext = os.path.splitext(filename)
                i = 2
                while f"{folder}/{root} ({i}){ext}" in used:
                    i += 1
                full = f"{folder}/{root} ({i}){ext}"
            used.add(full)
            data[rec["id"]] = {"dest_path": full, "uploaded_at": "seed", "size": rec.get("file_size") or 0}
        json.dump(data, open(os.path.join(out, "manifest.json"), "w"), indent=2, sort_keys=True)
        json.dump({"last_run_iso": run_start.isoformat()}, open(os.path.join(out, "last_sync.json"), "w"), indent=2)
        logging.info("Seed written: %d entries -> %s/{manifest.json,last_sync.json}", len(data), out)
        sys.exit(0)

    if args.export:
        # Authoritative inventory: list the LIVE SharePoint library via Graph, not a local mirror,
        # and annotate each file with its Canoe doc_id from the manifest (matched by path).
        try:
            files = GraphClient().list_files()
        except (config.ConfigError, GraphError) as exc:
            logging.error("Export needs Graph config: %s", exc)
            sys.exit(1)
        id_by_path = Manifest(config.manifest_path()).doc_id_by_path()
        live_paths = {it["path"] for it in files}
        out = os.path.abspath(os.path.expanduser(args.export))
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["doc_id", "sharepoint_path", "size_bytes", "item_id", "in_manifest"])
            for it in sorted(files, key=lambda x: x["path"]):
                did = id_by_path.get(it["path"], "")
                w.writerow([did, it["path"], it["size"], it["item_id"], "yes" if did else "no"])
            # Manifest entries recorded as uploaded but not actually present in the library:
            for p in sorted(set(id_by_path) - live_paths):
                w.writerow([id_by_path[p], p, "", "", "MISSING_IN_SHAREPOINT"])
        logging.info("Export: %d live files (%d matched to a doc_id) -> %s",
                     len(files), sum(1 for it in files if it["path"] in id_by_path), out)
        sys.exit(0)

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

    run_end = datetime.now(timezone.utc)
    if args.dry_run:
        mode = "dry-run"
    elif local_dest:
        mode = "local"
    elif args.full or (args.since is None and since_date is None):
        mode = "full"
    else:
        mode = "incremental"
    # A real Graph run appends to the shared run-history log; local/dry runs do not touch
    # the SharePoint source of truth, so they are not recorded there.
    if not args.dry_run and not local_dest:
        append_run_record({
            "run_start": run_start.isoformat(),
            "run_end": run_end.isoformat(),
            "duration_sec": round((run_end - run_start).total_seconds(), 1),
            "mode": mode,
            "since": since_date,
            "fetched": len(records),
            "uploaded": uploaded,
            "skipped": skipped,
            "errors": errors,
            "manifest_total": manifest.count(),
            "exit_code": 1 if errors else 0,
            "log_file": os.path.basename(log_path),
        })

    logging.info(
        "=== done: fetched=%d skipped=%d uploaded=%d errors=%d manifest_total=%d ===",
        len(records), skipped, uploaded, errors, manifest.count(),
    )
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
