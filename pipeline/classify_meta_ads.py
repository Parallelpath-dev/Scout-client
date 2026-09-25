#!/usr/bin/env python3
"""
Scout — Meta ads collector.

Pulls every active ad from each competitor's national Facebook page, classifies each one
for geographic reference, writes the ads to `portal.signals` and the week's rollup to
`portal.ad_geo_weekly`.

WHY THE NATIONAL PAGE
---------------------
Verified 11 Sep 2026: not one local page in this competitive set runs a single Meta ad.
Every ad these brands run is published from the corporate page. So `source_scope` on every
row this collector writes is 'national', and the interesting question is not where the ad
came from but whether its creative references this market. That is `geo_relevance`, and it
is an independent field for exactly this reason.

WHAT THE NUMBER MEANS
---------------------
`dc_referencing` is dc_landing + dc_explicit. It is a FLOOR on local activity. The Meta Ad
Library exposes no targeting, audience location, DMA or delivery region for commercial ads
anywhere, so "ads targeted at DC" is not a measurable quantity and must never be reported
as one. An ad classified `none` may be their heaviest DC spend.

There is deliberately no per-market average anywhere in this file. Dividing 367 national
ads by 7 locations produces a number with no basis in anything observable, and
`portal.ad_geo_weekly` has no column to put one in.

WHERE THE ADS COME FROM
-----------------------
This pipeline does its own scrape. It does not read from the internal tool, and the
internal tool does not know Bouldering Project's competitors exist.

That independence is the point. Bouldering Project's competitor config lives in
`portal.competitors` and `portal.channels`, not in `public.clients.config`, so
`weekly_scout.yml` never picks these pages up and the same five pages are never pulled
twice. A cap, a new competitor or a retired channel is a change in this repo alone.

Two things the internal collector does that we deliberately do not:

  - It truncates. `body_text[:500]` and `creative_assets[:10]`. For a classifier reading
    ad copy for place names, a 500-character clip drops geography mentioned late in a
    long body and the ad silently classifies as `none`.
  - It caps every page at 35 ads, sorted by impressions. For a competitor running 367,
    that describes their biggest campaigns rather than their market.

`--source internal` still exists and reads what the weekly pipeline collected. It is
there for the day Bouldering Project is also an internal client, where re-scraping the
same pages WOULD be paying twice. It is not the default and it inherits both limitations
above.

USAGE
-----
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...      # service role: bypasses RLS, never ship to a client

    python3 collect_meta_ads.py --client bouldering-project --dry-run
    python3 collect_meta_ads.py --client bouldering-project
    python3 collect_meta_ads.py --client bouldering-project --week 2026-09-28

    # fallback only, spends Apify credit:
    export APIFY_TOKEN=...
    python3 collect_meta_ads.py --client bouldering-project --source apify
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import requests

import apify
from geo import GeoClassifier, campaign_scope_hint, extract_ad_fields, is_recruitment
from supa import Supa, only

ACTOR = "apify~facebook-ads-scraper"

# The Ad Library URL that actually works. The two things that break it silently:
#   - view_all_page_id must be the CLASSIC page id, not the profile-style id starting 1000.
#     A profile id returns an empty result rather than an error.
#   - search_type=page, or it searches ad text instead of the advertiser.
AD_LIBRARY_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=US&is_targeted_country=false"
    "&media_type=all&search_type=page"
    "&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    "&view_all_page_id={page_id}"
)

# Used only when a channel has no max_ads set, which is the normal case. This is a
# runaway guard, not a sampling policy: Onelife was the largest page in this set at 367
# active ads on 11 Sep, so anything approaching this number means something changed
# upstream rather than a competitor tripling their spend overnight.
RESULTS_LIMIT_PER_PAGE = 800


# ── Apify ───────────────────────────────────────────────────────────────────


def run_actor(
    token: str,
    page_ids: list[str],
    caps: dict[str, int | None] | None = None,
    timeout_s: int = 1800,
) -> list[dict[str, Any]]:
    """Start the scraper, wait for it, return the dataset items.

    caps maps page id to its max_ads. A page with no cap takes the whole page, which is
    what we want for this client: the geographic mix of everything they are running is
    the product, and a capped pull is sorted by impressions rather than sampled.

    The actor takes one resultsLimit for the whole run rather than per URL, so a mixed
    set of caps runs as separate calls. In practice every page here is uncapped and this
    is a single call.
    """
    caps = caps or {}
    groups: dict[int, list[str]] = {}
    for pid in page_ids:
        groups.setdefault(caps.get(pid) or RESULTS_LIMIT_PER_PAGE, []).append(pid)

    items: list[dict[str, Any]] = []
    for limit, pids in groups.items():
        items.extend(_run_one(token, pids, limit, timeout_s))
    return items


def _run_one(
    token: str, page_ids: list[str], results_limit: int, timeout_s: int
) -> list[dict[str, Any]]:
    payload = {
        "startUrls": [{"url": AD_LIBRARY_URL.format(page_id=pid)} for pid in page_ids],
        "resultsLimit": results_limit,
        "activeStatus": "active",
    }
    # Start, poll and page through the dataset in apify.py, shared with the social
    # collector. A failed run raises there rather than returning an empty list.
    return apify.run_actor(token, ACTOR, payload, timeout_s=timeout_s)


# ── shaping ─────────────────────────────────────────────────────────────────


def tally_ids(rollup: list[dict[str, Any]]) -> list[str]:
    """Competitor ids present in this week's rollup."""
    return [r["competitor_id"] for r in rollup]


def week_of(d: date | None = None) -> date:
    """Monday of the collection week. One row per competitor per week, keyed on this."""
    d = d or datetime.now(timezone.utc).date()
    return d - timedelta(days=d.weekday())


# Both shapes again: `page_id` / `ad_archive_id` in the internal pipeline's normalized
# form, `pageID` / `adArchiveID` straight off the actor.
def page_id_of(ad: dict[str, Any]) -> str | None:
    for key in ("page_id", "pageID", "pageId"):
        v = ad.get(key)
        if v:
            return str(v)
    snap = ad.get("snapshot") or {}
    v = snap.get("pageId")
    return str(v) if v else None


def archive_id_of(ad: dict[str, Any]) -> str | None:
    for key in ("ad_archive_id", "adArchiveID", "adArchiveId"):
        v = ad.get(key)
        if v:
            return str(v)
    return None


def flatten(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The actor returns either ads directly or a wrapper carrying `results`.

    Which one depends on the input shape and the build. Handling both is three lines here
    and a silently empty week if it is left out.
    """
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # The internal pipeline stores one signal row per competitor with the ads under
        # `data.ads`. The actor returns either ads directly or a wrapper with `results`.
        for wrapper_key in ("ads", "results"):
            if isinstance(it.get(wrapper_key), list):
                out.extend(x for x in it[wrapper_key] if isinstance(x, dict))
                break
        else:
            if it.get("snapshot") or archive_id_of(it):
                out.append(it)
    return out


def build_rows(
    ads: list[dict[str, Any]],
    competitors: list[dict[str, Any]],
    channels_by_page: dict[str, dict[str, Any]],
    client_id: str,
    clf: GeoClassifier,
    totals_by_comp: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Classify every ad and produce (signal rows, weekly rollup rows, unmatched counts).

    totals_by_comp carries the Ad Library's own reported total per competitor, which is a
    different number from how many ads were pulled. Keeping them apart is the point of
    migration 003: the internal collector caps pages at 35 and the capped figure was
    previously stored under a column named `total_active`.
    """
    totals_by_comp = totals_by_comp or {}
    comp_by_id = {c["id"]: c for c in competitors}
    # A brand can publish from more than one page: Bouldering Project runs ads from its
    # corporate page and its DC location page. Every page maps back to its brand.
    by_page: dict[str, dict[str, Any]] = {
        pid: comp_by_id[ch["competitor_id"]]
        for pid, ch in channels_by_page.items() if ch.get("competitor_id") in comp_by_id}

    wk = week_of()
    collected_at = datetime.now(timezone.utc).isoformat()

    signals: list[dict[str, Any]] = []
    tally: dict[str, dict[str, int]] = {}
    unmatched: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()

    for ad in ads:
        pid = page_id_of(ad)
        comp = by_page.get(pid or "")
        if not comp:
            # A page id we are not monitoring. Counting it would put a stranger's ads in
            # the client's number, so it is dropped and reported at the end.
            unmatched[pid or "unknown"] = unmatched.get(pid or "unknown", 0) + 1
            continue

        archive_id = archive_id_of(ad)
        if not archive_id:
            continue
        key = (comp["id"], archive_id)
        if key in seen:
            # The library returns collation groups; the same creative can appear more than
            # once in a single pull. Counting it twice inflates every number downstream.
            continue
        seen.add(key)

        text_fields, urls = extract_ad_fields(ad)
        verdict = clf.classify(
            text_fields=text_fields, urls=urls, competitor_domain=comp.get("domain")
        )

        snap = ad.get("snapshot") or {}
        body = text_fields.get("body", "")
        recruitment = is_recruitment(text_fields, urls)
        scope_hint = campaign_scope_hint(urls)
        signals.append(
            {
                "client_id": client_id,
                "competitor_id": comp["id"],
                "channel_id": (channels_by_page.get(pid or "") or {}).get("id"),
                # The page's own scope. Every competitor ad so far comes from a national
                # page (verified 11 Sep); a location page, like Bouldering Project's DC
                # page, is local whatever the copy says.
                "source_scope": (channels_by_page.get(pid or "") or {}).get("scope") or "national",
                "geo_relevance": verdict.relevance,
                "geo_evidence": verdict.evidence,
                "signal_type": "ad_active",
                # These two are the uniqueness key (migration 002). Without them a
                # re-run inside the same week double-counts every ad.
                "external_ref": archive_id,
                "week_of": wk.isoformat(),
                "data": {
                    "ad_archive_id": archive_id,
                    "page_id": pid,
                    "page_name": snap.get("pageName"),
                    "body": body[:2000],
                    "title": text_fields.get("title"),
                    # Every field the classifier actually scanned, not just the two
                    # that happen to be at the top level. Two thirds of real ads are
                    # dynamic creative whose only copy lives on the cards, so without
                    # this an audit of a suspicious `none` needs a second paid scrape
                    # to see what the classifier saw. It cost one to learn that.
                    "text_scanned": {k: v[:600] for k, v in text_fields.items()},
                    "cta_text": text_fields.get("cta_text"),
                    "cta_type": snap.get("ctaType"),
                    "display_format": snap.get("displayFormat"),
                    "landing_urls": urls[:10],
                    "start_date": ad.get("startDateFormatted"),
                    "publisher_platform": ad.get("publisherPlatform"),
                    "card_count": ad.get("cards_count")
                    or len(ad.get("creative_assets") or snap.get("cards") or []),
                    # Kept so an audit can see every tier that matched, not only the winner.
                    "geo_all_matches": verdict.all_matches,
                    # An ad driving to a careers page is recruitment, not acquisition.
                    # Counted as competitive pressure it is the wrong conclusion from
                    # the right number.
                    "is_recruitment": recruitment,
                    # The advertiser's own UTM naming convention, never a platform field.
                    # Label it as inference wherever it surfaces, or leave it out.
                    "campaign_scope_hint": scope_hint,
                    # True when every top-level copy field was a dynamic-creative
                    # template token and the classification rests entirely on cards.
                    "top_level_copy_was_template": not any(
                        k in text_fields for k in ("body", "title")
                    ),
                },
                "source_url": f"https://www.facebook.com/ads/library/?id={archive_id}",
                "collected_at": collected_at,
            }
        )

        t = tally.setdefault(
            comp["id"],
            {
                "total_active": 0,
                "dc_landing": 0,
                "dc_explicit": 0,
                "regional": 0,
                "other_market": 0,
                "no_geo": 0,
            },
        )
        t["total_active"] += 1
        t["no_geo" if verdict.relevance == "none" else verdict.relevance] += 1

    rollup = []
    for cid, counts in tally.items():
        sampled = counts["total_active"]
        available = totals_by_comp.get(cid)
        caps = [ch.get("max_ads") for ch in channels_by_page.values() if ch.get("competitor_id") == cid]
        cap = max((c for c in caps if c), default=None) if any(caps) else None

        # How we know we got everything, without the library telling us.
        #
        # This actor returns bare ad objects with no wrapper carrying the page's own
        # total, so `available` is usually None and an earlier version recorded
        # sample_method='unknown' for every competitor. That is worse than useless: an
        # unknown denominator is the exact condition this build exists to avoid
        # reporting through.
        #
        # But the inference is sound. When a channel has no cap we ask for
        # RESULTS_LIMIT_PER_PAGE, which is a runaway guard rather than a sampling
        # policy. If fewer came back than we asked for, nothing was truncated: we have
        # the whole page. Onelife, the largest advertiser in this set, returned 266
        # against a ceiling of 800.
        #
        # Only claim census when BOTH are true — uncapped, and under the ceiling.
        # A pull that hits the ceiling exactly is the one case where we genuinely
        # cannot tell, and it stays 'unknown' rather than being rounded up to a
        # reassuring answer.
        if available is not None:
            method = "census" if sampled >= available else "top_by_impressions"
        elif cap:
            method = "top_by_impressions"
            available = None
        elif sampled < RESULTS_LIMIT_PER_PAGE:
            method = "census"
            available = sampled
        else:
            method = "unknown"

        rollup.append(
            {
                "client_id": client_id,
                "competitor_id": cid,
                "week_of": wk.isoformat(),
                "collected_at": collected_at,
                "total_available": available,
                "ads_sampled": sampled,
                "sample_method": method,
                **counts,
            }
        )
    # dc_referencing is a generated column in Postgres. Not computed here, so there is
    # exactly one definition of it and the database owns it.

    return signals, rollup, unmatched


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, help="client slug, e.g. bouldering-project")
    ap.add_argument("--dry-run", action="store_true", help="classify and print, write nothing")
    ap.add_argument(
        "--source",
        choices=("internal", "apify", "file"),
        default="apify",
        help="apify (default): scrape the Ad Library. internal: read what the "
        "internal weekly pipeline collected, which is capped at 35 and truncates copy. "
        "file: read a saved actor dump, for re-tuning without paying twice.",
    )
    ap.add_argument("--week", help="ISO date inside the target week. Defaults to this week.")
    ap.add_argument("--from-file", help="path for --source file")
    ap.add_argument("--save-raw", help="write the source payload to this path")
    ap.add_argument("--competitor", help="collect this one competitor only, by name")
    args = ap.parse_args()

    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supa_url or not supa_key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 2

    sb = Supa(supa_url, supa_key)

    clients = sb.get("public", "clients", {"slug": f"eq.{args.client}", "select": "id,slug,name"})
    if not clients:
        print(f"no client with slug {args.client}", file=sys.stderr)
        return 2
    client = clients[0]
    client_id = client["id"]
    print(f"client: {client['name']} ({client_id})")

    competitors = sb.get(
        "portal",
        "competitors",
        {
            "client_id": f"eq.{client_id}",
            "active": "eq.true",
            "select": "id,name,domain,single_market",
        },
    )
    channels = sb.get(
        "portal",
        "channels",
        {
            "platform": "eq.facebook",
            "purpose": "eq.paid_ads",
            "active": "eq.true",
            "select": "id,competitor_id,external_id,scope,max_ads",
        },
    )
    competitors = only(competitors, args.competitor)
    comp_ids = {c["id"] for c in competitors}
    channels_by_page = {
        str(ch["external_id"]): ch for ch in channels
        if ch["competitor_id"] in comp_ids and ch.get("external_id")
    }
    with_pages = {ch["competitor_id"] for ch in channels_by_page.values()}

    missing = [c["name"] for c in competitors if c["id"] not in with_pages]
    if missing:
        # Loud, because a competitor with no ads channel silently reports zero ads forever
        # and a zero looks like a finding rather than a gap.
        print(f"  WARNING no facebook/paid_ads channel for: {', '.join(missing)}")

    page_ids = list(channels_by_page)
    if not page_ids:
        print("no page ids to collect", file=sys.stderr)
        return 2
    print(f"  {len(competitors)} competitors, {len(page_ids)} ad pages")

    target_week = (
        week_of(date.fromisoformat(args.week)) if args.week else week_of()
    )

    if args.source == "file":
        if not args.from_file:
            print("--source file needs --from-file", file=sys.stderr)
            return 2
        with open(args.from_file) as fh:
            raw = json.load(fh)
        print(f"  loaded {len(raw)} items from {args.from_file}")

    elif args.source == "apify":
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("APIFY_TOKEN must be set", file=sys.stderr)
            return 2
        # Per-channel caps, from portal.channels.max_ads. NULL means census.
        caps = {pid: ch.get("max_ads") for pid, ch in channels_by_page.items()}
        capped = {k: v for k, v in caps.items() if v}
        if capped:
            print(f"  {len(capped)} of {len(caps)} pages are capped: {capped}")
        raw = run_actor(token, page_ids, caps=caps)
        print(f"  actor returned {len(raw)} items")

    else:
        # The default. Read what the internal pipeline collected for this week.
        raw = sb.get(
            "public",
            "signals",
            {
                # Scoped to this client. Without it the query returns every client's
                # meta_ads rows and relies on page-id matching to throw them away, which
                # works but drags seven clients' ads through memory to classify five.
                "client_id": f"eq.{client_id}",
                "signal_type": "eq.meta_ads",
                "collected_at": f"gte.{target_week.isoformat()}",
                "select": "id,competitor_id,collected_at,data",
                "order": "collected_at.desc",
            },
        )
        if not raw:
            print(
                f"  no meta_ads signals in public.signals for the week of {target_week}.\n"
                "  Either the weekly pipeline has not run yet, or Bouldering Project's\n"
                "  competitors are not in public.competitors so nothing pulled them.",
                file=sys.stderr,
            )
            return 1
        print(f"  {len(raw)} meta_ads signal rows from the week of {target_week}")

    if args.save_raw:
        with open(args.save_raw, "w") as fh:
            json.dump(raw, fh)
        print(f"  raw output saved to {args.save_raw}")

    ads = flatten(raw)
    print(f"  {len(ads)} ads after flattening")

    # The Ad Library's own total for a page is a different number from how many ads
    # were pulled, and it lives in a different place depending on the source:
    #
    #   apify   the actor puts `totalCount` on its own wrapper object, alongside the
    #           `url` that was requested. The url carries view_all_page_id, which is
    #           the only reliable way back to a competitor: the first ad's page_id can
    #           belong to a co-branded advertiser rather than the page we asked for.
    #   internal  the pipeline stores `total_available_ads` on the signal payload, and
    #           `total_active_ads` next to it is the SAMPLE SIZE despite the name.
    #
    # Reading the name rather than the meaning is how a 35 gets reported as a 367.
    page_to_comp = {pid: ch["competitor_id"] for pid, ch in channels_by_page.items()}

    def _page_id_from_url(u: str | None) -> str | None:
        if not isinstance(u, str):
            return None
        m = re.search(r"view_all_page_id=(\d+)", u)
        return m.group(1) if m else None

    totals_by_comp: dict[str, int] = {}
    page_totals: dict[tuple[str, str], int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        payload = item.get("data") if isinstance(item.get("data"), dict) else item

        cid = None
        avail = None

        # apify wrapper
        pid = _page_id_from_url(payload.get("url") or payload.get("inputUrl"))
        if pid:
            cid = page_to_comp.get(pid)
            avail = payload.get("totalCount")
            if avail is None:
                results = payload.get("results")
                if isinstance(results, list):
                    avail = next(
                        (r.get("totalCount") for r in results
                         if isinstance(r, dict) and r.get("totalCount") is not None),
                        None,
                    )

        # internal payload
        if cid is None:
            avail = payload.get("total_available_ads")
            first = next((a for a in (payload.get("ads") or []) if isinstance(a, dict)), None)
            cid = page_to_comp.get(str(page_id_of(first) or "")) if first else None

        if cid and isinstance(avail, (int, str)) and str(avail).isdigit():
            # One total per page; a brand with two pages has the sum of both.
            key = (cid, pid or "")
            page_totals[key] = max(page_totals.get(key, 0), int(avail))

    for (cid, _pid), n in page_totals.items():
        totals_by_comp[cid] = totals_by_comp.get(cid, 0) + n

    if not totals_by_comp:
        # Not fatal, but it means every row records sample_method='unknown', and an
        # unknown denominator is the condition this whole build exists to avoid
        # reporting through.
        print(
            "  WARNING could not read the Ad Library's own totals from the source.\n"
            "  Every competitor will record sample_method='unknown' and `avail` will\n"
            "  print as '?'. The tier counts are still correct out of `sampled`."
        )

    signals, rollup, unmatched = build_rows(
        ads, competitors, channels_by_page, client_id, GeoClassifier(), totals_by_comp
    )

    name_of = {c["id"]: c["name"] for c in competitors}
    single = {c["id"]: bool(c.get("single_market")) for c in competitors}

    print(f"\n  week of {target_week}")
    print(f"  {'competitor':<22} {'avail':>6} {'sampled':>8} {'dc_ref':>7} {'land':>5} "
          f"{'expl':>5} {'regl':>5} {'other':>6} {'none':>5}  method")
    for row in sorted(rollup, key=lambda r: -(r["ads_sampled"] or 0)):
        cid = row["competitor_id"]
        dc_ref = row["dc_landing"] + row["dc_explicit"]
        avail = row.get("total_available")
        # A single-market brand's ratio is not reported, because every ad they run is
        # in-market whether the copy says so or not. Printing 0/21 for a brand that
        # only advertises here says the opposite of the truth.
        dc_cell = "in-mkt" if single.get(cid) else str(dc_ref)
        print(
            f"  {name_of.get(cid, '?'):<22} "
            f"{(avail if avail is not None else '?'):>6} {row['ads_sampled']:>8} "
            f"{dc_cell:>7} {row['dc_landing']:>5} {row['dc_explicit']:>5} "
            f"{row['regional']:>5} {row['other_market']:>6} {row['no_geo']:>5}  "
            f"{row['sample_method']}"
        )

    n_single = sum(1 for cid in tally_ids(rollup) if single.get(cid))
    if n_single:
        print(
            f"\n  {n_single} of {len(rollup)} competitors operate ONLY in this market,\n"
            "  shown as 'in-mkt'. Every ad they run is local by definition, so a\n"
            "  DC-referencing ratio for them would read as absence when it means\n"
            "  their creative is placeless. The ratio is meaningful only for the\n"
            "  brands that also advertise somewhere else."
        )

    partial = [r for r in rollup if r["sample_method"] != "census"]
    if partial:
        names = ", ".join(name_of.get(r["competitor_id"], "?") for r in partial)
        print(
            f"\n  NOT a census for: {names}.\n"
            "  Their tier counts are out of `sampled`, not `avail`. Do not extrapolate:\n"
            "  the Ad Library sorts by impressions, so a capped pull describes the\n"
            "  highest-spend creative rather than the page."
        )
    if unmatched:
        print(f"\n  unmatched page ids (dropped): {unmatched}")

    if args.dry_run:
        print("\n  dry run, nothing written")
        return 0

    n_sig = sb.upsert(
        "portal",
        "signals",
        signals,
        on_conflict="competitor_id,signal_type,external_ref,week_of",
    )
    n_roll = sb.upsert(
        "portal", "ad_geo_weekly", rollup, on_conflict="competitor_id,week_of"
    )
    print(f"\n  wrote {n_sig} signals, {n_roll} weekly rollup rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
