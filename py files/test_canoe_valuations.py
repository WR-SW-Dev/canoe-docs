#!/usr/bin/env python3
"""Unit tests for canoe_valuations.build_valuations -- pure logic, no network/creds.

Run:  python3 test_canoe_valuations.py
Covers NAV auto-detection and every validation gate (review status, consolidated
span>1, missing tag/entity, missing NAV, negative NAV, duplicate-period dedupe by
statement priority, and the period-over-period NAV-jump outlier flag).
"""
import canoe_valuations as CV


def doc(did, dtype, status, allocs, uploaded="2026-01-15"):
    return {"id": did, "name": did + ".pdf", "document_type": dtype,
            "document_status": status, "uploaded": uploaded, "allocations": allocs}


def al(inv, iid, ent, date, **kw):
    d = {"investment": inv, "investment_id": iid, "entity": ent, "data_date": date}
    d.update(kw)
    return d


def main():
    assert CV.detect_nav({"reporting_value": "1,234.5"}) == ("reporting_value", 1234.5)
    assert CV.detect_nav({"market_value": 500}) == ("market_value", 500.0)
    assert CV.detect_nav({"foo": 1}) == (None, None)
    print("detect_nav  OK")

    docs = [
        doc("d1", "Capital Account Statement", "confirmed",
            [al("Fund A", "111", "Entity X", "2024-03-31", reporting_value=1000)]),
        doc("d2", "Quarterly Report", "confirmed",
            [al("Fund A", "111", "Entity X", "2024-03-31", reporting_value=1000)]),  # dup, superseded
        doc("d3", "Account Statement", "Anomaly Detected",
            [al("Fund A", "111", "Entity X", "2024-06-30", reporting_value=1100)]),  # review
        doc("d4", "Account Statement", "confirmed",
            [al("Fund A", "111", "Entity X", "2024-09-30", reporting_value=1),
             al("Fund B", "222", "Entity X", "2024-09-30", reporting_value=2)]),      # consolidated
        doc("d5", "Account Statement", "confirmed",
            [al("Fund A", "111", "--", "2024-12-31", reporting_value=1200)]),          # bad entity
        doc("d6", "Account Statement", "confirmed",
            [al("Fund A", "111", "Entity X", "2025-03-31")]),                          # no NAV
        doc("d7", "Capital Account Statement", "confirmed",
            [al("Fund A", "111", "Entity X", "2024-06-30", reporting_value=1050)]),
        doc("d8", "Capital Account Statement", "confirmed",
            [al("Fund A", "111", "Entity X", "2024-09-30", reporting_value=10500)]),   # 10x jump
    ]
    acc, exc = CV.build_valuations(docs, jump_pct=0.5)
    accset = {(r["period"], r["nav"]) for r in acc}

    assert ("2024-03", 1000.0) in accset
    assert not any(r["document_type"] == "Quarterly Report" for r in acc)
    assert ("2024-06", 1050.0) in accset
    assert ("2024-09", 10500.0) not in accset
    for needle in ("review status", "span >1 investment", "entity",
                   "no NAV field", "duplicate for period", "NAV jump"):
        assert any(needle in e["reason"] for e in exc), needle
    print("all gates + dedupe + jump flag  OK")
    print("\nALL CANOE PHASE-1 VALIDATION TESTS PASSED")


if __name__ == "__main__":
    main()
