#!/usr/bin/env python3
"""
canoe_reclassify.py -- Deterministic (no-AI) cleanup for Undated / Unknown documents.

Reads each PDF's text LOCALLY (PyMuPDF) and tries to recover:
  * the year   -- for files sitting in an `Undated` folder
  * the fund   -- for files sitting under `Unknown Investment`
then refiles / renames the HIGH-CONFIDENCE ones and lists everything else for a
human to clear. It never sends document text anywhere and never deletes a file.

Confidence rules (deliberately conservative -- we would rather flag than mis-file):
  * year  : HIGH only when a period-end date (month/quarter end) is found in the text.
  * fund  : HIGH only when exactly ONE known fund matches the text (full name or a
            distinctive token). Multiple matches -> ambiguous -> flagged, not moved.

Compliance: this is rules-based text processing, not GenAI -- no approval needed, and
no document content leaves the machine or appears in the report (metadata only).

Usage:
  # Dry run (default): writes a review CSV, moves nothing.
  python canoe_reclassify.py

  # Apply the high-confidence moves (writes an undo log):
  python canoe_reclassify.py --apply

  # Undo a previous --apply run:
  python canoe_reclassify.py --undo _reclassify_moves_<timestamp>.json
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
import re

import fitz  # PyMuPDF

fitz.TOOLS.mupdf_display_errors(False)

DEFAULT_DIR = os.environ.get("CANOE_ARCHIVE_DIR") or \
    "/Users/jasonbyrne/Library/CloudStorage/OneDrive-SharedLibraries-WakeRobin/Investment - Documents/Canoe"
UNKNOWN = "Unknown Investment"
UNDATED = "Undated"

GENERIC = {
    "fund", "funds", "capital", "partners", "ventures", "venture", "credit",
    "opportunities", "opportunity", "global", "real", "estate", "special", "direct",
    "lending", "investment", "investments", "select", "holding", "holdings",
    "technology", "technologies", "frontier", "diversified", "private", "equity",
    "series", "token", "data", "focus", "alpha", "proof", "high", "income", "strategies",
}
MON = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def distinctive_token(name: str) -> str | None:
    words = [w for w in re.findall(r"[A-Za-z]{4,}", name) if w.lower() not in GENERIC]
    return max(words, key=len).lower() if words else None


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def extract_text(path: str, pages: int = 2) -> str:
    try:
        doc = fitz.open(path)
        text = "".join(doc[i].get_text() for i in range(min(pages, len(doc))))
        doc.close()
        return text
    except Exception:
        return ""


def all_dates(text: str) -> list[tuple[int, int, int]]:
    out = []
    for m in re.finditer(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\b", text):
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = 2000 + y if y < 100 else y
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append((y, mo, d))
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", text):
        mn = m.group(1)[:3].lower()
        if mn in MON:
            out.append((int(m.group(3)), MON.index(mn) + 1, int(m.group(2))))
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            out.append((y, mo, d))
    return [t for t in out if 2000 <= t[0] <= 2030]


def is_period_end(y: int, mo: int, d: int) -> bool:
    try:
        last = calendar.monthrange(y, mo)[1]
    except calendar.IllegalMonthError:
        return False
    return d >= last - 1


def detect_year(text: str) -> tuple[str | None, str]:
    """Return (year, confidence) where confidence is 'high' | 'low' | 'none'."""
    ds = all_dates(text)
    if not ds:
        return None, "none"
    pe = [x for x in ds if is_period_end(*x)]
    if pe:
        return str(max(pe)[0]), "high"
    return str(max(ds)[0]), "low"


def build_fund_matchers(dir_path: str):
    managers = [
        d for d in os.listdir(dir_path)
        if os.path.isdir(os.path.join(dir_path, d)) and d != UNKNOWN
    ]
    full = [(norm(m), m) for m in managers]
    tokens = [(distinctive_token(m), m) for m in managers]
    tokens = [(t, m) for t, m in tokens if t]
    return managers, full, tokens


def detect_fund(text: str, full, tokens) -> tuple[str | None, str, list[str]]:
    """Return (fund, confidence, candidates). confidence: 'high'|'ambiguous'|'none'."""
    tl = norm(text)
    strong = sorted({m for n, m in full if len(n) >= 6 and n in tl})
    if len(strong) == 1:
        return strong[0], "high", strong
    if len(strong) > 1:
        return None, "ambiguous", strong
    matched = sorted({m for tok, m in tokens if re.search(r"\b" + re.escape(tok) + r"\b", tl)})
    if len(matched) == 1:
        return matched[0], "high", matched
    if len(matched) > 1:
        return None, "ambiguous", matched
    return None, "none", []


def rel(dir_path, p):
    return os.path.relpath(p, dir_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic cleanup of Undated / Unknown Canoe docs.")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--apply", action="store_true", help="Actually move files (default: dry run).")
    ap.add_argument("--report", default=None, help="CSV path for the review list.")
    ap.add_argument("--undo", default=None, help="Reverse a prior run using its move-log JSON.")
    args = ap.parse_args()
    dir_path = os.path.abspath(args.dir)

    if args.undo:
        with open(args.undo) as f:
            moves = json.load(f)
        undone = 0
        for mv in reversed(moves):
            if os.path.exists(mv["dst"]) and not os.path.exists(mv["src"]):
                os.makedirs(os.path.dirname(mv["src"]), exist_ok=True)
                os.rename(mv["dst"], mv["src"])
                undone += 1
        print(f"Undo complete: reverted {undone}/{len(moves)} moves.")
        return

    managers, full, tokens = build_fund_matchers(dir_path)
    report_path = args.report or os.path.join(dir_path, "_reclassify_review.csv")

    in_scope = []
    for dp, _, fs in os.walk(dir_path):
        for f in fs:
            if not f.lower().endswith(".pdf"):
                continue
            p = os.path.join(dp, f)
            parts = rel(dir_path, p).split(os.sep)
            if len(parts) < 4:
                continue  # not Manager/Year/Category/file
            mgr, yr, cat = parts[0], parts[1], parts[2]
            if mgr == UNKNOWN or yr == UNDATED:
                in_scope.append((p, mgr, yr, cat, f))

    rows = []
    moves = []
    auto = review = image_only = 0

    for p, mgr, yr, cat, fname in in_scope:
        needs_fund = (mgr == UNKNOWN)
        needs_year = (yr == UNDATED)
        text = extract_text(p)
        is_image = len(text.strip()) < 20

        new_mgr, new_yr = mgr, yr
        fund_conf = year_conf = "n/a"
        cands = []

        if needs_year:
            y, year_conf = detect_year(text)
            if year_conf == "high" and y:
                new_yr = y
        if needs_fund:
            fund, fund_conf, cands = detect_fund(text, full, tokens)
            if fund_conf == "high" and fund:
                new_mgr = fund

        year_ok = (not needs_year) or (new_yr != UNDATED)
        fund_ok = (not needs_fund) or (new_mgr != UNKNOWN)
        high_conf = year_ok and fund_ok and not is_image

        decision = "AUTO" if high_conf else "REVIEW"
        new_fname = fname
        new_rel = None
        if high_conf:
            if needs_fund and new_mgr != UNKNOWN:
                new_fname = re.sub(re.escape(UNKNOWN), new_mgr, fname, count=1) if UNKNOWN in fname else f"{new_mgr}-{fname}"
            new_rel = os.path.join(new_mgr, new_yr, cat, new_fname)
            moves.append({"src": p, "dst": os.path.join(dir_path, new_rel)})
            auto += 1
        else:
            review += 1
        if is_image:
            image_only += 1

        rows.append({
            "current_path": rel(dir_path, p),
            "needs": ",".join([x for x, on in [("fund", needs_fund), ("year", needs_year)] if on]),
            "detected_year": new_yr if needs_year else "",
            "year_confidence": year_conf if needs_year else "",
            "detected_fund": new_mgr if needs_fund else "",
            "fund_confidence": fund_conf if needs_fund else "",
            "ambiguous_options": " | ".join(cands) if cands else "",
            "image_only": "yes" if is_image else "",
            "decision": decision,
            "proposed_path": new_rel or "",
        })

    with open(report_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["current_path"])
        w.writeheader()
        w.writerows(rows)

    applied = 0
    log_path = None
    if args.apply and moves:
        # collision-safe: never overwrite; suffix on clash.
        safe_moves = []
        for mv in moves:
            dst = mv["dst"]
            if os.path.abspath(dst) == os.path.abspath(mv["src"]):
                continue
            root, ext = os.path.splitext(dst)
            i = 1
            while os.path.exists(dst):
                i += 1
                dst = f"{root}__{i}{ext}"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(mv["src"], dst)
            safe_moves.append({"src": mv["src"], "dst": dst})
            applied += 1
        import time as _t
        stamp = os.environ.get("RECLASSIFY_STAMP", "run")
        log_path = os.path.join(dir_path, f"_reclassify_moves_{stamp}.json")
        with open(log_path, "w") as f:
            json.dump(safe_moves, f, indent=2)

    print("\n--- Reclassify summary ---")
    print(f"  in scope (Undated/Unknown) : {len(in_scope)}")
    print(f"  image-only (unreadable)    : {image_only}")
    print(f"  HIGH-confidence auto-file  : {auto}")
    print(f"  flagged for review         : {review}")
    print(f"  review CSV                 : {report_path}")
    if args.apply:
        print(f"  MOVES APPLIED              : {applied}")
        print(f"  undo log                   : {log_path}")
    else:
        print("  (dry run -- nothing moved. Re-run with --apply to perform the high-confidence moves.)")


if __name__ == "__main__":
    main()
