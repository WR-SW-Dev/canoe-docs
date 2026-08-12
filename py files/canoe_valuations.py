#!/usr/bin/env python3
"""
canoe_valuations.py -- Canoe valuation feed, Phase 1 (read-only, standalone)
============================================================================
Pulls Canoe's *extracted* statement NAVs and turns them into two vetted tables:

  canoe_valuations.csv            -- accepted NAV-by-investment x entity x period
  canoe_validation_exceptions.csv -- everything quarantined, with the reason

This is the valuation analog of the statement tracker: same Canoe auth and the
same `GET /v1/documents/data` pull quirks, but it keeps the allocation *values*
(the tracker's _slim throws them away). It reads only Canoe's API-extracted
figures -- no local PDF parsing, no GenAI.

Framing risk (Jason): "errant tags and incorrect numbers pulled from docs." So
every NAV must clear the validation gates below or it lands on the exceptions
list -- never silently trusted. Three-way Canoe|Archway|Addepar cross-checking
is Phase 2 (in the recon); Phase 1 proves extraction quality on real data.

FIRST RUN -- confirm the NAV field name on a machine that has Canoe creds:
    python3 canoe_valuations.py --probe
It dumps the raw allocation keys + a sample so we can pin the NAV field. The
pipeline auto-detects it from NAV_FIELD_CANDIDATES meanwhile.

    python3 canoe_valuations.py            # full pull -> the two CSVs
    python3 canoe_valuations.py --refresh full
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict

import requests
import canoe_auth   # reused: OAuth + get_auth_headers()

# --- constants (mirror statement_tracker.py conventions) ------------------- #
DATA_URL = "https://api.canoesoftware.com/v1/documents/data"
PAGE_LIMIT = 100
MAX_RETRIES = 5
STATEMENT_TYPE_NAMES = [
    "Account Statement", "Capital Account Statement", "Monthly Report",
    "Quarterly Report", "Annual Report", "Financials",
]
# Statuses a person must review before the figure can be trusted.
REVIEW_STATUSES = {"awaiting confirmation", "anomaly detected", "potential discrepancy"}
# Prefer a capital account statement's NAV over a report's, etc.
TYPE_PRIORITY = {
    "capital account statement": 0, "account statement": 1, "monthly report": 2,
    "quarterly report": 3, "annual report": 4, "financials": 5,
}
# Canoe's NAV/ending-capital field name varies by tenant; take the first present.
# Confirm the real one with --probe and move it to the front.
NAV_FIELD_CANDIDATES = [
    "reporting_value", "ending_capital_balance", "ending_capital", "market_value",
    "ending_market_value", "nav", "net_asset_value", "current_value",
    "ending_value", "capital_balance", "value",
]
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_valuations")


# --------------------------------------------------------------------------- #
# Pull (defensive pagination; NEVER pass `fields=` -> it empties allocations)
# --------------------------------------------------------------------------- #
def _fetch_page(page: int, extra: dict) -> requests.Response:
    params = {"page": page, "limit": PAGE_LIMIT, **extra}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(DATA_URL, headers=canoe_auth.get_auth_headers(),
                                params=params, timeout=600)
        except requests.exceptions.RequestException as exc:
            time.sleep(15 * attempt)
            if attempt == MAX_RETRIES:
                raise
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(30 * attempt)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"page {page} failed after {MAX_RETRIES} attempts")


def pull_docs(max_pages: int = 200) -> list[dict]:
    """All statement-type docs, full allocations kept. Dedupe by id."""
    types_param = ",".join(sorted(STATEMENT_TYPE_NAMES))
    by_id: dict[str, dict] = {}
    page = 1
    while page <= max_pages:
        resp = _fetch_page(page, {"document_type": types_param})
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        before = len(by_id)
        for d in batch:
            if d.get("id"):
                by_id[d["id"]] = d
        tp = resp.headers.get("total_pages")
        print(f"  page {page}{'/'+tp if tp else ''}: total {len(by_id)}")
        if len(by_id) == before or (tp and page >= int(tp)):
            break
        page += 1
    return list(by_id.values())


def _flatten_allocations(doc: dict) -> list[dict]:
    raw = doc.get("allocations") or []
    flat = []
    for a in raw:
        if isinstance(a, list):
            flat.extend(x for x in a if isinstance(x, dict))
        elif isinstance(a, dict):
            flat.append(a)
    return flat


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def detect_nav(alloc: dict):
    """(field_name, float value) using the first present candidate, or (None, None)."""
    for k in NAV_FIELD_CANDIDATES:
        if k in alloc and alloc[k] not in (None, ""):
            try:
                return k, float(str(alloc[k]).replace(",", "").replace("$", ""))
            except (TypeError, ValueError):
                continue
    return None, None


def parse_date(s):
    if not s:
        return None
    s = str(s).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def period_key(d: dt.date) -> str:
    return d.strftime("%Y-%m") if d else ""


# --------------------------------------------------------------------------- #
# Validation pipeline (the crux)
# --------------------------------------------------------------------------- #
def build_valuations(docs: list[dict], jump_pct: float = 0.5):
    """Return (accepted rows, exception rows). Pure function -- unit-testable
    with synthetic docs, no network."""
    accepted, exceptions = [], []

    def exc(reason, doc, alloc, nav=None, period=None):
        exceptions.append({
            "reason": reason, "doc_id": doc.get("id"), "doc_name": doc.get("name"),
            "document_type": doc.get("document_type"),
            "document_status": doc.get("document_status"),
            "investment": (alloc or {}).get("investment"),
            "investment_id": (alloc or {}).get("investment_id"),
            "entity": (alloc or {}).get("entity"),
            "period": period, "nav": nav,
        })

    candidates = []   # (investment_id, entity, period) -> list of candidate rows
    grouped = defaultdict(list)
    for doc in docs:
        status = (doc.get("document_status") or "").strip().lower()
        allocs = _flatten_allocations(doc)
        # tag gate: a doc whose allocations span >1 investment is consolidated
        # (custodian/Merrill) -- manager statements map to exactly one.
        span = {a.get("investment_id") for a in allocs if a.get("investment_id")}
        for a in allocs:
            nav_field, nav = detect_nav(a)
            as_of = parse_date(a.get("data_date"))
            per = period_key(as_of)
            inv_id = a.get("investment_id")
            entity = (a.get("entity") or "").strip()
            # ---- gates ----
            if status in REVIEW_STATUSES:
                exc(f"review status: {status}", doc, a, nav, per); continue
            if len(span) > 1:
                exc("allocations span >1 investment (consolidated/custodian)", doc, a, nav, per); continue
            if not inv_id:
                exc("missing investment_id (untagged)", doc, a, nav, per); continue
            if not entity or entity == "--":
                exc("missing/`--` entity tag", doc, a, nav, per); continue
            if as_of is None:
                exc("unparseable data_date", doc, a, nav, per); continue
            if nav is None:
                exc("no NAV field found in allocation", doc, a, nav, per); continue
            if nav < 0:
                exc("negative NAV", doc, a, nav, per); continue
            grouped[(inv_id, entity, per)].append({
                "investment": a.get("investment"), "investment_id": inv_id,
                "entity": entity, "period": per, "as_of": as_of.isoformat(),
                "nav": nav, "nav_field": nav_field,
                "document_type": doc.get("document_type"),
                "doc_id": doc.get("id"), "doc_name": doc.get("name"),
                "uploaded": doc.get("uploaded") or "",
            })

    # dedupe per (investment, entity, period): best statement type, then latest upload
    def rank(r):
        return (TYPE_PRIORITY.get((r["document_type"] or "").lower(), 9),
                _neg_upload(r["uploaded"]))
    for key, rows in grouped.items():
        rows.sort(key=rank)
        winner = rows[0]
        accepted.append(winner)
        for loser in rows[1:]:
            exceptions.append({**{k: loser.get(k) for k in
                               ("doc_id","doc_name","document_type","investment",
                                "investment_id","entity","period","nav")},
                               "reason": "duplicate for period (superseded by higher-priority statement)"})

    # numeric sanity: flag implausible period-over-period jumps (possible OCR slip)
    by_series = defaultdict(list)
    for r in accepted:
        by_series[(r["investment_id"], r["entity"])].append(r)
    flagged_ids = set()
    for series in by_series.values():
        series.sort(key=lambda r: r["period"])
        for prev, cur in zip(series, series[1:]):
            if prev["nav"] and cur["nav"]:
                jump = abs(cur["nav"] / prev["nav"] - 1)
                if jump > jump_pct:
                    flagged_ids.add(id(cur))
                    exceptions.append({
                        "reason": f"NAV jump {jump*100:.0f}% vs prior period (review: possible OCR/decimal error)",
                        "doc_id": cur["doc_id"], "doc_name": cur["doc_name"],
                        "document_type": cur["document_type"], "investment": cur["investment"],
                        "investment_id": cur["investment_id"], "entity": cur["entity"],
                        "period": cur["period"], "nav": cur["nav"],
                    })
    accepted = [r for r in accepted if id(r) not in flagged_ids]
    return accepted, exceptions


def _neg_upload(s):
    """Sort key so the latest upload wins (ties after type priority)."""
    d = parse_date(s)
    return -(d.toordinal()) if d else 0


# --------------------------------------------------------------------------- #
# Probe + main
# --------------------------------------------------------------------------- #
def probe(n_docs: int = 5):
    """Dump raw allocation field names + a sample so the NAV field can be pinned."""
    print("Probing Canoe /v1/documents/data (read-only)...")
    resp = _fetch_page(1, {"document_type": ",".join(sorted(STATEMENT_TYPE_NAMES))})
    docs = resp.json()[:n_docs]
    keys = set()
    sample = None
    for d in docs:
        for a in _flatten_allocations(d):
            keys |= set(a.keys())
            if sample is None and detect_nav(a)[0]:
                sample = a
    print("\nUnion of allocation field names:\n  " + "\n  ".join(sorted(keys)))
    nav_present = [k for k in NAV_FIELD_CANDIDATES if k in keys]
    print(f"\nNAV candidates present: {nav_present or 'NONE — inspect the keys above and add the right one to NAV_FIELD_CANDIDATES'}")
    if sample:
        print("\nSample allocation (NAV auto-detected as "
              f"'{detect_nav(sample)[0]}'={detect_nav(sample)[1]}):")
        print(json.dumps({k: sample.get(k) for k in
              ("investment", "investment_id", "entity", "data_date", detect_nav(sample)[0])}, indent=2))


def write_csv(path, rows, cols):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="dump allocation fields to pin the NAV key")
    ap.add_argument("--jump-pct", type=float, default=0.5, help="period-over-period NAV jump band (0.5=50%%)")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    if args.probe:
        probe()
        return 0

    print("Pulling Canoe statement docs (read-only)...")
    docs = pull_docs()
    print(f"{len(docs)} docs. Building + validating valuations...")
    accepted, exceptions = build_valuations(docs, jump_pct=args.jump_pct)

    acc_cols = ["investment", "investment_id", "entity", "period", "as_of", "nav",
                "nav_field", "document_type", "doc_id", "doc_name"]
    exc_cols = ["reason", "investment", "investment_id", "entity", "period", "nav",
                "document_type", "document_status", "doc_id", "doc_name"]
    write_csv(os.path.join(args.out, "canoe_valuations.csv"), accepted, acc_cols)
    write_csv(os.path.join(args.out, "canoe_validation_exceptions.csv"), exceptions, exc_cols)

    from collections import Counter
    print(f"\nAccepted NAVs: {len(accepted)}  |  Exceptions: {len(exceptions)}")
    print("Exception reasons:", dict(Counter(e["reason"].split(" (")[0].split(":")[0]
                                              for e in exceptions).most_common()))
    print("Output:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
