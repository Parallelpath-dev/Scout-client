#!/usr/bin/env python3
"""
Fixtures for synthesizer.py. No network, no database, no model: the model is a function
that returns canned JSON, so every rule the code owns is pinned here.

    python3 test_synthesizer.py
"""

from __future__ import annotations

import json
import sys
from datetime import date

import momentum as mo
import synthesizer as sy
from week_window import in_window

WK = date(2026, 9, 21)
PREV = "2026-09-14"

COMPS = [
    {"id": "c-mov", "name": "Movement", "single_market": False, "prices_by_location": True,
     "display_local": "Movement Crystal City"},
    {"id": "c-one", "name": "Onelife", "single_market": False, "prices_by_location": False},
    {"id": "c-vida", "name": "VIDA", "single_market": True, "prices_by_location": False},
    {"id": "c-ymca", "name": "YMCA", "single_market": True, "prices_by_location": False},
]
EMAIL_CHANNELS = {"c-mov", "c-one", "c-vida"}  # YMCA has none


def sig(i, t, comp, *, geo="none", scope="national", data=None, week="2026-09-21", url=None):
    return {"id": i, "signal_type": t, "competitor_id": comp, "geo_relevance": geo,
            "source_scope": scope, "data": data or {}, "week_of": week,
            "source_url": url or f"https://example.com/{i}", "collected_at": "2026-09-21T08:00:00Z"}


SIGNALS = [
    # Movement national price change: material (0), geo none (3). Must outrank the next.
    sig("w1", "web_change", "c-mov", data={
        "headline_type": "price", "materiality": "material", "surfaces": True,
        "rank": [0, 3, 0], "applies_locally": "unknown", "price_before": ["116"],
        "price_after": ["109"], "evidence": ["price: removed $116; added $109"],
        "caveat": "This brand prices by home gym. The change is on a national page and may "
                  "not apply in DC. Verify against the local location page before acting."}),
    # VIDA DC schedule tweak: minor (1), dc_explicit (1).
    sig("w2", "web_change", "c-vida", geo="dc_explicit", scope="local", data={
        "headline_type": "schedule", "materiality": "minor", "surfaces": True,
        "rank": [1, 1, 4], "applies_locally": "yes", "evidence": ['schedule: "new class"']}),
    # Cosmetic, does not surface.
    sig("w3", "web_change", "c-one", data={"materiality": "cosmetic", "surfaces": False,
                                           "rank": [2, 3, 6]}),
    # Onelife promo email sent LAST week: filed under the previous Monday, must be read.
    sig("e1", "email", "c-one", week=PREV, data={
        "email_type": "offer", "materiality": "material", "surfaced": True, "rank": [0, 3, 0],
        "subject": "$0 enrollment ends Sunday", "evidence": ['offer: "$0 enrollment"']}),
    # A web change from last week must NOT be read again.
    sig("w-old", "web_change", "c-mov", week=PREV, data={"surfaces": True, "rank": [0, 0, 0]}),
    # Ads.
    sig("a1", "ad_active", "c-one", geo="dc_explicit", data={
        "body": "Join Onelife Tenleytown today", "display_format": "IMAGE",
        "start_date": "2026-09-18"}),
    sig("a2", "ad_active", "c-one", data={
        "body": "{{product.brand}}", "text_scanned": {"card_0_body": "Gyms across the DMV"},
        "display_format": "DCO", "start_date": "2026-01-02"}),
    sig("a3", "ad_active", "c-one", data={"body": "We're hiring trainers", "is_recruitment": True,
                                          "start_date": "2026-09-19"}),
    sig("a4", "ad_active", "c-vida", data={"body": "Summer at VIDA", "start_date": "2026-06-01"}),
    # Search, with the directional caveat on the row.
    sig("s1", "domain_overview", "c-one", week="2026-09-21", data={
        "paid_keywords": 36, "accuracy": "directional",
        "caveat": "Semrush paid estimates are directional. Treat as a rough relative indicator."}),
    sig("s2", "client_keyword_positions", None, data={
        "keywords": [{"keyword": "bouldering", "position": 5, "volume": 210}]}),
]

ROLLUPS = [
    {"competitor_id": "c-one", "total_available": 266, "ads_sampled": 266, "sample_method": "census",
     "dc_landing": 0, "dc_explicit": 3, "regional": 10, "other_market": 16},
    {"competitor_id": "c-vida", "total_available": 21, "ads_sampled": 21, "sample_method": "census",
     "dc_landing": 0, "dc_explicit": 0, "regional": 0, "other_market": 0},
]
PRIOR = [{"competitor_id": "c-one", "total_available": 250, "dc_landing": 0, "dc_explicit": 1}]

CLIENT = {"id": "cl-bp", "name": "Bouldering Project", "output_profile": "executive",
          "brain": "Opening Columbia Heights. Eckington overlap is raised on a call only."}


def window():
    return [s for s in SIGNALS if in_window(s, WK)]


def digest():
    return sy.build_digest(window(), COMPS, ROLLUPS, PRIOR, EMAIL_CHANNELS,
                           "Bouldering Project", WK)


GOOD_ANALYSIS = {
    "summary": "Movement cut a national price and Onelife ran a $0 enrollment offer. "
               "Neither is confirmed in DC yet, but both land in your pre-opening window.",
    "developments": [
        # Deliberately returned in the WRONG order: code must reorder on collector rank.
        {"competitor": "VIDA", "headline": "VIDA added a new class at U Street",
         "so_what": "Adds programming on your corridor.", "observed": "One schedule change.",
         "confidence": "high", "signal_ids": ["w2"]},
        {"competitor": "Onelife", "headline": "Onelife waived enrollment for new members",
         "so_what": "Removes the joining cost for price-sensitive prospects.",
         "observed": "A $0 enrollment email and a Tenleytown ad.",
         "confidence": "high", "signal_ids": ["e1", "a1"]},
        {"competitor": "Movement", "headline": "Movement dropped its national membership price",
         "so_what": "Narrows the gap to your $124 list price.",
         "observed": "Memberships page moved from $116 to $109.",
         "confidence": "high", "signal_ids": ["w1", "w1"]},
    ],
    "sections": {"paid": {"spend_signals": [{"competitor": "Onelife", "observation": "266 ads.",
                                             "signal_ids": ["a1"]}], "junk": [1]},
                 "owned": "not a dict"},
}

GOOD_STRATEGY = {"recommendations": [
    {"index": 0, "recommendation": "Check the Crystal City page before matching anything."},
    {"index": 1, "recommendation": "Brief pre-opening copy on what the enrollment fee buys."},
    {"index": 2, "recommendation": "Note the class in the corridor comparison sheet."},
]}


def fake_model(analysis, strategy):
    calls = []

    def m(system, user, max_tokens, temperature):
        calls.append(system)
        return json.dumps(analysis if len(calls) == 1 else strategy)

    return m, calls


# ── tests ────────────────────────────────────────────────────────────────────

def test_window():
    ids = {s["id"] for s in window()}
    assert "e1" in ids, "last week's email must be read: email is filed by send date"
    assert "w-old" not in ids, "last week's web change must not be re-read"
    snap = sig("p", "page_snapshot", "c-mov")
    assert not in_window(snap, WK), "snapshots are memory, not evidence"
    loose = {"id": "x", "signal_type": "domain_overview", "week_of": None,
             "collected_at": "2026-09-22T14:00:00Z"}
    assert in_window(loose, WK) and not in_window(loose, date(2026, 9, 28))


def test_ranked_order_is_the_collectors():
    d = digest()
    order = [i["signal_id"] for i in d["ranked_changes"]]
    assert order[:2] in (["w1", "e1"], ["e1", "w1"]), order  # both (0,3,0)
    assert order[-1] == "w2", "materiality outranks geography: DC minor goes last"
    assert "w3" not in order and d["not_surfaced"]["web_change"] == 1
    mov = next(i for i in d["ranked_changes"] if i["signal_id"] == "w1")
    assert mov["applies_locally"] == "unknown" and "home gym" in mov["caveat"]


def test_paid_digest():
    d = digest()
    one = next(p for p in d["paid"] if p["competitor"] == "Onelife")
    assert one["dc_referencing_floor"] == 3
    assert one["prior_week"]["dc_referencing_floor"] == 1
    assert one["recruitment_ads"] == 1
    assert [a["signal_id"] for a in one["geo_referencing_ads"]] == ["a1"]
    assert all(a["signal_id"] != "a3" for a in one["new_ads"]), "hiring ads are not acquisition"
    vida = next(p for p in d["paid"] if p["competitor"] == "VIDA")
    assert vida["single_market"] is True
    # DCO: the real copy lives on the cards, never the template token.
    a2 = sy.build_digest([s for s in window() if s["id"] == "a2"], COMPS,
                         [dict(ROLLUPS[0], regional=0)], [], set(), "BP", WK)
    assert "{{" not in json.dumps(a2)


def test_coverage():
    cov = " ".join(digest()["coverage"])
    assert "YMCA" in cov and "No email list" in cov
    assert "No email received this week from" in cov and "Movement" in cov and "VIDA" in cov
    assert "floor" in cov and "directional" in cov and "Organic social" in cov


def test_search_carries_directional():
    d = digest()
    ov = next(s for s in d["search"] if s["signal_id"] == "s1")
    assert ov["accuracy"] == "directional" and "directional" in ov["caveat"]
    cl = next(s for s in d["search"] if s["signal_id"] == "s2")
    assert cl["competitor"] == "Bouldering Project (client)"
    assert cl["keywords"][0]["position"] == 5


def pressure():
    per = mo.week_metrics(window(), COMPS, WK, ran={"ads", "web", "email", "search"},
                          email_channels=EMAIL_CHANNELS, watch_terms=["tenleytown"])
    return mo.score_week(per, {}, COMPS)


def test_pressure_is_code_not_model():
    p = pressure()
    assert p["market"]["status"] == "calibrating" and p["market"]["score"] is None
    one = next(c for c in p["competitors"] if c["competitor"] == "Onelife")
    assert {e["kind"] for e in one["events"]} >= {"offer", "watch_term"}
    assert p["driver"]["competitor"] == "Onelife"


def test_end_to_end_publishes():
    d = digest()
    model, calls = fake_model(GOOD_ANALYSIS, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=d, signals=window(), prior_score=40,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    heads = [x["competitor"] for x in row["developments"]]
    assert heads[-1] == "VIDA", heads
    assert set(heads[:2]) == {"Movement", "Onelife"}, heads
    mov = next(x for x in row["developments"] if x["competitor"] == "Movement")
    assert mov["applies_locally"] == "unknown" and "home gym" in mov["caveat"]
    assert mov["source_url"] == "https://example.com/w1"
    assert all(x.get("recommendation") for x in row["developments"])
    fr = row["full_report"]
    assert set(fr["sections"]) == {"search", "paid", "social", "owned"}
    assert "junk" not in fr["sections"]["paid"] and fr["sections"]["owned"]["website_changes"] == []
    assert row["pressure_score"] is None and fr["pressure"]["status"] == "calibrating"
    assert fr["pressure"]["delta"] is None
    assert fr["pressure"]["driver"]["competitor"] == "Onelife"
    assert fr["validation"]["ok"] is True
    assert "OUTPUT PROFILE: EXECUTIVE" in calls[0] and "OUTPUT PROFILE: EXECUTIVE" in calls[1]


def test_fabricated_id_holds():
    bad = json.loads(json.dumps(GOOD_ANALYSIS))
    bad["developments"][1]["signal_ids"] = ["e1", "not-a-real-id"]
    model, _ = fake_model(bad, GOOD_STRATEGY)
    _, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=model)
    assert not rep.ok and any("not collected this week" in f for f in rep.failures)


def test_cannibalisation_holds():
    leak = json.loads(json.dumps(GOOD_STRATEGY))
    leak["recommendations"][0]["recommendation"] = "Watch Eckington cannibalisation."
    model, _ = fake_model(GOOD_ANALYSIS, leak)
    _, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=model)
    assert not rep.ok and any("out of writing" in f for f in rep.failures)


def test_ceiling_cuts_count_not_words():
    many = json.loads(json.dumps(GOOD_ANALYSIS))
    extra = {"competitor": "Onelife", "headline": "Onelife raised its ad count",
             "so_what": "More reach.", "observed": "266 ads.", "confidence": "high",
             "signal_ids": ["a1", "a2"]}
    many["developments"] += [extra, dict(extra), dict(extra)]
    model, _ = fake_model(many, GOOD_STRATEGY)
    row, _ = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=model)
    assert len(row["developments"]) == 4
    assert [x["competitor"] for x in row["developments"]][2] == "VIDA", \
        "ranked developments come before unranked ones"


def test_extract_json():
    assert sy.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert sy.extract_json('Here you go {"a": 2} thanks') == {"a": 2}
    try:
        sy.extract_json("[1, 2]")
        raise AssertionError("a list is not a briefing")
    except ValueError:
        pass


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
