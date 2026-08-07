#!/usr/bin/env python3
"""
canoe_bulk_download.py -- Download documents from Canoe into a foldered archive.

GET /v1/documents returns a ZIP bundle of the actual files, paged (up to `limit`
per page), already foldered by manager. This tool pages through them, extracts
each ZIP, and sub-organizes each manager folder by year and Canoe category.

Two modes:
  * Full pull      : no date filter -> every document.
  * Incremental    : --since <ISO|auto> adds file_upload_time_start so only
                     documents uploaded on/after that date are fetched. With
                     --since auto it reads/writes a last-run state file, so a
                     scheduled weekly run pulls just what arrived since last time.

Placement is content-aware (keyed by each file's CRC-32, which the ZIP stores):
distinct files that share a name get __2/__3 suffixes so none is lost; identical
duplicates are collapsed; files already on disk are left in place. Dedup is lazy
(only the colliding base path is scanned on disk), so incremental runs are fast.

Usage:
  # Full pull, organized Manager/Year/Category:
  python canoe_bulk_download.py --dest "/path/to/Private Fund Reporting" --organize year-category

  # Weekly incremental (only new since last run); safe to schedule:
  python canoe_bulk_download.py --dest "/path/to/Private Fund Reporting" \
      --organize year-category --since auto --state "/path/to/.canoe_last_run.json"
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import math
import os
import re
import time
import zipfile
import zlib
from datetime import datetime, timedelta, timezone

import requests

import canoe_auth

DOCS_URL = "https://api.canoesoftware.com/v1/documents"
TYPES_URL = "/v1/documents/types"
DEFAULT_LIMIT = 100
RATE_PAUSE_THRESHOLD = 3
RATE_PAUSE_SECONDS = 60
MAX_RETRIES = 4
DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")

_EXTRA_PARAMS: dict[str, str] = {}   # e.g. {"file_upload_time_start": "2026-08-01"}


# --------------------------------------------------------------------------- #
# Organization
# --------------------------------------------------------------------------- #

def _sanitize(component: str) -> str:
    for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
        component = component.replace(ch, "-")
    return component.strip().strip(".") or "Unfiled"


def load_type_pairs() -> list[tuple[str, str]]:
    types = canoe_auth.api_get(TYPES_URL)
    pairs = [(t["document_type"], t.get("category") or "Uncategorized")
             for t in types if isinstance(t, dict) and t.get("document_type")]
    return sorted(pairs, key=lambda x: -len(x[0]))


def classify(stem: str, pairs) -> tuple[str | None, str | None]:
    low = stem.lower()
    for dt, cat in pairs:
        if dt.lower() in low:
            return dt, cat
    return None, None


def parse_year(stem: str) -> str | None:
    m = DATE_RE.search(stem)
    if not m:
        return None
    y = m.group(3)
    return ("20" + y) if len(y) == 2 else y


def make_organizer(scheme: str, pairs):
    def rel_path(name: str) -> str:
        parts = [p for p in name.split("/") if p]
        manager = _sanitize(parts[0]) if len(parts) > 1 else "Unfiled"
        base = parts[-1]
        stem = os.path.splitext(base)[0]
        if scheme == "none":
            sub = []
        elif scheme == "category":
            sub = [classify(stem, pairs)[1] or "Uncategorized"]
        elif scheme == "type":
            sub = [classify(stem, pairs)[0] or "Uncategorized"]
        elif scheme == "year":
            sub = [parse_year(stem) or "Undated"]
        elif scheme == "category-year":
            _, cat = classify(stem, pairs)
            sub = [cat or "Uncategorized", parse_year(stem) or "Undated"]
        elif scheme == "year-category":
            _, cat = classify(stem, pairs)
            sub = [parse_year(stem) or "Undated", cat or "Uncategorized"]
        else:
            sub = []
        return os.path.join(manager, *[_sanitize(s) for s in sub], base)
    return rel_path


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_page(page: int, limit: int) -> requests.Response:
    params = {"page": page, "limit": limit, **_EXTRA_PARAMS}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(DOCS_URL, headers=canoe_auth.get_auth_headers(),
                                params=params, timeout=600)
        except requests.exceptions.RequestException as exc:
            wait = 15 * attempt
            print(f"    network error ({exc.__class__.__name__}); retry in {wait}s ({attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue
        if resp.status_code == 429:
            wait = RATE_PAUSE_SECONDS * attempt
            print(f"    rate limited (429); waiting {wait}s ({attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue
        if resp.status_code in (500, 502, 503, 504):
            wait = 10 * attempt
            print(f"    server error {resp.status_code}; retry in {wait}s ({attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Page {page} failed after {MAX_RETRIES} attempts.")


# --------------------------------------------------------------------------- #
# Content-aware placement (lazy dedup)
# --------------------------------------------------------------------------- #

def _crc_of(path: str) -> int:
    with open(path, "rb") as fh:
        return zlib.crc32(fh.read()) & 0xFFFFFFFF


def existing_variants(base_target: str) -> dict[int, str]:
    """Return {crc: path} for base_target and its __N siblings already on disk."""
    variants: dict[int, str] = {}
    root, ext = os.path.splitext(base_target)
    for c in [base_target] + sorted(glob.glob(glob.escape(root) + "__*" + ext)):
        if os.path.exists(c) and os.path.getsize(c) > 0:
            try:
                variants[_crc_of(c)] = c
            except OSError:
                pass
    return variants


def safe_extract(zf: zipfile.ZipFile, dest: str, organizer, seen: dict, written_paths: list) -> tuple[int, int, int]:
    """Content-aware extract. Returns (written, existed, deduped); appends new relpaths to written_paths."""
    dest_abs = os.path.abspath(dest)
    written = existed = deduped = 0
    for member in zf.infolist():
        name = member.filename
        if name.endswith("/"):
            continue
        base_target = os.path.abspath(os.path.join(dest_abs, organizer(name)))
        if base_target != dest_abs and not base_target.startswith(dest_abs + os.sep):
            print(f"    SKIP unsafe path in zip: {name}")
            continue
        if base_target not in seen:
            seen[base_target] = existing_variants(base_target)   # lazy: only this path
        slot = seen[base_target]
        if member.CRC in slot:
            deduped += 1
            continue
        root, ext = os.path.splitext(base_target)
        used = set(slot.values())
        i = 1
        while True:
            cand = base_target if i == 1 else f"{root}__{i}{ext}"
            if cand not in used and not (os.path.exists(cand) and os.path.getsize(cand) > 0):
                break
            i += 1
        slot[member.CRC] = cand
        os.makedirs(os.path.dirname(cand), exist_ok=True)
        with zf.open(member) as src, open(cand, "wb") as out:
            out.write(src.read())
        written += 1
        written_paths.append(os.path.relpath(cand, dest_abs))
    return written, existed, deduped


# --------------------------------------------------------------------------- #
# Incremental state
# --------------------------------------------------------------------------- #

def resolve_since(args) -> str | None:
    """Return a YYYY-MM-DD to pass as file_upload_time_start, or None for a full pull."""
    if args.since and args.since != "auto":
        return args.since[:10]
    if args.since == "auto":
        last = None
        if args.state and os.path.exists(args.state):
            try:
                last = json.load(open(args.state)).get("last_run_iso")
            except (OSError, ValueError):
                last = None
        if last:
            return last[:10]
        fallback = datetime.now(timezone.utc) - timedelta(days=args.lookback_days)
        return fallback.strftime("%Y-%m-%d")
    return None


def default_run_log(args) -> str:
    if args.run_log:
        return args.run_log
    sd = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(sd), "run_history.csv")


def append_run_log(path: str, run_utc: str, mode: str, docs: int, new: int, dup: int, elapsed_min: float) -> None:
    """Timestamps + counts only -- no file names, safe to keep in Git."""
    try:
        is_new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            wr = csv.writer(f)
            if is_new:
                wr.writerow(["run_utc", "mode", "documents_seen", "new_files", "duplicates", "elapsed_min"])
            wr.writerow([run_utc, mode, docs, new, dup, elapsed_min])
    except OSError as exc:
        print(f"  (could not write run history: {exc})")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Download Canoe documents into a foldered archive.")
    ap.add_argument("--dest", required=True)
    ap.add_argument("--organize", default="year-category",
                    choices=["none", "category", "type", "year", "category-year", "year-category"])
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--since", default=None,
                    help="ISO date, or 'auto' to use the last-run state file (incremental).")
    ap.add_argument("--state", default=None, help="Last-run state JSON (used with --since auto).")
    ap.add_argument("--lookback-days", type=int, default=8,
                    help="First incremental run with no state looks back this many days.")
    ap.add_argument("--activity-log", default=None,
                    help="Per-file log WITH names, kept beside the archive (default: <dest>/_download_activity.csv). Never commit to Git.")
    ap.add_argument("--run-log", default=None,
                    help="Run-history log: timestamps + counts only, NO file names (default: <repo>/run_history.csv). Safe for Git.")
    args = ap.parse_args()

    dest = os.path.abspath(os.path.expanduser(args.dest))
    os.makedirs(dest, exist_ok=True)
    run_start = datetime.now(timezone.utc)

    since = resolve_since(args)
    if since:
        _EXTRA_PARAMS["file_upload_time_start"] = since

    print(f"Destination : {dest}")
    print(f"Organize by : {args.organize}")
    print(f"Mode        : {'incremental since ' + since if since else 'FULL pull'}")

    pairs = []
    if args.organize in ("category", "type", "category-year", "year-category"):
        pairs = load_type_pairs()
    organizer = make_organizer(args.organize, pairs)
    seen: dict = {}

    resp = fetch_page(args.start_page, args.limit)
    total = int(resp.headers.get("total", 0))
    total_pages = int(resp.headers.get("total_pages", 0)) or math.ceil(total / args.limit) or 1
    last_page = total_pages
    if args.max_pages is not None:
        last_page = min(total_pages, args.start_page + args.max_pages - 1)
    print(f"Canoe reports {total} document(s) across {total_pages} page(s).")
    if total == 0:
        print("Nothing to fetch.")
        append_run_log(default_run_log(args), run_start.isoformat(),
                       f"incremental since {since}" if since else "full", 0, 0, 0, 0.0)
        if args.since == "auto" and args.state:
            json.dump({"last_run_iso": run_start.isoformat()}, open(args.state, "w"), indent=2)
        return
    print(f"Downloading pages {args.start_page}..{last_page}.\n")

    tw = te = td = docs = 0
    written_paths: list = []
    t0 = time.time()
    page, current = args.start_page, resp
    while page <= last_page:
        try:
            zf = zipfile.ZipFile(io.BytesIO(current.content))
        except zipfile.BadZipFile:
            print(f"  Page {page}: not a valid ZIP ({len(current.content)} bytes) -- stopping.")
            break
        n = len([m for m in zf.namelist() if not m.endswith("/")])
        if n == 0:
            print(f"  Page {page}: empty -- done.")
            break
        w, e, d = safe_extract(zf, dest, organizer, seen, written_paths)
        tw += w; te += e; td += d; docs += n
        mb = len(current.content) / 1048576
        print(f"  Page {page}/{last_page}: {n} docs, {mb:.1f} MB (new {w}, dup {d}) | {docs} seen, {(time.time()-t0)/60:.1f} min")
        rem = current.headers.get("X-RateLimit-Remaining")
        if rem is not None and int(rem) <= RATE_PAUSE_THRESHOLD:
            print(f"    near rate limit ({rem}); pausing {RATE_PAUSE_SECONDS}s..."); time.sleep(RATE_PAUSE_SECONDS)
        page += 1
        if page > last_page:
            break
        current = fetch_page(page, args.limit)

    mode = f"incremental since {since}" if since else "full"

    # Detailed activity log (WITH file names) -- lives beside the archive (SharePoint), NEVER in Git.
    if written_paths:
        alog = args.activity_log or os.path.join(dest, "_download_activity.csv")
        try:
            is_new = not os.path.exists(alog)
            with open(alog, "a", newline="") as f:
                wr = csv.writer(f)
                if is_new:
                    wr.writerow(["run_utc", "mode", "relpath", "manager", "year", "category"])
                for rp in written_paths:
                    parts = rp.split(os.sep)
                    wr.writerow([run_start.isoformat(), mode, rp,
                                 parts[0] if len(parts) > 0 else "",
                                 parts[1] if len(parts) > 1 else "",
                                 parts[2] if len(parts) > 2 else ""])
            print(f"  activity log      : {alog}")
        except OSError as exc:
            print(f"  (could not write activity log: {exc})")

    # Run-history log (timestamps + counts only, NO file names) -- safe to keep in Git.
    run_log = default_run_log(args)
    append_run_log(run_log, run_start.isoformat(), mode, docs, tw, td, round((time.time() - t0) / 60, 2))
    print(f"  run history       : {run_log}")

    # Only advance the state after a clean pass, so a crash never skips documents.
    if args.since == "auto" and args.state:
        json.dump({"last_run_iso": run_start.isoformat()}, open(args.state, "w"), indent=2)

    print("\n--- Done ---")
    print(f"  documents seen    : {docs}")
    print(f"  new files written : {tw}")
    print(f"  duplicates        : {td}")
    print(f"  elapsed           : {(time.time()-t0)/60:.1f} min")
    print(f"  destination       : {dest}")
    if since:
        print(f"  (incremental since {since})")


if __name__ == "__main__":
    main()
