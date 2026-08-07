#!/usr/bin/env python3
"""
canoe_route.py -- Route non-fund documents to dedicated top-level folders.

Content-based (no-AI) routing for documents that don't belong under a fund:
  * Merrill / Bank of America custodian statements  -> `Merrill/`
  * News / press articles                           -> `News Articles/`

Reads PDF text LOCALLY and moves only confident matches. Dry-run by default;
--apply writes an undo log. Emits metadata only -- never document content.

Confidence:
  * merrill : text contains "merrill" or "mlpf" (specific, reliable).
  * news    : text contains a real publication name AND the doc is not typed as a
              financial statement (K-1, Account Statement, Financials, etc.).
              News is inherently fuzzy -- treat as best-effort, review the list.

Usage:
  python canoe_route.py                      # dry run, all rules
  python canoe_route.py --rules merrill --apply
  python canoe_route.py --rules news         # dry run, news only
  python canoe_route.py --undo _route_moves_<stamp>.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re

import fitz  # PyMuPDF

fitz.TOOLS.mupdf_display_errors(False)

DEFAULT_DIR = "/Users/jasonbyrne/Library/CloudStorage/OneDrive-WakeRobin/Canoe/Private Fund Reporting"
SCOPE_SUBDIR = "Unknown Investment"   # where unmapped docs live

PUBS = [
    "reuters", "bloomberg", "wall street journal", "the wall street journal", "wsj",
    "financial times", "cnbc", "forbes", "techcrunch", "axios", "associated press",
    "business wire", "prnewswire", "pr newswire", "marketwatch", "barron", "seeking alpha",
    "the information", "the new york times", "yahoo finance", "fortune", "wired",
]
FIN_TYPES = [
    "Account Statement", "Financials", "K-1", "Capital Call", "Capital Account",
    "Quarterly Report", "Annual Report", "Capital Distribution", "Performance Estimate",
]


def extract_text(path: str, pages: int = 2) -> str:
    try:
        doc = fitz.open(path)
        t = "".join(doc[i].get_text() for i in range(min(pages, len(doc))))
        doc.close()
        return t
    except Exception:
        return ""


def is_merrill(text: str) -> bool:
    t = text.lower()
    return "merrill" in t or "mlpf" in t


def is_news(text: str, fname: str) -> bool:
    t = text.lower()
    if any(ft.lower() in fname.lower() for ft in FIN_TYPES):
        return False
    return any(p in t for p in PUBS)


def classify(text: str, fname: str) -> str | None:
    if is_merrill(text):
        return "Merrill"
    if is_news(text, fname):
        return "News Articles"
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Route Merrill / news docs to dedicated folders.")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--rules", default="merrill,news", help="Comma list: merrill,news")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", default=None)
    args = ap.parse_args()
    dir_path = os.path.abspath(args.dir)

    if args.undo:
        moves = json.load(open(args.undo))
        n = 0
        for mv in reversed(moves):
            if os.path.exists(mv["dst"]) and not os.path.exists(mv["src"]):
                os.makedirs(os.path.dirname(mv["src"]), exist_ok=True)
                os.rename(mv["dst"], mv["src"])
                n += 1
        print(f"Undo complete: reverted {n}/{len(moves)} moves.")
        return

    want = {r.strip().lower() for r in args.rules.split(",")}
    label_for = {"Merrill": "merrill", "News Articles": "news"}
    scope = os.path.join(dir_path, SCOPE_SUBDIR)

    rows, moves = [], []
    counts = {"Merrill": 0, "News Articles": 0}
    image_only = 0

    for dp, _, fs in os.walk(scope):
        for f in fs:
            if not f.lower().endswith(".pdf"):
                continue
            p = os.path.join(dp, f)
            text = extract_text(p)
            if len(text.strip()) < 20:
                image_only += 1
                continue
            label = classify(text, f)
            if not label or label_for[label] not in want:
                continue
            parts = os.path.relpath(p, dir_path).split(os.sep)
            # parts = [Unknown Investment, Year, Category, file] -> keep Year/Category
            sub = parts[1:-1]
            new_name = re.sub(re.escape(SCOPE_SUBDIR), label, f, count=1) if SCOPE_SUBDIR in f else f
            new_rel = os.path.join(label, *sub, new_name)
            counts[label] += 1
            moves.append({"label": label, "src": p, "dst": os.path.join(dir_path, new_rel)})
            rows.append({"label": label, "current": os.path.relpath(p, dir_path), "proposed": new_rel})

    report = os.path.join(dir_path, "_route_review.csv")
    if rows:
        with open(report, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["label", "current", "proposed"])
            w.writeheader()
            w.writerows(rows)

    applied = 0
    log_path = None
    if args.apply and moves:
        safe = []
        for mv in moves:
            dst = mv["dst"]
            root, ext = os.path.splitext(dst)
            i = 1
            while os.path.exists(dst):
                i += 1
                dst = f"{root}__{i}{ext}"
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(mv["src"], dst)
            safe.append({"src": mv["src"], "dst": dst})
            applied += 1
        stamp = os.environ.get("ROUTE_STAMP", "run")
        log_path = os.path.join(dir_path, f"_route_moves_{stamp}.json")
        json.dump(safe, open(log_path, "w"), indent=2)

    print("\n--- Route summary ---")
    print(f"  scope                : {SCOPE_SUBDIR}")
    print(f"  image-only (skipped) : {image_only}")
    print(f"  Merrill matches      : {counts['Merrill']}")
    print(f"  News matches         : {counts['News Articles']}")
    print(f"  review CSV           : {report if rows else '(none)'}")
    if args.apply:
        print(f"  MOVES APPLIED        : {applied}")
        print(f"  undo log             : {log_path}")
    else:
        print("  (dry run -- nothing moved. Add --apply to move the selected rules.)")


if __name__ == "__main__":
    main()
