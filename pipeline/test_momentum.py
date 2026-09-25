#!/usr/bin/env python3
"""
Fixtures for calibrate.py and momentum.py. The behaviours Jack asked for, pinned:

  * a set that always scores 70 reads 50 at 70
  * a set that always moves ten points reads a ten-point move as normal
  * a real ramp reads high, then becomes the new normal, and the trend still says so
  * a big advertiser having a normal week is not pressure
  * DC activity from a national brand outweighs the same volume with no geography

    python3 test_momentum.py
"""

from __future__ import annotations

import sys
from datetime import date

import calibrate as cal
import momentum as mo

WK = date(2026, 9, 21)


# ── calibrate.py ─────────────────────────────────────────────────────────────

def test_always_seventy_is_normal():
    assert cal.calibrate_score(70, [70] * 8) == 50
    assert cal.calibrate_score(70, [72, 68, 70, 71, 69, 70, 72, 68]) == 50


def test_usual_swing_is_normal():
    h = [62, 72, 62, 72, 62, 72, 62, 72]
    s = cal.calibrate_score(72, h)
    assert 50 <= s <= 66, s       # a usual top-of-range week, not an alarm
    assert cal.calibrate_score(92, h) >= 80   # twice the usual swing above the top


def test_ramp_then_new_normal_then_trend():
    h = [42] * 8
    assert cal.calibrate_score(72, h) >= 90, "a 30-point jump on a flat series is a ramp"
    series = [42] * 8 + [72] * 12
    assert cal.calibrate_score(72, series[:-1]) == 50, "after a quarter, 72 is normal"
    assert cal.trend(series[-12:]) == "steady"
    assert cal.trend([42] * 4 + [72] * 8) == "hotter", "the slow reading keeps the ramp"


def test_one_freak_week_does_not_move_normal():
    h = [50, 50, 50, 95, 50, 50, 50, 50]
    assert cal.calibrate_score(50, h) == 50


def test_floor_stops_noise_explosions():
    # A dead-flat history has zero spread. One model step of 10 must not read as 100.
    assert cal.calibrate_score(52, [42] * 8) == 65
    assert cal.robust_z(1, [0] * 8, abs_floor=1.0) == 1.0


def test_calibrating_until_four_weeks():
    assert cal.calibrate_score(70, [70, 70, 70]) is None
    assert cal.calibrate_score(70, [None, 70, None, 70, 70, 70]) == 50


# ── momentum.py ──────────────────────────────────────────────────────────────

COMPS = [
    {"id": "big", "name": "Onelife", "single_market": False},
    {"id": "loc", "name": "VIDA", "single_market": True},
    {"id": "nat", "name": "Movement", "single_market": False},
]


def ads(comp, n, geo="none", start="2026-01-01", prefix="a"):
    return [{"id": f"{prefix}{comp}{i}", "signal_type": "ad_active", "competitor_id": comp,
             "geo_relevance": geo, "data": {"start_date": start}} for i in range(n)]


def metrics(signals, ran=("ads", "web", "email", "search"), email=("big", "loc", "nat"),
            terms=None):
    return mo.week_metrics(signals, COMPS, WK, ran=set(ran), email_channels=set(email),
                           watch_terms=terms)


def test_geo_weighting():
    m = metrics(ads("nat", 4, geo="dc_explicit") + ads("big", 4) + ads("loc", 4))
    assert m["nat"]["metrics"]["ads_active"] == 4.0, "DC-referencing counts in full"
    assert m["big"]["metrics"]["ads_active"] == 1.0, "no geography counts a quarter"
    assert m["loc"]["metrics"]["ads_active"] == 4.0, "single-market brand is local by definition"


def test_recruitment_and_launches():
    s = ads("big", 3, start="2026-09-17") + ads("big", 2, start="2026-03-01", prefix="o")
    s.append({"id": "hire", "signal_type": "ad_active", "competitor_id": "big",
              "geo_relevance": "dc_explicit", "data": {"is_recruitment": True,
                                                       "start_date": "2026-09-18"}})
    m = metrics(s)["big"]["metrics"]
    assert m["ads_active"] == 1.25 and m["ads_launched"] == 0.75


def test_unknown_is_not_zero():
    m = metrics([], ran=("ads",), email=("big",))
    assert "web" not in m["big"]["metrics"], "web did not run: unknown, not zero"
    assert "email" not in m["loc"]["metrics"], "no email channel: unknown, not zero"
    assert m["big"]["metrics"]["ads_active"] == 0.0, "ads ran and found none: a real zero"


def test_events_and_watch_terms():
    s = [
        {"id": "w", "signal_type": "web_change", "competitor_id": "nat", "geo_relevance": "none",
         "data": {"surfaces": True, "materiality": "material", "applies_locally": "unknown",
                  "change_types": ["price"], "evidence": ["price: removed $116; added $109"]}},
        {"id": "e", "signal_type": "email", "competitor_id": "big", "geo_relevance": "none",
         "data": {"email_type": "offer", "materiality": "material",
                  "subject": "Now open near Columbia Heights"}},
        {"id": "c", "signal_type": "email", "competitor_id": "loc", "geo_relevance": "none",
         "data": {"email_type": "confirmation", "materiality": "noise"}},
    ]
    m = metrics(s, terms=["columbia heights"])
    assert m["nat"]["metrics"]["web"] == 1.5, "material x unknown-locally"
    assert [e["kind"] for e in m["nat"]["events"]] == ["price"]
    assert {e["kind"] for e in m["big"]["events"]} == {"offer", "watch_term"}
    assert m["loc"]["metrics"]["email"] == 0.0, "confirmations are not pressure"


def history(value, weeks=8):
    return [{"ads_active": value, "ads_launched": 2.0} for _ in range(weeks)]


def test_big_advertiser_normal_week_is_normal():
    m = {"ads_active": 66.5, "ads_launched": 2.0}
    r = mo.score_row(m, history(66.5), [])
    assert r["status"] == "scored" and r["score"] == 50


def test_local_push_reads_as_ramp():
    r = mo.score_row({"ads_active": 12.0, "ads_launched": 6.0},
                     [{"ads_active": 4.0, "ads_launched": 1.0}] * 8, [])
    assert r["score"] >= 85, r


def test_events_add_after_calibration_and_cap():
    ev = [{"kind": "watch_term", "points": 20}, {"kind": "price", "points": 10},
          {"kind": "offer", "points": 10}]
    r = mo.score_row({"ads_active": 66.5, "ads_launched": 2.0}, history(66.5), ev)
    assert r["score"] == 50 + mo.EVENT_CAP


def test_week_one_scores_every_component_against_the_set():
    # Jack, 24 Sep: a score for each component from the first week, no lookback needed.
    per = {
        "big": {"metrics": {"ads_active": 70.0, "ads_launched": 0.5, "web": 0.0}, "events": []},
        "nat": {"metrics": {"ads_active": 18.0, "ads_launched": 11.0, "web": 0.0}, "events": []},
        "loc": {"metrics": {"ads_active": 30.0, "ads_launched": 3.0, "web": 0.0}, "events": []},
    }
    p = mo.score_week(per, {}, COMPS)
    by = {c["competitor"]: c for c in p["competitors"]}
    assert all(c["status"] == "scored" for c in by.values())
    assert by["Movement"]["components"]["paid"]["score"] > 70, "11 launches against 0.5 and 3"
    assert by["Onelife"]["components"]["paid"]["score"] < 50, \
        "the biggest advertiser launching nothing is not paid pressure"
    assert by["Onelife"]["metric_z"]["ads_active"] is None, "size is never compared to the set"
    assert by["Movement"]["components"]["web"]["score"] == 50
    assert by["Movement"]["components"]["email"]["basis"] == "not collected"
    assert by["Movement"]["basis"]["ads_launched"] == "set"
    assert p["market"]["status"] == "scored"
    assert p["driver"]["competitor"] == "Movement"
    assert "set" in p["driver"]["reason"]


def test_own_history_takes_over_gradually():
    peers = {"big": {"metrics": {"ads_launched": 1.0}, "events": []},
             "loc": {"metrics": {"ads_launched": 1.0}, "events": []}}
    per = dict(peers, nat={"metrics": {"ads_launched": 8.0}, "events": []})
    week1 = mo.score_week(per, {}, COMPS)
    usual = [{"ads_launched": 8.0}]
    blend = mo.score_week(per, {"nat": usual * 2}, COMPS)
    own = mo.score_week(per, {"nat": usual * 4}, COMPS)
    s = lambda p: next(c for c in p["competitors"] if c["competitor"] == "Movement")
    assert s(week1)["basis"]["ads_launched"] == "set" and s(week1)["score"] > 70
    assert s(blend)["basis"]["ads_launched"] == "blend"
    assert 50 < s(blend)["score"] < s(week1)["score"], "halfway to its own normal"
    assert s(own)["basis"]["ads_launched"] == "own" and s(own)["score"] == 50, \
        "eight launches every week is Movement's normal"


def test_events_still_add():
    per = metrics([{"id": "e", "signal_type": "email", "competitor_id": "big",
                    "geo_relevance": "none", "data": {"email_type": "offer",
                                                      "materiality": "material"}}])
    p = mo.score_week(per, {}, COMPS)
    big = next(c for c in p["competitors"] if c["competitor"] == "Onelife")
    assert big["event_points"] == 10 and big["score"] >= 60
    assert p["driver"]["competitor"] == "Onelife"


def test_driver_is_furthest_above_their_own_normal():
    per = {
        "big": {"metrics": {"ads_active": 70.0, "ads_launched": 2.0}, "events": []},
        "nat": {"metrics": {"ads_active": 9.0, "ads_launched": 3.0}, "events": []},
        "loc": {"metrics": {"ads_active": 5.0, "ads_launched": 1.0}, "events": []},
    }
    hist = {"big": history(66.5), "nat": [{"ads_active": 4.0, "ads_launched": 1.0}] * 8,
            "loc": [{"ads_active": 5.0, "ads_launched": 1.0}] * 8}
    p = mo.score_week(per, hist, COMPS)
    assert p["driver"]["competitor"] == "Movement", p["driver"]
    assert p["driver"]["reason"] == "furthest above their own normal"


def test_other_market_is_not_pressure():
    s = [{"id": "e", "signal_type": "email", "competitor_id": "nat", "geo_relevance": "other_market",
          "data": {"email_type": "opening", "materiality": "material", "subject": "Now open in Denver"}}]
    m = metrics(s)
    assert m["nat"]["metrics"]["email"] == 0.0 and m["nat"]["events"] == []


def test_new_metric_does_not_fake_a_trend():
    hist = [{"ads_active": 10.0, "ads_launched": 2.0}] * 7 + \
           [{"ads_active": 10.0, "ads_launched": 2.0, "email": 3.0}] * 5
    r = mo.score_row({"ads_active": 10.0, "ads_launched": 2.0, "email": 3.0}, hist, [])
    assert r["score"] == 50 and r["trend"] != "hotter", r


def test_partial_social_collection_is_unknown():
    s = [{"id": "p", "signal_type": "social_profile", "competitor_id": "big", "channel_id": "fb",
          "source_scope": "local", "geo_relevance": "none", "data": {}}]
    both = {"big": {"fb", "ig"}}
    assert "social_posts" not in mo.week_metrics(
        s, COMPS, WK, ran={"social"}, email_channels=set(), social_channels=both)["big"]["metrics"]
    assert mo.week_metrics(
        s, COMPS, WK, ran={"social"}, email_channels=set(),
        social_channels={"big": {"fb"}})["big"]["metrics"]["social_posts"] == 0.0


def test_launches_count_messages_not_ids():
    dup = [{"id": f"v{i}", "signal_type": "ad_active", "competitor_id": "loc",
            "geo_relevance": "none",
            "data": {"start_date": "2026-09-19", "body": "Join  VIDA today" if i < 20 else f"Msg {i}"}}
           for i in range(23)]
    m = metrics(dup)["loc"]["metrics"]
    assert m["ads_active"] == 23.0, "running ads still counted by ID"
    assert m["ads_launched"] == 4.0, "20 duplicates of one message plus 3 others is 4 messages"


def test_location_page_ads_count_in_full():
    s = [{"id": "x", "signal_type": "ad_active", "competitor_id": "nat", "geo_relevance": "none",
          "source_scope": "local", "data": {"start_date": "2026-01-01"}}]
    assert metrics(s)["nat"]["metrics"]["ads_active"] == 1.0, "a location page is local"


def test_benchmark_is_scored_beside_never_among():
    per = {"big": {"metrics": {"ads_launched": 1.0}, "events": []},
           "nat": {"metrics": {"ads_launched": 2.0}, "events": []},
           "loc": {"metrics": {"ads_launched": 1.5}, "events": []}}
    before = mo.score_week(per, {}, COMPS)
    b = mo.score_benchmark({"metrics": {"ads_launched": 9.0}, "events": []}, [], per)
    after = mo.score_week(per, {}, COMPS)
    assert before == after, "scoring the client changes nothing about the competitors"
    assert b["basis"]["ads_launched"] == "set" and b["components"]["paid"]["score"] > 70
    assert b["event_points"] == 0


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
