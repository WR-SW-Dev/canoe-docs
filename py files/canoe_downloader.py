#!/usr/bin/env python3
"""
canoe_downloader.py -- Document discovery and download from Canoe Intelligence.

Folder structure (entity-aware):
  archive_root/
    <Entity>/                # Or <Fund> -- whichever is the most useful top-level split
      <Year>/
        <Document Type>/
          <DocumentID>_<Date>.<ext>

Deduplication:
  .state/downloaded_ids.json tracks processed documents across runs.

Delta polling:
  Default behavior: only fetch documents uploaded since the last poll.
  First-run / backfill: use --backfill to override the date filter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_API_BASE = "https://api.canoesoftware.com"
DEFAULT_PAGE_LIMIT = 100
DEFAULT_ARCHIVE_ROOT = "archive"
STATE_DIR = ".state"

# ---------------------------------------------------------------------------
# Backfill helpers
# ---------------------------------------------------------------------------


def build_backfill_filters(since: str | None, backfill_years: int | None) -> dict[str, str]:
    """
    Build query filters for delta polling or backfill.

    Priority:
    1. If --since is given, use it directly as file_upload_time_start.
    2. If --backfill N is given, use N years ago from today as data_date_start.
       We do NOT use file_upload_time_start for backfill because that filter
       only goes back to the upload date, not the document's own data date.
    3. Otherwise no date filter (delta from last poll timestamp).
    """
    filters: dict[str, str] = {}
    if since:
        filters["file_upload_time_start"] = since
    elif backfill_years is not None:
        now = datetime.now(timezone.utc)
        start_year = now.year - backfill_years
        # Jan 1 of that year gives a clean multi-year range.
        filters["data_date_start"] = f"{start_year}-01-01"
    return filters


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


@dataclass
class PollState:
    """Persistent state for a Canoe poller."""
    last_poll_iso: str = ""
    downloaded_ids: list[str] = None

    def __post_init__(self):
        if self.downloaded_ids is None:
            self.downloaded_ids = []


def load_state(state_dir: Path) -> PollState:
    state_file = state_dir / "downloaded_ids.json"
    if state_file.exists():
        with open(state_file, "r") as f:
            data = json.load(f)
        return PollState(
            last_poll_iso=data.get("last_poll_iso", ""),
            downloaded_ids=data.get("downloaded_ids", []),
        )
    return PollState()


def save_state(state: PollState, state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "downloaded_ids.json"
    with open(state_file, "w") as f:
        json.dump(asdict(state), f, indent=2)


# ---------------------------------------------------------------------------
# Document listing
# ---------------------------------------------------------------------------


def list_documents_since(
    since_iso: str | None = None,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_LIMIT,
    extra_filters: dict[str, str] | None = None,
) -> tuple[list[dict], bool]:
    """
    Query Canoe for documents.
    Returns (documents, has_more).

    extra_filters: additional query params, e.g. {"fund_id": "..."}.
    """
    from canoe_auth import api_get

    params: dict[str, Any] = {
        "page": page,
        "limit": per_page,
    }
    if since_iso:
        params["file_upload_time_start"] = since_iso
    if extra_filters:
        params.update(extra_filters)

    data = api_get("/v1/documents", params=params)

    if isinstance(data, list):
        docs = data
        has_more = False
    else:
        docs = data.get("documents", data.get("data", []))
        has_more = len(docs) >= per_page

    return docs, has_more


def fetch_all_documents(
    since_iso: str | None = None,
    extra_filters: dict[str, str] | None = None,
) -> list[dict]:
    """Iterate all pages and return every document matching the filters."""
    all_docs: list[dict] = []
    page = 1
    while True:
        docs, has_more = list_documents_since(since_iso, page=page, extra_filters=extra_filters)
        all_docs.extend(docs)
        if not has_more or not docs:
            break
        page += 1
    return all_docs


# ---------------------------------------------------------------------------
# Document download
# ---------------------------------------------------------------------------


def download_document(doc_id: str) -> bytes:
    """Download raw file bytes for a single document by ID."""
    from canoe_auth import api_get_bytes
    return api_get_bytes(f"/v1/documents/{doc_id}")


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ---------------------------------------------------------------------------
# Path construction
# ---------------------------------------------------------------------------


def _safe_component(value: str) -> str:
    """Sanitize a string for use as a folder or file name component."""
    value = value.strip()
    for ch in ('/', '\\', ':', '*', '?', '"', '<', '>', '|'):
        value = value.replace(ch, "-")
    value = value.strip(".-")
    return value or "unknown"


def top_level_name(doc: dict, fallback: str = " Uncategorized") -> str:
    """
    Resolve the top-level folder name.

    Entity/fund are allocation-level fields in the Canoe API (see the
    `Document` schema in api-docs-v1.json -- they do not exist on the
    document object itself). A document can have multiple allocations;
    we use the first one as representative for the top-level folder.

    Preference order:
    1. Entity name (allocations[0].entity)
    2. Fund name (allocations[0].fund_name)
    3. client_document_id
    4. fallback
    """
    allocations = doc.get("allocations") or []
    first_allocation = allocations[0] if allocations else {}

    entity = first_allocation.get("entity")
    if entity:
        return _safe_component(entity)
    fund = first_allocation.get("fund_name")
    if fund:
        return _safe_component(fund)
    return _safe_component(doc.get("client_document_id") or fallback)


def doc_type_name(doc: dict) -> str:
    """document_type is a plain string per the Document schema, not an object."""
    document_type = doc.get("document_type", "")
    if isinstance(document_type, dict):
        document_type = document_type.get("name", "")
    return _safe_component(document_type or "Other")


def build_archive_path(archive_root: Path, doc: dict, ext: str) -> Path:
    """
    Build: archive_root / <Entity|Fund|Fallback> / <Year> / <Document Type> / <ID>_<Date>.<ext>
    """
    top = top_level_name(doc)
    doc_type = doc_type_name(doc)

    # data_date is allocation-level; uploaded is the document-level upload timestamp
    # (there is no top-level data_date or file_upload_time field on Document).
    allocations = doc.get("allocations") or []
    data_date = allocations[0].get("data_date") if allocations else None
    raw_date = data_date or doc.get("uploaded", "") or ""
    year = str(raw_date)[:4] if raw_date else str(datetime.now(timezone.utc).year)
    try:
        year_int = int(year)
        if year_int < 2000 or year_int > 2100:
            year = str(datetime.now(timezone.utc).year)
    except ValueError:
        year = str(datetime.now(timezone.utc).year)

    doc_id = _safe_component(str(doc.get("id", "unknown")))
    date_part = str(raw_date)[:10] if raw_date else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"{doc_id}_{date_part}{ext}"

    return archive_root / top / year / doc_type / filename


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_download(
    archive_root: Path,
    state: PollState,
    backfill_years: int | None = None,
    since: str | None = None,
    fund_id: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Download all new documents.
    Returns a summary dict.
    """
    archive_root = Path(archive_root).expanduser().resolve()
    archive_root.mkdir(parents=True, exist_ok=True)

    # Build the date filter
    filters = build_backfill_filters(since=since, backfill_years=backfill_years)
    if fund_id:
        filters["fund_id"] = fund_id

    since_iso = since or (state.last_poll_iso if not backfill_years else "")
    print(f"Polling Canoe for new documents...")
    if since_iso:
        print(f"  date filter   : since {since_iso}")
    if backfill_years is not None:
        print(f"  backfill mode : from {filters.get('data_date_start','?')} to present")
    if fund_id:
        print(f"  fund filter   : {fund_id}")

    docs = fetch_all_documents(since_iso=since_iso, extra_filters=filters)
    if limit is not None:
        docs = docs[:limit]

    print(f"Found {len(docs)} document(s) in API response.")

    downloaded = 0
    skipped = 0
    errors: list[str] = []
    existing = set(state.downloaded_ids)

    for doc in docs:
        doc_id = str(doc.get("id", "unknown"))
        if doc_id in existing:
            skipped += 1
            continue

        file_name = doc.get("original_file_name", f"{doc_id}.bin")
        ext = Path(file_name).suffix or ".pdf"

        try:
            raw = download_document(doc_id)
            dest = build_archive_path(archive_root, doc, ext)
            dest.parent.mkdir(parents=True, exist_ok=True)

            if dry_run:
                print(f"  [DRY RUN] Would save: {dest}")
            else:
                checksum = md5_bytes(raw)
                with open(dest, "wb") as f:
                    f.write(raw)
                print(f"  Saved: {dest}  (md5: {checksum[:12]})")
                existing.add(doc_id)
                state.downloaded_ids.append(doc_id)
                downloaded += 1
        except Exception as exc:
            errors.append(f"{doc_id}: {exc}")
            print(f"  ERROR downloading {doc_id}: {exc}", file=sys.stderr)

    state.last_poll_iso = datetime.now(timezone.utc).isoformat()

    summary = {
        "poll_ts": state.last_poll_iso,
        "total_checked": len(docs),
        "downloaded": downloaded,
        "skipped": skipped,
        "errors": len(errors),
        "error_details": errors,
    }
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Download new documents from Canoe Intelligence API.",
    )
    parser.add_argument(
        "--archive",
        default=str(Path.cwd() / DEFAULT_ARCHIVE_ROOT),
        help="Root folder for the archive (default: ./archive/)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ISO-8601 timestamp: only fetch documents uploaded after this point.",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=None,
        metavar="N",
        help="Backfill N years of history (uses data_date_start filter).",
    )
    parser.add_argument(
        "--fund-id",
        default=None,
        help="Limit to a single fund (pass Canoe fund_id value).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max documents to process in this run (for testing).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without writing files.",
    )
    parser.add_argument(
        "--state-dir",
        default=str(Path.cwd() / STATE_DIR),
        help="Directory for state files (default: ./.state/)",
    )
    args = parser.parse_args()

    state_dir = Path(args.state_dir)
    state = load_state(state_dir)

    if args.since:
        state.last_poll_iso = args.since

    summary = run_download(
        archive_root=Path(args.archive),
        state=state,
        backfill_years=args.backfill,
        since=args.since,
        fund_id=args.fund_id,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        save_state(state, state_dir)

    print("\n--- Poll Summary ---")
    for k, v in summary.items():
        if k != "error_details":
            print(f"  {k}: {v}")
    if summary["error_details"]:
        print(f"  Errors ({summary['errors']}):")
        for e in summary["error_details"]:
            print(f"    - {e}")


if __name__ == "__main__":
    main()
