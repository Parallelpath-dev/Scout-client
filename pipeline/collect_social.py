#!/usr/bin/env python3
"""
Scout — organic social collector for the client portal.

    python3 collect_social.py --client bouldering-project
    python3 collect_social.py --client bouldering-project --dry-run
    python3 collect_social.py --client bouldering-project --week 2026-09-21
    python3 collect_social.py --client bouldering-project --platform instagram

Reads portal.channels (purpose = organic_social), runs one Apify actor per platform for
every channel on it, and writes two kinds of signal, both filed under the briefing week:

  social_post     one per post published in [W-7, W), classified for geography with
                  geo.py like every ad, page and email. external_ref = platform:post_id.
  social_profile  one per channel that was collected, even with zero posts. It is the
                  proof the collector ran: a channel with a profile row and no posts
                  posted nothing, a channel with no profile row is unknown. momentum.py
                  depends on that distinction.

WHICH ACCOUNT, AND WHY
----------------------
Organic is monitored at the location account wherever a live one exists (onboarding
brief, section 2): that is where ground-level, DC-specific content is published. Paid
is monitored at the corporate page, because teams run centralised ad accounts and no
local page in this set runs a Meta ad. The channel's `scope` records which it is, and
travels on every row as source_scope, so a regional DMV feed is never read as local.

One actor failing costs that platform's rows for the week, not the run: the other
platforms still write, and the failed channels get no profile row, so they read as
unknown rather than silent.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import apify
import social_platforms as sp
from geo import GeoClassifier
from supa import Supa
from week_window import parse_week

TEXT_CAP = 1500


def load_channels(sb: Supa, client_id: str, platform: str | None) -> tuple[list[dict], list[sp.Channel]]:
    competitors = sb.get("portal", "competitors", {
        "client_id": f"eq.{client_id}", "active": "eq.true",
        "select": "id,name,domain,single_market"})
    ids = ",".join(c["id"] for c in competitors)
    if not ids:
        return competitors, []
    params = {"competitor_id": f"in.({ids})", "purpose": "eq.organic_social",
              "active": "eq.true",
              "select": "id,competitor_id,platform,scope,handle,external_id,url,location_label"}
    if platform:
        params["platform"] = f"eq.{platform}"
    rows = sb.get("portal", "channels", params)
    chans = []
    for r in rows:
        if r["platform"] not in sp.ACTORS:
            continue
        needs = "external_id" if r["platform"] == "youtube" else "handle"
        if not r.get(needs) and not (r["platform"] == "facebook" and r.get("external_id")):
            print(f"  WARNING {r['platform']} channel {r['id']} has no {needs}; skipped")
            continue
        chans.append(sp.Channel(id=r["id"], competitor_id=r["competitor_id"],
                                platform=r["platform"], scope=r["scope"],
                                handle=r.get("handle"), external_id=r.get("external_id"),
                                url=r.get("url"), location_label=r.get("location_label")))
    return competitors, chans


def build_rows(
    posts_by_channel: dict[str, list[sp.Post]],
    collected: list[sp.Channel],
    followers: dict[str, int],
    competitors: list[dict[str, Any]],
    client_id: str,
    wk,
    returned: dict[str, int],
    clf: GeoClassifier,
    *,
    run_weeks: int = 1,
    run_until=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Posts inside week wk become social_post rows; every collected channel gets a
    social_profile row. Pure apart from the classifier, so it is tested directly.

    run_weeks / run_until describe the actor run the posts came from, when one run
    covered several weeks (a backfill). A channel that hit the run's limit has its
    oldest weeks cut off, so a week older than its oldest returned post is left out
    for that channel: unknown, not zero.
    """
    since, until = sp.window(wk)
    run_since = sp.window(run_until or wk, run_weeks)[0]
    limit = sp.limit_for(run_weeks)
    comp = {c["id"]: c for c in competitors}
    now = datetime.now(timezone.utc).isoformat()
    post_rows, profile_rows = [], []

    for ch in collected:
        c = comp.get(ch.competitor_id, {})
        allp = posts_by_channel.get(ch.id, [])
        oldest = min((p.posted_at for p in allp), default=None)
        hit_limit = returned.get(ch.id, 0) >= limit and oldest is not None and oldest >= run_since
        if hit_limit and run_weeks > 1 and oldest > since:
            continue
        seen: set[str] = set()
        in_window: list[sp.Post] = []
        for p in posts_by_channel.get(ch.id, []):
            if p.post_id in seen or not (since <= p.posted_at < until):
                continue
            # A pinned post is the same post every week. It was published once.
            if p.is_pinned and p.posted_at < since:
                continue
            seen.add(p.post_id)
            in_window.append(p)

        for p in in_window:
            g = clf.classify(text_fields={"text": p.text[:TEXT_CAP]} if p.text else {},
                             urls=[], competitor_domain=c.get("domain"))
            post_rows.append({
                "client_id": client_id, "competitor_id": ch.competitor_id,
                "channel_id": ch.id, "source_scope": ch.scope,
                "geo_relevance": g.relevance, "geo_evidence": g.evidence,
                "signal_type": "social_post",
                "external_ref": f"{ch.platform}:{p.post_id}",
                "week_of": wk.isoformat(),
                "observed_at": p.posted_at.isoformat(),
                "source_url": p.url,
                "collected_at": now,
                "data": {
                    "platform": ch.platform, "handle": ch.handle or ch.external_id,
                    "location_label": ch.location_label,
                    "text": p.text[:TEXT_CAP], "posted_at": p.posted_at.isoformat(),
                    "likes": p.likes, "comments": p.comments, "shares": p.shares,
                    "views": p.views, "format": p.format, "is_repost": p.is_repost,
                },
            })

        n_returned = returned.get(ch.id, 0)
        fol = followers.get(ch.id)
        if fol is None:
            fol = next((p.followers for p in posts_by_channel.get(ch.id, []) if p.followers), None)
        eng = [sum(x or 0 for x in (p.likes, p.comments, p.shares)) for p in in_window]
        profile_rows.append({
            "client_id": client_id, "competitor_id": ch.competitor_id,
            "channel_id": ch.id, "source_scope": ch.scope,
            "geo_relevance": "none", "geo_evidence": None,
            "signal_type": "social_profile",
            "external_ref": f"profile:{ch.id}",
            "week_of": wk.isoformat(), "source_url": ch.page_url, "collected_at": now,
            "data": {
                "platform": ch.platform, "handle": ch.handle or ch.external_id,
                "location_label": ch.location_label, "scope": ch.scope,
                "followers": fol,
                "posts_in_week": len(in_window),
                "items_returned": n_returned,
                # Asked for LIMIT, got LIMIT, and even the oldest item returned is inside
                # the window: there may be more. The count is a floor and says so,
                # rather than a cap reported as a count. (TikTok always returns LIMIT,
                # because its date filter is paid; its oldest item is what decides.)
                "capped": hit_limit and (run_weeks == 1 or oldest >= since),
                "avg_engagement": round(sum(eng) / len(eng), 1) if eng else None,
                "dc_referencing_posts": sum(
                    1 for r in post_rows if r["channel_id"] == ch.id
                    and r["geo_relevance"] in ("dc_landing", "dc_explicit")),
                "latest_post_at": max((p.posted_at for p in posts_by_channel.get(ch.id, [])),
                                      default=None).isoformat()
                                  if posts_by_channel.get(ch.id) else None,
            },
        })
    return post_rows, profile_rows


def collect(token: str, channels: list[sp.Channel], wk, weeks: int = 1) -> tuple[
        dict[str, list[sp.Post]], list[sp.Channel], dict[str, int], dict[str, int]]:
    by_platform: dict[str, list[sp.Channel]] = defaultdict(list)
    for c in channels:
        by_platform[c.platform].append(c)

    posts: dict[str, list[sp.Post]] = defaultdict(list)
    returned: dict[str, int] = defaultdict(int)
    followers: dict[str, int] = {}
    collected: list[sp.Channel] = []

    for platform, chans in sorted(by_platform.items()):
        print(f"\n  {platform}: {len(chans)} channel(s)")
        # X's maxItems is one budget for the whole run, so one busy handle could starve
        # the rest into a false zero. One run per handle there; one per platform elsewhere.
        groups = [[c] for c in chans] if platform == "x" else [chans]
        items, ok = [], []
        for g in groups:
            try:
                items += apify.run_actor(token, sp.ACTORS[platform],
                                         sp.payload(platform, g, wk, weeks))
                ok += g
            except Exception as e:  # noqa: BLE001 — one platform's failure is not the run's
                print(f"  ERROR {platform} {[c.handle for c in g]}: {e}. "
                      f"These channels read as unknown this week.")
        chans = ok
        if not chans:
            continue
        norm = sp.NORMALIZE[platform]
        unmatched = 0
        for it in items:
            p = norm(it)
            if not p:
                continue
            ch = sp.match(p, chans)
            if not ch:
                unmatched += 1
                continue
            posts[ch.id].append(p)
            returned[ch.id] += 1
        if unmatched:
            print(f"  {unmatched} {platform} item(s) matched no channel and were dropped")
        collected.extend(chans)

        if platform == "instagram":
            try:
                det = apify.run_actor(token, sp.ACTORS[platform], sp.details_payload(chans))
                followers.update(sp.followers_from_details(det, chans))
            except Exception as e:  # noqa: BLE001 — followers are context, not the count
                print(f"  WARNING instagram details failed ({e}); followers unknown")
    return posts, collected, followers, returned


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--week", help="ISO date inside the briefing week. Default this week.")
    ap.add_argument("--platform", choices=sorted(sp.ACTORS), help="collect one platform only")
    ap.add_argument("--dry-run", action="store_true", help="collect and print, write nothing")
    ap.add_argument("--backfill-weeks", type=int, default=0, metavar="N",
                    help="also write the N weeks before --week, from one run per platform, "
                         "so pressure has a social baseline from the first briefing")
    args = ap.parse_args()

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    token = os.environ.get("APIFY_TOKEN")
    if not url or not key or not token:
        print("SUPABASE_URL, SUPABASE_SERVICE_KEY and APIFY_TOKEN must be set", file=sys.stderr)
        return 2
    sb = Supa(url, key)
    clients = sb.get("public", "clients", {"slug": f"eq.{args.client}", "select": "id,name"})
    if not clients:
        print(f"no client with slug {args.client}", file=sys.stderr)
        return 2
    client = clients[0]
    wk = parse_week(args.week)
    since, until = sp.window(wk)
    competitors, channels = load_channels(sb, client["id"], args.platform)
    print(f"client: {client['name']} · week of {wk} · posts from {since:%Y-%m-%d} to "
          f"{until:%Y-%m-%d} · {len(channels)} channels")
    if not channels:
        print("no organic_social channels", file=sys.stderr)
        return 2

    weeks = 1 + max(0, args.backfill_weeks)
    posts, collected, followers, returned = collect(token, channels, wk, weeks)
    clf = GeoClassifier()
    post_rows, profile_rows = build_rows(posts, collected, followers, competitors,
                                         client["id"], wk, returned, clf,
                                         run_weeks=weeks, run_until=wk)
    back_posts, back_profiles = [], []
    from datetime import timedelta
    for i in range(1, weeks):
        w = wk - timedelta(weeks=i)
        bp, bf = build_rows(posts, collected, {}, competitors, client["id"], w, returned,
                            clf, run_weeks=weeks, run_until=wk)
        # Followers are today's number. On a past week it would be a fabricated history.
        for r in bf:
            r["data"]["followers"] = None
            r["data"]["backfilled"] = True
        back_posts += bp
        back_profiles += bf
    if weeks > 1:
        print(f"  backfill: {len(back_posts)} posts across {weeks - 1} earlier weeks")

    name = {c["id"]: c["name"] for c in competitors}
    print(f"\n  {'competitor':<10} {'platform':<9} {'scope':<9} {'where':<14} "
          f"{'posts':>5} {'dc':>3} {'followers':>9}  note")
    for r in sorted(profile_rows, key=lambda r: (name.get(r["competitor_id"], ""), r["data"]["platform"])):
        d = r["data"]
        note = "CAPPED, count is a floor" if d["capped"] else ""
        print(f"  {name.get(r['competitor_id'], '?'):<10} {d['platform']:<9} {d['scope']:<9} "
              f"{(d['location_label'] or '-')[:14]:<14} {d['posts_in_week']:>5} "
              f"{d['dc_referencing_posts']:>3} {str(d['followers'] or '?'):>9}  {note}")
    missing = [c for c in channels if c not in collected]
    if missing:
        print(f"\n  NOT collected (actor failed): "
              f"{', '.join(f'{name.get(c.competitor_id)} {c.platform}' for c in missing)}")

    if args.dry_run:
        print("\n  dry run, nothing written")
        return 0
    n1 = sb.upsert("portal", "signals", post_rows + back_posts,
                   on_conflict="competitor_id,signal_type,external_ref,week_of")
    n2 = sb.upsert("portal", "signals", profile_rows + back_profiles,
                   on_conflict="competitor_id,signal_type,external_ref,week_of")
    print(f"\n  wrote {n1} posts, {n2} channel profiles")
    # Every platform failing is a failed run. Some failing is a partial week, logged above.
    return 0 if collected else 1


if __name__ == "__main__":
    sys.exit(main())
