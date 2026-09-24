#!/usr/bin/env python3
"""
Fixtures for social_platforms.py and collect_social.build_rows(). Item shapes follow
each actor's published output schema (Apify, 24 Sep 2026). The internal collector's
four data bugs are pinned here as regressions: followers read from the wrong field,
a cap reported as a count, a 30-day window on a weekly run, and failure returning [].

    python3 test_social.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone

import collect_social as cs
import momentum as mo
import social_platforms as sp
from geo import GeoClassifier

WK = date(2026, 9, 21)                     # briefing week; window is 14 Sep - 21 Sep
IN = "2026-09-17T15:00:00.000Z"
BEFORE = "2026-09-10T15:00:00.000Z"
AFTER = "2026-09-21T09:00:00.000Z"          # Monday morning: next week's, not this one's

CH = {
    "ig": sp.Channel("ch-ig", "c-one", "instagram", "local", "onelifetenley",
                     location_label="Tenleytown"),
    "igdmv": sp.Channel("ch-igdmv", "c-mov", "instagram", "regional", "movementgymsdmv",
                        location_label="DMV"),
    "fb": sp.Channel("ch-fb", "c-ymca", "facebook", "local", "YMCABowen", "289948114406820",
                     location_label="Anthony Bowen"),
    "tt": sp.Channel("ch-tt", "c-one", "tiktok", "national", "onelifefit"),
    "yt": sp.Channel("ch-yt", "c-mov", "youtube", "national", None, "UCykbSnimHBWsvq-ndDkCzpQ"),
    "x": sp.Channel("ch-x", "c-vida", "x", "national", "VIDAFitnessDC"),
}
COMPS = [
    {"id": "c-one", "name": "Onelife", "domain": "onelifefitness.com", "single_market": False},
    {"id": "c-mov", "name": "Movement", "domain": "movementgyms.com", "single_market": False},
    {"id": "c-ymca", "name": "YMCA", "domain": "ymcadc.org", "single_market": True},
    {"id": "c-vida", "name": "VIDA", "domain": "vidafitness.com", "single_market": True},
]


def ig(pid, ts, caption="", owner="onelifetenley", **k):
    return {"id": pid, "shortCode": pid, "timestamp": ts, "caption": caption,
            "likesCount": 10, "commentsCount": 2, "url": f"https://instagram.com/p/{pid}",
            "ownerUsername": owner, "type": "Image", **k}


# ── time ─────────────────────────────────────────────────────────────────────

def test_parse_time_every_shape():
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert sp.parse_time("2026-09-17T15:00:00.000Z").day == 17
    assert sp.parse_time("2026-09-17T15:00:00").tzinfo is not None
    assert sp.parse_time(1789657200).year == 2026
    assert sp.parse_time(1789657200000).year == 2026, "epoch millis"
    assert sp.parse_time("Thu Sep 17 15:00:00 +0000 2026").day == 17, "X's format"
    assert sp.parse_time("3 days ago", now).day == 18, "YouTube's relative dates"
    assert sp.parse_time("") is None and sp.parse_time("soon") is None


def test_window_is_the_week_before_and_does_not_overlap():
    a, b = sp.window(WK)
    assert (a.date(), b.date()) == (date(2026, 9, 14), date(2026, 9, 21))
    assert sp.window(date(2026, 9, 28))[0] == b, "next week starts where this one ends"


# ── payloads ─────────────────────────────────────────────────────────────────

def test_payloads_match_actor_schemas():
    p = sp.payload("instagram", [CH["ig"]], WK)
    assert p["directUrls"] == ["https://www.instagram.com/onelifetenley/"]
    assert p["onlyPostsNewerThan"] == "2026-09-14" and p["resultsType"] == "posts"
    assert sp.payload("facebook", [CH["fb"]], WK)["startUrls"] == [
        {"url": "https://www.facebook.com/YMCABowen"}]
    assert sp.payload("tiktok", [CH["tt"]], WK)["profiles"] == ["onelifefit"]
    yt = sp.payload("youtube", [CH["yt"]], WK)
    assert yt["startUrls"][0]["url"].endswith("/channel/UCykbSnimHBWsvq-ndDkCzpQ/videos")
    x = sp.payload("x", [CH["x"]], WK)
    assert x["searchTerms"] == ["from:VIDAFitnessDC since:2026-09-14 until:2026-09-21"], \
        "start/end are ignored for twitterHandles; the backfill paid for 650 May tweets"
    assert sp.payload("tiktok", [CH["tt"]], WK)["resultsPerPage"] == 20


# ── normalizers: the internal tool's follower bugs ──────────────────────────

def test_tiktok_followers_from_authormeta():
    p = sp.NORMALIZE["tiktok"]({"id": "7", "createTimeISO": IN, "text": "x",
                                "authorMeta": {"name": "onelifefit", "fans": 1818},
                                "playCount": 40, "diggCount": 3})
    assert p.followers == 1818, "internal read profile.fans off a 'user' item that never exists"
    assert p.views == 40 and p.likes == 3


def test_youtube_subscribers_and_relative_date():
    p = sp.NORMALIZE["youtube"]({"id": "v1", "date": "2026-09-16T00:00:00.000Z",
                                 "title": "Bouldering in Crystal City", "text": "desc",
                                 "numberOfSubscribers": 3120, "viewCount": 90,
                                 "channelId": "UCykbSnimHBWsvq-ndDkCzpQ", "type": "video"})
    assert p.followers == 3120, "internal read channelSubscriberCount, which does not exist"
    assert "Crystal City" in p.text


def test_facebook_and_x_normalize():
    f = sp.NORMALIZE["facebook"]({"postId": "p1", "time": IN, "text": "Open house Saturday",
                                  "likes": 5, "comments": 1, "shares": 2,
                                  "url": "https://facebook.com/p1",
                                  "inputUrl": "https://www.facebook.com/YMCABowen"})
    assert f.followers is None, "Facebook posts carry no follower count; unknown, not zero"
    x = sp.NORMALIZE["x"]({"id": "t1", "createdAt": "Thu Sep 17 15:00:00 +0000 2026",
                           "fullText": "U Street classes", "likeCount": 4, "isRetweet": True,
                           "author": {"userName": "VIDAFitnessDC", "followers": 900}})
    assert x.is_repost and x.followers == 900
    assert sp.NORMALIZE["instagram"]({"error": "not_found"}) is None


def test_matching_never_attributes_a_stranger():
    chans = [CH["ig"], CH["igdmv"]]
    a = sp.NORMALIZE["instagram"](ig("1", IN, owner="movementgymsdmv"))
    b = sp.NORMALIZE["instagram"](ig("2", IN, owner="someone_else"))
    assert sp.match(a, chans) is CH["igdmv"]
    assert sp.match(b, chans) is None
    c = sp.NORMALIZE["youtube"]({"id": "v", "date": IN, "channelId": None,
                                 "input": "https://www.youtube.com/channel/UCykbSnimHBWsvq-ndDkCzpQ/videos"})
    assert sp.match(c, [CH["yt"], CH["ig"]]) is CH["yt"]


def test_instagram_followers_from_details():
    out = sp.followers_from_details([{"username": "onelifetenley", "followersCount": 1073}],
                                    [CH["ig"]])
    assert out == {"ch-ig": 1073}


# ── build_rows: window, cap, zero, geography ────────────────────────────────

def rows(items_by_channel, followers=None):
    posts, returned = {}, {}
    for cid, items in items_by_channel.items():
        norm = [sp.NORMALIZE[CH[cid].platform](i) for i in items]
        posts[CH[cid].id] = [p for p in norm if p]
        returned[CH[cid].id] = len(posts[CH[cid].id])
    collected = [CH[c] for c in items_by_channel]
    return cs.build_rows(posts, collected, followers or {}, COMPS, "cl", WK, returned,
                         GeoClassifier())


def test_only_this_week_and_no_duplicates():
    p, prof = rows({"ig": [ig("1", IN), ig("2", BEFORE), ig("3", AFTER), ig("1", IN)]})
    assert [r["external_ref"] for r in p] == ["instagram:1"]
    d = prof[0]["data"]
    assert d["posts_in_week"] == 1 and not d["capped"]


def test_cap_is_a_floor_not_a_count():
    many = [ig(str(i), IN) for i in range(sp.LIMIT)]
    _, prof = rows({"ig": many})
    assert prof[0]["data"]["capped"] is True, "internal reported the limit (20) as a count"
    # TikTok always returns LIMIT; if the oldest is before the window, nothing was cut.
    tt = [{"id": str(i), "createTimeISO": IN if i < 5 else BEFORE, "authorMeta": {"name": "onelifefit"}}
          for i in range(sp.limit_for(1, "tiktok"))]
    _, prof = rows({"tt": tt})
    assert prof[0]["data"]["capped"] is False and prof[0]["data"]["posts_in_week"] == 5


def test_age_restricted_profile_is_unknown_not_zero():
    item = {"inputUrl": "https://www.instagram.com/ymcabowen/", "username": "ymcabowen",
            "private": False, "error": "Restricted profile", "isRestrictedProfile": True,
            "restrictionReason": "You must be 13 years old or over to see this profile"}
    ymca = sp.Channel("ch-yig", "c-ymca", "instagram", "local", "ymcabowen")
    assert "13 years old" in sp.item_error(item)
    assert sp.match_error(item, [CH["ig"], ymca]) is ymca
    assert sp.NORMALIZE["instagram"](item) is None
    calls = []
    def fake(token, actor, payload, **k):
        calls.append(payload.get("resultsType"))
        return [item] if payload.get("resultsType") == "posts" else []
    real = cs.apify.run_actor
    cs.apify.run_actor = fake
    try:
        posts, collected, _, _ = cs.collect("t", [CH["ig"], ymca], WK)
    finally:
        cs.apify.run_actor = real
    assert ymca not in collected, "an unreadable channel must get no profile row"
    assert CH["ig"] in collected, "a readable channel beside it still counts, as a real zero"
    assert sp.item_error({"noResults": True, "error": "x"}) is None, "X noResults is a real zero"


def test_zero_posts_still_writes_a_profile():
    p, prof = rows({"tt": [{"id": "9", "createTimeISO": "2024-07-01T00:00:00Z",
                            "authorMeta": {"name": "onelifefit", "fans": 1818}}]})
    assert p == [] and prof[0]["data"]["posts_in_week"] == 0
    assert prof[0]["data"]["followers"] == 1818


def test_posts_are_geo_classified():
    p, prof = rows({"ig": [ig("1", IN, caption="New HIIT class at Tenleytown this Saturday")]})
    assert p[0]["source_scope"] == "local"
    assert p[0]["geo_relevance"] in ("dc_explicit", "dc_landing"), p[0]["geo_relevance"]
    assert prof[0]["data"]["dc_referencing_posts"] == 1


# ── momentum: social is weighted by where the account and the post are ──────

def test_momentum_social_weights():
    sig = [
        {"id": "1", "signal_type": "social_post", "competitor_id": "c-one",
         "source_scope": "local", "geo_relevance": "none", "data": {}},
        {"id": "2", "signal_type": "social_post", "competitor_id": "c-mov",
         "source_scope": "regional", "geo_relevance": "none", "data": {}},
        {"id": "3", "signal_type": "social_post", "competitor_id": "c-mov",
         "source_scope": "regional", "geo_relevance": "dc_explicit", "data": {}},
        {"id": "4", "signal_type": "social_profile", "competitor_id": "c-vida",
         "source_scope": "national", "geo_relevance": "none", "data": {}},
    ]
    m = mo.week_metrics(sig, COMPS, WK, ran={"social"}, email_channels=set())
    assert m["c-one"]["metrics"]["social_posts"] == 1.0, "location account counts in full"
    assert m["c-mov"]["metrics"]["social_posts"] == 1.5, "DMV feed 0.5, a DC post on it 1.0"
    assert m["c-vida"]["metrics"]["social_posts"] == 0.0, "collected, posted nothing: a real zero"
    assert "social_posts" not in m["c-ymca"]["metrics"], "not collected: unknown"


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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
