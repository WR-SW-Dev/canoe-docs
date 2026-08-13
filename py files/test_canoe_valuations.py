#!/usr/bin/env python3
"""Unit tests for canoe_valuations.build_valuations -- pure logic, no network.

Reflects the real Canoe shape: merged allocation dicts carrying validated_data
(endingBalance/endingDate/entity/fundName) plus the Addepar/Archway crosswalk ids
(addepar_owned_id / addepar_owner_id / archway_identifier).

    python3 test_canoe_valuations.py
"""
import canoe_valuations as CV


def alloc(aid, fund, owned, owner, date, nav, entity="LP A", status="complete",
          arch="ARCH1", **vd_extra):
    vd = {"fundName": fund, "entity": entity, "endingDate": date}
    if nav is not None:
        vd["endingBalance"] = nav
    vd.update(vd_extra)
    return {"allocation_id": aid, "addepar_owned_id": owned, "addepar_owner_id": owner,
            "archway_identifier": arch, "investment_structure": "drawdown_fund",
            "document_status": status, "validated_data": vd}


def main():
    assert CV.detect_nav({"endingBalance": "1,234.5"}) == ("endingBalance", 1234.5)
    assert CV.detect_nav({"endingBalanceQTD": 5}) == ("endingBalanceQTD", 5.0)
    assert CV.detect_nav({"foo": 1}) == (None, None)
    print("detect_nav  OK")

    allocs = [
        alloc("a1", "Fund A", "111", "900", "2024-03-31", 1_000_000, paidInCapital=800000),
        # duplicate period, sparser -> superseded (richer one kept)
        alloc("a2", "Fund A", "111", "900", "2024-03-31", 1_000_000),
        alloc("a3", "Fund A", "111", "900", "2024-06-30", 1_050_000),   # baseline for jump
        alloc("a4", "Fund A", "111", "900", "2024-09-30", 10_500_000),  # 10x jump -> flagged
        alloc("a5", "Fund B", "222", "901", "2024-03-31", 500000, status="Anomaly Detected"),  # review
        alloc("a6", "Fund C", "333", "902", "2024-03-31", None),        # no NAV
        alloc("a7", "Fund D", "444", "903", "2024-03-31", 200000, entity=""),  # no entity
        alloc("a8", "Fund E", None, None, "2024-03-31", 300000, arch=None),    # no crosswalk id
        alloc("a9", "Fund F", "555", "904", "2024-03-31", -10, ),       # negative
    ]
    acc, exc = CV.build_valuations(allocs, jump_pct=0.5)
    accset = {(r["fund"], r["period"], r["nav"]) for r in acc}
    reasons = [e["reason"] for e in exc]

    assert ("Fund A", "2024-03-31", 1_000_000.0) in accset
    assert sum(1 for r in acc if r["fund"] == "Fund A" and r["period"] == "2024-03-31") == 1  # deduped
    # the kept Fund A 3/31 row is the richer one (has paidInCapital)
    kept = [r for r in acc if r["fund"] == "Fund A" and r["period"] == "2024-03-31"][0]
    assert kept.get("paidInCapital") == 800000.0
    assert ("Fund A", "2024-06-30", 1_050_000.0) in accset
    assert ("Fund A", "2024-09-30", 10_500_000.0) not in accset  # jump flagged
    for needle in ("review status", "no NAV", "no entity", "crosswalk", "negative",
                   "duplicate for period", "NAV jump"):
        assert any(needle in r for r in reasons), needle
    print("all gates + crosswalk + dedupe + jump  OK")
    print("\nALL CANOE VALUATION TESTS PASSED")


if __name__ == "__main__":
    main()
