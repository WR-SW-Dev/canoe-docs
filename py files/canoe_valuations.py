#!/usr/bin/env python3
"""
canoe_valuations.py -- Canoe valuation feed, Phase 1 (read-only, standalone)
============================================================================
Pulls Canoe's *extracted & validated* statement figures and turns them into two
vetted tables:

  canoe_valuations.csv            -- accepted NAV per investment x entity x period
  canoe_validation_exceptions.csv -- everything quarantined, with the reason

WHERE THE DATA LIVES (learned from the API + live probing, 2026-08-13):
  * The NAV is `validated_data.endingBalance` on **Account Statement** documents
    (Capital Activity). It is present for BOTH hedge_fund and drawdown_fund (PE)
    statements -- `endingBalance` was in all 163/163 allocations sampled.
  * `validated_data` is EMPTY unless you request it: use
    `fields=allocation_id,validated_data` + `sum_values=true`.
  * The Addepar/Archway crosswalk is already inside Canoe: the allocation carries
    `addepar_owner_id`, `addepar_owned_id`, `archway_identifier` -- so we join by
    ID, not by name. (~98% populated; the null tail falls back to archway_identifier
    / entity name.)
  * QUIRK: a long `fields` list silently truncates `validated_data` to just
    `entity`. So we fetch figures and IDs in TWO calls per window and join on
    `allocation_id`. And `/v1/documents/data` ignores `limit` and times out on
    multi-year spans -> we window by month (like canoe_downloader).

No PDF parsing, no GenAI -- this reads Canoe's own validated API data.

    python3 canoe_valuations.py --start 2024-01 --end 2024-06
    python3 canoe_valuations.py --probe            # dump validated_data field names
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict

import requests
import canoe_auth

BASE = "https://api.canoesoftware.com"
DATA_URL = BASE + "/v1/documents/data"
DOC_TYPE = "Account Statement"          # the type carrying validated_data figures
MAX_RETRIES = 5
# NAV field inside validated_data. endingBalance is universal; the rest are
# safety fallbacks for templates that ever omit it.
NAV_FIELDS = ["endingBalance", "endingBalanceQTD", "endingBalanceYTD", "netAssetValue"]
# Extra figures worth carrying (cash-flow cross-checks / context).
EXTRA_FIELDS = ["paidInCapital", "contribution", "distribution", "cumulativeDistribution",
                "commitment", "unfundedCommitment", "navPerShare", "irr", "moic"]
REVIEW_STATUSES = {"awaiting confirmation", "anomaly detected", "potential discrepancy"}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_valuations")


# --------------------------------------------------------------------------- #
# Pull
# --------------------------------------------------------------------------- #
def _get(params):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(DATA_URL, headers=canoe_auth.get_auth_headers(),
                             params=params, timeout=120)
        except requests.exceptions.RequestException:
            time.sleep(10 * attempt)
            if attempt == MAX_RETRIES:
                raise
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(20 * attempt)
            continue
        r.raise_for_status()
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem is not None and int(rem) <= 3:
            time.sleep(30)
        return r.json()
    raise RuntimeError("request failed after retries")


def _allocs(docs, extra_doc_fields=()):
    """Flatten allocations -> {allocation_id: allocation-dict (+ carried doc fields)}."""
    out = {}
    for d in docs:
        carried = {f: d.get(f) for f in extra_doc_fields}
        for a in (d.get("allocations") or []):
            for x in ([a] if isinstance(a, dict) else a):
                if isinstance(x, dict) and x.get("allocation_id"):
                    x = {**x, **carried}
                    out[x["allocation_id"]] = x
    return out


def pull_window(date_start, date_end):
    """Two calls (figures + ids) for a data_date window, joined on allocation_id.
    Returns a list of merged allocation dicts."""
    common = {"document_type": DOC_TYPE, "data_date_start": date_start,
              "data_date_end": date_end, "sum_values": "true"}
    figures = _allocs(_get({**common, "fields": "allocation_id,validated_data"}))
    ids = _allocs(_get({**common, "fields":
              "allocation_id,investment,investment_id,investment_structure,"
              "addepar_owner_id,addepar_cash_owner_id,addepar_owned_id,"
              "archway_identifier,document_status,name"}),
              extra_doc_fields=())
    merged = []
    for aid, fig in figures.items():
        idrec = ids.get(aid, {})
        merged.append({**idrec, "allocation_id": aid,
                       "validated_data": fig.get("validated_data") or {}})
    return merged


def month_windows(start_ym, end_ym):
    y, m = map(int, start_ym.split("-"))
    ey, em = map(int, end_ym.split("-"))
    while (y, m) <= (ey, em):
        last = calendar.monthrange(y, m)[1]
        yield f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"
        m += 1
        if m > 12:
            m, y = 1, y + 1


# --------------------------------------------------------------------------- #
# Extract + validate
# --------------------------------------------------------------------------- #
def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def detect_nav(vd):
    for k in NAV_FIELDS:
        v = _num(vd.get(k))
        if v is not None:
            return k, v
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


def build_valuations(allocs, jump_pct=0.5):
    """Pure function: merged allocation dicts -> (accepted, exceptions)."""
    accepted, exceptions = [], []

    def exc(reason, a, nav=None, per=None):
        vd = a.get("validated_data") or {}
        exceptions.append({
            "reason": reason, "allocation_id": a.get("allocation_id"),
            "fund": vd.get("fundName") or a.get("investment"),
            "entity": vd.get("entity"),
            "addepar_owned_id": a.get("addepar_owned_id"),
            "addepar_owner_id": a.get("addepar_owner_id"),
            "archway_identifier": a.get("archway_identifier"),
            "period": per, "nav": nav, "document_status": a.get("document_status"),
        })

    grouped = defaultdict(list)
    for a in allocs:
        vd = a.get("validated_data") or {}
        status = (a.get("document_status") or "").strip().lower()
        nav_field, nav = detect_nav(vd)
        as_of = parse_date(vd.get("endingDate") or vd.get("reportDate"))
        per = as_of.isoformat() if as_of else None
        entity = (vd.get("entity") or "").strip()
        owned = a.get("addepar_owned_id")
        owner = a.get("addepar_owner_id")
        arch = a.get("archway_identifier")

        if status in REVIEW_STATUSES:
            exc(f"review status: {status}", a, nav, per); continue
        if as_of is None:
            exc("no endingDate in validated_data", a, nav, per); continue
        if nav is None:
            exc("no NAV (endingBalance) in validated_data", a, nav, per); continue
        if not entity:
            exc("no entity in validated_data", a, nav, per); continue
        # crosswalk: need at least one durable link to Addepar/Archway
        if not (owner or owned or arch):
            exc("no Addepar/Archway crosswalk id on allocation", a, nav, per); continue
        if nav < 0:
            exc("negative endingBalance (review)", a, nav, per); continue

        rec = {
            "fund": vd.get("fundName") or a.get("investment"),
            "entity": entity,
            "period": per, "as_of": per, "nav": nav, "nav_field": nav_field,
            "addepar_owned_id": owned, "addepar_owner_id": owner,
            "archway_identifier": arch,
            "investment_structure": a.get("investment_structure"),
            "currency": vd.get("currency_code") or vd.get("currency"),
            "allocation_id": a.get("allocation_id"),
            "doc_name": a.get("name"),
        }
        for f in EXTRA_FIELDS:
            if f in vd:
                rec[f] = _num(vd.get(f))
        # dedupe key: the durable identity of this LP position at this period-end
        key = (owned or a.get("investment_id") or rec["fund"], owner or entity, per)
        grouped[key].append(rec)

    # one winner per (investment, entity, period): richest validated_data wins
    for key, rows in grouped.items():
        rows.sort(key=lambda r: -sum(1 for f in EXTRA_FIELDS if r.get(f) is not None))
        accepted.append(rows[0])
        for extra in rows[1:]:
            exceptions.append({**{k: extra.get(k) for k in
                              ("allocation_id", "fund", "entity", "period", "nav",
                               "addepar_owned_id", "addepar_owner_id")},
                              "reason": "duplicate for period (kept the richer statement)"})

    # numeric sanity: implausible period-over-period NAV jump (OCR/decimal guard)
    by_series = defaultdict(list)
    for r in accepted:
        by_series[(r["addepar_owned_id"] or r["fund"], r["addepar_owner_id"] or r["entity"])].append(r)
    flagged = set()
    for series in by_series.values():
        series.sort(key=lambda r: r["period"])
        for prev, cur in zip(series, series[1:]):
            if prev["nav"] and cur["nav"] and abs(cur["nav"] / prev["nav"] - 1) > jump_pct:
                flagged.add(id(cur))
                exceptions.append({**{k: cur.get(k) for k in
                                  ("allocation_id", "fund", "entity", "period", "nav",
                                   "addepar_owned_id", "addepar_owner_id")},
                                  "reason": f"NAV jump {abs(cur['nav']/prev['nav']-1)*100:.0f}% vs prior (review)"})
    accepted = [r for r in accepted if id(r) not in flagged]
    return accepted, exceptions


# --------------------------------------------------------------------------- #
# probe / main
# --------------------------------------------------------------------------- #
def probe():
    print("Probing a recent quarter of Account Statement validated_data...")
    allocs = pull_window("2024-03-30", "2024-03-31")
    from collections import Counter
    navf = Counter()
    ids = Counter()
    for a in allocs:
        vd = a.get("validated_data") or {}
        f, _ = detect_nav(vd)
        navf[f] += 1
        ids["owner_id" if a.get("addepar_owner_id") else "owner_id_NULL"] += 1
        ids["owned_id" if a.get("addepar_owned_id") else "owned_id_NULL"] += 1
    print(f"allocations: {len(allocs)}")
    print("NAV field detected:", dict(navf))
    print("crosswalk id population:", dict(ids))
    for a in allocs[:2]:
        vd = a.get("validated_data") or {}
        print("  sample:", {"fund": vd.get("fundName"), "entity": vd.get("entity"),
              "endingBalance": vd.get("endingBalance"), "endingDate": vd.get("endingDate"),
              "addepar_owner_id": a.get("addepar_owner_id"),
              "addepar_owned_id": a.get("addepar_owned_id")})


def write_csv(path, rows, cols):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="first month YYYY-MM")
    ap.add_argument("--end", help="last month YYYY-MM")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--jump-pct", type=float, default=0.5)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    if args.probe:
        probe()
        return 0
    if not (args.start and args.end):
        print("Provide --start YYYY-MM --end YYYY-MM (or --probe).")
        return 2

    all_allocs = []
    for ds, de in month_windows(args.start, args.end):
        w = pull_window(ds, de)
        if w:
            print(f"  {ds[:7]}: {len(w)} statement allocations")
        all_allocs.extend(w)
    print(f"\nPulled {len(all_allocs)} allocations. Validating...")
    accepted, exceptions = build_valuations(all_allocs, jump_pct=args.jump_pct)

    acc_cols = ["fund", "entity", "period", "nav", "nav_field", "addepar_owned_id",
                "addepar_owner_id", "archway_identifier", "investment_structure",
                "currency"] + EXTRA_FIELDS + ["allocation_id", "doc_name"]
    exc_cols = ["reason", "fund", "entity", "period", "nav", "addepar_owned_id",
                "addepar_owner_id", "archway_identifier", "document_status", "allocation_id"]
    write_csv(os.path.join(args.out, "canoe_valuations.csv"), accepted, acc_cols)
    write_csv(os.path.join(args.out, "canoe_validation_exceptions.csv"), exceptions, exc_cols)
    from collections import Counter
    print(f"\nAccepted NAVs: {len(accepted)} | Exceptions: {len(exceptions)}")
    print("Exception reasons:", dict(Counter(e["reason"].split(" (")[0].split(":")[0]
                                              for e in exceptions).most_common()))
    print("Output:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
