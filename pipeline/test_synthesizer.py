#!/usr/bin/env python3
"""
Fixtures for synthesizer.py. No network, no database, no model: the model is a function
that returns canned JSON, so every rule the code owns is pinned here.

    python3 test_synthesizer.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date

import momentum as mo
import synthesizer as sy
import validate_briefing as vb
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
    sig("a4", "ad_active", "c-vida", data={"body": "Fall classes at VIDA", "start_date": "2026-09-19"}),
    sig("a5", "ad_active", "c-mov", data={"body": "Memberships from $109", "start_date": "2026-09-18"}),
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
    {"competitor_id": "c-mov", "total_available": 66, "ads_sampled": 66, "sample_method": "census",
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
         "confidence": "high", "signal_ids": ["w2", "a4"]},
        {"competitor": "Onelife", "headline": "Onelife waived enrollment for new members",
         "so_what": "Removes the joining cost for price-sensitive prospects.",
         "observed": "A $0 enrollment email and a Tenleytown ad.",
         "confidence": "high", "signal_ids": ["e1", "a1"]},
        {"competitor": "Movement", "headline": "Movement dropped its national membership price",
         "so_what": "Narrows the gap to your $124 list price.",
         "observed": "Memberships page moved from $116 to $109.",
         "confidence": "high", "signal_ids": ["w1", "a5"]},
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
    """analysis may be a list: one answer per Analyst call, the last one repeating."""
    calls = []
    answers = analysis if isinstance(analysis, list) else [analysis]
    seen = {"analyst": 0}

    def m(system, user, max_tokens, temperature):
        calls.append(system)
        if "Scout Analyst" in system:
            a = answers[min(seen["analyst"], len(answers) - 1)]
            seen["analyst"] += 1
            return json.dumps(a)
        return json.dumps(strategy)

    return m, calls


# ── tests ────────────────────────────────────────────────────────────────────

def test_window():
    ids = {s["id"] for s in window()}
    assert "e1" in ids, "last week's email must be read: email is filed by send date"
    assert "w-old" not in ids, "last week's web change must not be re-read"
    monday = sig("e-now", "email", "c-one", week="2026-09-21")
    assert not in_window(monday, WK), "an email sent this Monday is next week's briefing"
    assert in_window(sig("e-prev", "email", "c-one", week=PREV), WK)
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


def test_launches_are_counted_in_messages():
    # The 21 Sep briefing said "VIDA launched 28 new ads". It was 8 messages.
    dup = [sig(f"v{i}", "ad_active", "c-vida", data={
        "body": "Join VIDA today" if i < 20 else f"Message {i}", "start_date": "2026-09-19"})
        for i in range(23)]
    d = sy.build_digest(dup, COMPS, [ROLLUPS[1]], [], set(), "BP", WK)
    v = d["paid"][0]
    assert v["new_messages_since_last_week"] == 4
    assert v["new_ad_ids_since_last_week_OVERCOUNTS"] == 23
    assert len(v["new_ads"]) == 4 and v["new_ads"][0]["ad_ids_with_this_message"] == 20


def test_search_caveat_stays_off_an_ad_story():
    by = {x["id"]: x for x in window()}
    ads = sy.enrich({"signal_ids": ["a1", "a2", "a5", "s1"]}, by)
    assert "caveat" not in ads, "one Semrush row among ads is not a search story"
    search = sy.enrich({"signal_ids": ["s1", "a1"]}, by)
    assert "directional" in search["caveat"]


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
    assert p["market"]["status"] == "scored" and p["market"]["score"] is not None, \
        "a score from the first week"
    one = next(c for c in p["competitors"] if c["competitor"] == "Onelife")
    assert {e["kind"] for e in one["events"]} >= {"offer", "watch_term"}
    assert p["driver"]["competitor"] == "Onelife"


def test_score_says_what_it_measures():
    # Week of 21 Sep: organic social had 12 weeks of history, paid had none. Social is
    # scored on its own normal, paid against the set, and the reader is told which.
    per = {cid: {"metrics": {"ads_active": 30.0, "ads_launched": n, "social_posts": 5.0},
                 "events": []} for cid, n in (("c-vida", 8.0), ("c-mov", 1.0), ("c-one", 0.5))}
    hist = {cid: [{"social_posts": 2.0}] * 12 for cid in per}
    hist[None] = [{"social_posts": 6.0}] * 12
    p = sy.pressure_for_digest(mo.score_week(per, hist, COMPS))
    vida = next(c for c in p["competitors"] if c["competitor"] == "VIDA")
    assert vida["components"]["paid"]["basis"] == "set"
    assert vida["components"]["social"]["basis"] == "own"
    assert "ads_launched" in vida["scored_on"] and "ads_active" not in vida["scored_on"]
    cov = sy.pressure_coverage(mo.score_week(per, hist, COMPS))
    assert cov and cov[0].startswith("Paid scores compare") and "size alone" in cov[0], cov


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
    assert set(fr["sections"]) == {"overview", "search", "paid", "social", "owned"}
    assert "junk" not in fr["sections"]["paid"] and fr["sections"]["owned"]["website_changes"] == []
    assert fr["pressure"]["status"] == "scored"
    assert row["pressure_score"] == fr["pressure"]["score"] is not None
    assert fr["pressure"]["delta"] == row["pressure_score"] - 40
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


def test_single_signal_is_suppressed_not_held():
    one = json.loads(json.dumps(GOOD_ANALYSIS))
    one["developments"][0]["signal_ids"] = ["w2", "w2"]   # duplicates are one signal
    model, _ = fake_model(one, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    assert "VIDA" not in [d["competitor"] for d in row["developments"]]
    assert any("suppressed" in w for w in row["full_report"]["validation"]["warnings"])


def test_sections_are_gated():
    bad = json.loads(json.dumps(GOOD_ANALYSIS))
    bad["sections"]["owned"] = {"website_changes": [
        {"competitor": "Movement", "observation": "Price cut \u2014 big news.",
         "signal_ids": ["invented"]}]}
    model, _ = fake_model(bad, GOOD_STRATEGY)
    _, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=model)
    assert any("sections.owned" in f and "em dash" in f for f in rep.failures)
    assert any("sections.owned" in f and "not collected" in f for f in rep.failures)


def test_uncited_coverage_notes_are_dropped_not_held():
    # The first live run: the model wrote "email monitoring began after this week" as a
    # section item with no ids. Coverage already says it; the week must still publish.
    a = json.loads(json.dumps(GOOD_ANALYSIS))
    a["sections"]["owned"] = {
        "email_programs": [{"competitor": "all", "signal_ids": [],
                            "observation": "Email monitoring began after this week."}],
        "website_changes": [{"competitor": "unknown", "observation": "One change seen."}]}
    model, _ = fake_model(a, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    assert row["full_report"]["sections"]["owned"] == {
        "website_changes": [], "email_programs": [], "recommendations": []}
    assert sum("dropped uncited" in w for w in row["full_report"]["validation"]["warnings"]) == 2


def test_model_sees_short_refs_and_they_map_back():
    d = digest()
    aliased, real = sy.alias_digest(d)
    shown = sy.digest_signal_ids(aliased)
    assert shown and all(re.fullmatch(r"s\d+", x) for x in shown), shown
    assert set(real.values()) == sy.digest_signal_ids(d)
    ref = next(iter(shown))
    back = sy.unalias({"developments": [{"signal_ids": [ref, "s9999"]}]}, real)
    assert back["developments"][0]["signal_ids"] == [real[ref], "s9999"], \
        "a ref the model made up stays as written, so the gate fails it"


def test_near_miss_is_repaired_once():
    # The second live run: one word over a cap. The model gets the failure back.
    long = json.loads(json.dumps(GOOD_ANALYSIS))
    long["developments"][0]["so_what"] = " ".join(["word"] * 26)
    model, calls = fake_model([long, GOOD_ANALYSIS], GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    assert row["full_report"]["validation"]["attempts"] == 2
    assert sum("Scout Analyst" in c for c in calls) == 2


def test_repair_that_still_fails_is_held():
    long = json.loads(json.dumps(GOOD_ANALYSIS))
    long["developments"][0]["so_what"] = " ".join(["word"] * 26)
    model, calls = fake_model(long, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert not rep.ok and row["full_report"]["validation"]["attempts"] == 2
    assert sum("Scout Analyst" in c for c in calls) == 2, "exactly one repair, never a loop"


def test_prompt_states_every_gate_rule():
    g = sy.gate_rules("executive")
    for x in vb.BANNED_PHRASES + vb.GENERIC_OPENERS:
        assert x.strip() in g, x
    for k in ("summary", "headline", "so_what", "recommendation"):
        assert f"{k} {vb.CAPS[k]}" in g, k
    assert "en dash" in g and "\"not\"" in g and "\"low\"" in g
    # the examples the prompt holds up as right must themselves pass the gate
    for good in ["Movement kept its price and added yoga", "Sept 17 to 21",
                 "Movement opened a second Crystal City location"]:
        r = vb.Report(); vb.check_text(r, "x", good, 40)
        assert r.ok, (good, r.failures)
    for bad in ["Movement did not change price but added yoga", "Sept 17\u201321"]:
        r = vb.Report(); vb.check_text(r, "x", bad, 40)
        assert not r.ok, bad
    import executive_profile as ep
    for role in ("analyst", "strategist"):
        assert not re.search("[\u2014\u2013]", ep.profile_block("executive", role)), \
            "the model imitates the prompt; the prompt carries no dashes"


def test_low_confidence_is_suppressed_not_held():
    a = json.loads(json.dumps(GOOD_ANALYSIS))
    a["developments"][0]["confidence"] = "low"
    model, _ = fake_model(a, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    assert all(d.get("confidence") != "low" for d in row["developments"])


def test_long_recommendation_goes_back_to_the_strategist():
    long = json.loads(json.dumps(GOOD_STRATEGY))
    long["recommendations"][0]["recommendation"] = " ".join(["word"] * 31) + "."
    users = []
    answers = iter([long, GOOD_STRATEGY])

    def m(system, user, max_tokens, temperature):
        if "Scout Analyst" in system:
            return json.dumps(GOOD_ANALYSIS)
        users.append(user)
        return json.dumps(next(answers))

    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=m)
    assert rep.ok, rep.render()
    assert "FAILED REVIEW" in users[1] and "31 words" in users[1]


def test_tab_recommendations_are_cited_and_gated():
    # Jack, 24 Sep: the recommendations at the bottom of each tab are the dashboard's
    # most valuable use. The Strategist writes them from the tab findings, citing refs.
    seen = {}

    def m(system, user, max_tokens, temperature):
        if "Scout Analyst" in system:
            return json.dumps(GOOD_ANALYSIS)
        seen["user"] = user
        ref = re.search(r'"signal_ids": \[\s*"(s\d+)"', user.split("FINDINGS BY TAB")[1]).group(1)
        return json.dumps(dict(GOOD_STRATEGY, section_recommendations={
            "paid": [
                {"observation": "Onelife runs 266 ads.", "recommendation": "Keep launch spend on new prospects.",
                 "why": "Members already see acquisition ads.", "signal_ids": [ref]},
                {"observation": "Nothing cited.", "recommendation": "Do something.", "why": "Because."}],
            "search": []}))

    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=m)
    assert rep.ok, rep.render()
    assert "FINDINGS BY TAB" in seen["user"] and '"s' in seen["user"].split("FINDINGS BY TAB")[1]
    paid = row["full_report"]["sections"]["paid"]["recommendations"]
    assert len(paid) == 1 and paid[0]["signal_ids"] == ["a1"], "refs map back; uncited is dropped"
    assert any("dropped uncited paid.recommendations" in w for w in row["full_report"]["validation"]["warnings"])


def test_tab_recommendation_with_invented_ref_holds():
    def m(system, user, max_tokens, temperature):
        if "Scout Analyst" in system:
            return json.dumps(GOOD_ANALYSIS)
        return json.dumps(dict(GOOD_STRATEGY, section_recommendations={"social": [
            {"observation": "A post.", "recommendation": "Post more.", "why": "Reach.",
             "signal_ids": ["s9999"]}]}))
    _, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=m)
    assert not rep.ok and any("sections.social.recommendations" in f for f in rep.failures)


def test_campaign_needs_one_theme_on_two_channels():
    # Jack, 24 Sep: coordinated campaigns are a shared theme across channels, not only
    # activity. The model names the theme; code proves the channels from the signals.
    a = json.loads(json.dumps(GOOD_ANALYSIS))
    a["sections"]["overview"] = {"campaign_signals": [
        {"competitor": "Onelife", "theme": "$0 enrollment",
         "evidence": ["A $0 enrollment email.", "A Tenleytown ad."], "signal_ids": ["e1", "a1"]},
        {"competitor": "Onelife", "theme": "Ads everywhere",
         "evidence": ["Many ads on one message."], "signal_ids": ["a1", "a2"]}]}
    model, _ = fake_model(a, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    got = row["full_report"]["sections"]["overview"]["campaign_signals"]
    assert [c["theme"] for c in got] == ["$0 enrollment"], "two ads are one channel"
    assert got[0]["channels"] == ["email", "paid"] and got[0]["confidence"] == "medium"
    assert any("one channel" in w for w in row["full_report"]["validation"]["warnings"])


def test_promotions_get_channel_and_dates_from_their_signals():
    a = json.loads(json.dumps(GOOD_ANALYSIS))
    a["sections"]["overview"] = {"promotions": [
        {"competitor": "Onelife", "offer": "$0 enrollment", "signal_ids": ["e1", "a1"]},
        {"competitor": "VIDA", "offer": "Free first class", "channels": ["tv"], "first_seen": "1999-01-01"}]}
    model, _ = fake_model(a, GOOD_STRATEGY)
    row, rep = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                             pressure=pressure(), model=model)
    assert rep.ok, rep.render()
    promos = row["full_report"]["sections"]["overview"]["promotions"]
    assert len(promos) == 1, "an offer citing nothing is dropped"
    p = promos[0]
    assert p["channels"] == ["email", "paid"], "channels from the cited signals, never the model"
    assert p["first_seen"] == "2026-09-18" and p["last_seen"] == "2026-09-21"


def test_client_row_stays_out_of_pressure_and_digest():
    me = {"id": "c-bp", "name": "Bouldering Project", "single_market": False, "is_client": True}
    mine = [sig("bp1", "ad_active", "c-bp", scope="local",
                data={"body": "Columbia Heights is coming", "start_date": "2026-09-19"})]
    base = {"signals": window(), "competitors": COMPS, "ran": {"ads", "web", "email", "search"},
            "email_channels": EMAIL_CHANNELS, "watch_terms": ["columbia heights"],
            "social_channels": {}, "history": {}}
    alone, _ = sy.compute_pressure(dict(base), WK)
    withme, _ = sy.compute_pressure(dict(base, client_self=me, self_signals=mine,
                                         self_social_channels={}), WK)
    assert alone["market"] == withme["market"] and alone["competitors"] == withme["competitors"]
    assert alone["driver"] == withme["driver"]
    b = withme["benchmark"]
    assert b["competitor"] == "Bouldering Project" and b["event_points"] == 0, \
        "its own Columbia Heights posts are not a watch-term event"
    assert "c-bp" not in json.dumps(sy.pressure_for_digest(withme)), "the Analyst never sees it"
    rows = sy.pressure_rows("cl", WK, withme)
    assert any(r["competitor_id"] == "c-bp" for r in rows), "stored for the dashboard"


def test_source_url_is_never_the_models():
    a = json.loads(json.dumps(GOOD_ANALYSIS))
    a["developments"][2]["source_url"] = "https://movementgyms.com/made-up"
    model, _ = fake_model(a, GOOD_STRATEGY)
    row, _ = sy.synthesize(client=CLIENT, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=model)
    mov = next(d for d in row["developments"] if d["competitor"] == "Movement")
    assert mov["source_url"] == "https://example.com/w1"


def test_client_never_in_writing_terms():
    leak = json.loads(json.dumps(GOOD_STRATEGY))
    leak["recommendations"][0]["recommendation"] = "Draw members away from Eckington early."
    model, _ = fake_model(GOOD_ANALYSIS, leak)
    c = dict(CLIENT, config={"never_in_writing": ["eckington"]})
    _, rep = sy.synthesize(client=c, digest=digest(), signals=window(), prior_score=None,
                           pressure=pressure(), model=model)
    assert not rep.ok and any("eckington" in f for f in rep.failures)


def test_http_model_call():
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    calls, waits = [], []

    class R:
        def __init__(self, code, data=None):
            self.status_code, self._d, self.text = code, data, "err"
        def json(self):
            return self._d

    replies = [R(529), R(200, {"stop_reason": "end_turn",
                               "content": [{"type": "text", "text": '{"ok": 1}'}]})]

    def post(url, headers, json, timeout):
        calls.append(json)
        return replies.pop(0)

    out = sy.anthropic_model("sys", "user", 100, 0.1, post=post, sleep=waits.append)
    assert out == '{"ok": 1}' and len(calls) == 2 and waits, "retries an overloaded API"
    assert "temperature" not in calls[0], "the argument that broke the first live run"
    assert calls[0]["model"] == sy.MODEL and calls[0]["system"] == "sys"
    try:
        sy.anthropic_model("s", "u", 1, 0, post=lambda *a, **k: R(200, {
            "stop_reason": "max_tokens", "content": []}), sleep=waits.append)
        raise AssertionError("a truncated reply must not be parsed")
    except RuntimeError:
        pass
    try:
        sy.anthropic_model("s", "u", 1, 0, post=lambda *a, **k: R(400), sleep=waits.append)
        raise AssertionError("a 400 must raise, not return empty text")
    except RuntimeError:
        pass


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
