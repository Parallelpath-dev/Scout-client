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
The internal weekly pipeline already scrapes these pages and stores the result in
public.signals as signal_type='meta_ads'. This module READS that, classifies it, and
writes the client-facing rows. It does not scrape.

That is deliberate. The scrape is the expensive part, the internal fetcher is proven, and
running a second pull of the same five pages would pay Apify twice for identical ads. The
schemas are separate for client data isolation, which was never a reason to duplicate
collection.

--source apify exists as a fallback for a backfill or a one-off outside the weekly
cadence. It costs money. It is not the default and should stay that way.

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
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import requests

from geo import GeoClassifier, campaign_scope_hint, extract_ad_fields, is_recruitment

ACTOR = "apify~facebook-ads-scraper"
APIFY_BASE = "https://api.apify.com/v2"

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

# A single page running more than this is a signal that something changed upstream, not a
# real result. Onelife was the ceiling at 367 on 11 Sep. The cap bounds spend on a runaway.
RESULTS_LIMIT_PER_PAGE = 800


# ── Supabase ────────────────────────────────────────────────────────────────


class Supa:
    """Thin PostgREST client. Service role, so RLS does not apply to anything here."""

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.h = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _headers(self, schema: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(self.h)
        # PostgREST needs the profile header for a non-default schema, and it differs
        # between reads and writes: Accept-Profile for GET, Content-Profile for the rest.
        h["Accept-Profile"] = schema
        h["Content-Profile"] = schema
        if extra:
            h.update(extra)
        return h

    def get(self, schema: str, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        r = requests.get(
            f"{self.url}/rest/v1/{table}",
            headers=self._headers(schema),
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def upsert(
        self, schema: str, table: str, rows: list[dict[str, Any]], on_conflict: str | None = None
    ) -> int:
        if not rows:
            return 0
        params = {}
        prefer = "resolution=merge-duplicates,return=minimal"
        if on_conflict:
            params["on_conflict"] = on_conflict
        written = 0
        # Chunked so one oversized week cannot produce a request PostgREST refuses.
        for i in range(0, len(rows), 250):
            chunk = rows[i : i + 250]
            r = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers=self._headers(schema, {"Prefer": prefer}),
                params=params,
                data=json.dumps(chunk),
                timeout=120,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"{table} upsert failed {r.status_code}: {r.text[:500]}")
            written += len(chunk)
        return written


# ── Apify ───────────────────────────────────────────────────────────────────


def run_actor(token: str, page_ids: list[str], timeout_s: int = 1800) -> list[dict[str, Any]]:
    """Start the scraper, wait for it, return the dataset items."""
    payload = {
        "startUrls": [{"url": AD_LIBRARY_URL.format(page_id=pid)} for pid in page_ids],
        "resultsLimit": RESULTS_LIMIT_PER_PAGE,
        "activeStatus": "active",
    }
    r = requests.post(
        f"{APIFY_BASE}/acts/{ACTOR}/runs",
        params={"token": token},
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    run = r.json()["data"]
    run_id, dataset_id = run["id"], run["defaultDatasetId"]
    print(f"  apify run {run_id}")

    deadline = time.time() + timeout_s
    status = run["status"]
    while status in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(10)
        s = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token}, timeout=60
        )
        s.raise_for_status()
        status = s.json()["data"]["status"]

    if status != "SUCCEEDED":
        raise RuntimeError(f"apify run ended {status} (run {run_id})")

    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        d = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": token, "offset": offset, "limit": 1000, "clean": "true"},
            timeout=120,
        )
        d.raise_for_status()
        batch = d.json()
        if not batch:
            break
        items.extend(batch)
        offset += len(batch)
    return items


# ── shaping ─────────────────────────────────────────────────────────────────


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
    channels_by_comp: dict[str, dict[str, Any]],
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
    by_page: dict[str, dict[str, Any]] = {}
    for c in competitors:
        ch = channels_by_comp.get(c["id"])
        if ch and ch.get("external_id"):
            by_page[str(ch["external_id"])] = c

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
                "channel_id": (channels_by_comp.get(comp["id"]) or {}).get("id"),
                # Every ad in this set is published from the national page. This is an
                # observation from the 11 Sep verification, not an assumption.
                "source_scope": "national",
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
        rollup.append(
            {
                "client_id": client_id,
                "competitor_id": cid,
                "week_of": wk.isoformat(),
                "collected_at": collected_at,
                # The library's own total for the page. The headline number.
                "total_available": available,
                # What was actually classified. The tier counts sum to this.
                "ads_sampled": sampled,
                # census only when we demonstrably got everything. Anything else is a
                # sample sorted by impressions, which is biased toward the highest-spend
                # creative and must never be extrapolated.
                "sample_method": (
                    "census"
                    if available is not None and sampled >= available
                    else ("top_by_impressions" if available is not None else "unknown")
                ),
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
        default="internal",
        help="internal (default): read the ads the weekly pipeline already pulled. "
        "apify: scrape again, SPENDS CREDIT. file: read a saved actor dump.",
    )
    ap.add_argument("--week", help="ISO date inside the target week. Defaults to this week.")
    ap.add_argument("--from-file", help="path for --source file")
    ap.add_argument("--save-raw", help="write the source payload to this path")
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
        {"client_id": f"eq.{client_id}", "active": "eq.true", "select": "id,name,domain"},
    )
    channels = sb.get(
        "portal",
        "channels",
        {
            "platform": "eq.facebook",
            "purpose": "eq.paid_ads",
            "active": "eq.true",
            "select": "id,competitor_id,external_id,scope",
        },
    )
    comp_ids = {c["id"] for c in competitors}
    channels_by_comp = {
        ch["competitor_id"]: ch for ch in channels if ch["competitor_id"] in comp_ids
    }

    missing = [c["name"] for c in competitors if c["id"] not in channels_by_comp]
    if missing:
        # Loud, because a competitor with no ads channel silently reports zero ads forever
        # and a zero looks like a finding rather than a gap.
        print(f"  WARNING no facebook/paid_ads channel for: {', '.join(missing)}")

    page_ids = [
        str(ch["external_id"]) for ch in channels_by_comp.values() if ch.get("external_id")
    ]
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
        # Deliberately awkward to reach. The weekly pipeline already scrapes these exact
        # pages; running this pays Apify a second time for identical ads.
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("APIFY_TOKEN must be set for --source apify", file=sys.stderr)
            return 2
        print("  WARNING --source apify re-scrapes pages the weekly pipeline already pulls.")
        raw = run_actor(token, page_ids)
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

    # The Ad Library's own total sits on the wrapper, not on any individual ad, and it is
    # a different number from how many were pulled. `total_active_ads` in the internal
    # payload is the SAMPLE SIZE despite the name; `total_available_ads` is the real
    # total. Reading the name rather than the meaning is how a 35 gets reported as a 367.
    page_to_comp = {
        str(ch["external_id"]): cid
        for cid, ch in channels_by_comp.items()
        if ch.get("external_id")
    }
    totals_by_comp: dict[str, int] = {}
    for item in raw:
        payload = item.get("data") if isinstance(item.get("data"), dict) else item
        if not isinstance(payload, dict):
            continue
        avail = payload.get("total_available_ads")
        first = next((a for a in (payload.get("ads") or []) if isinstance(a, dict)), None)
        pid = page_id_of(first) if first else None
        cid = page_to_comp.get(str(pid or ""))
        if cid and isinstance(avail, (int, str)) and str(avail).isdigit():
            totals_by_comp[cid] = max(totals_by_comp.get(cid, 0), int(avail))

    signals, rollup, unmatched = build_rows(
        ads, competitors, channels_by_comp, client_id, GeoClassifier(), totals_by_comp
    )

    name_of = {c["id"]: c["name"] for c in competitors}
    print(f"\n  week of {target_week}")
    print(f"  {'competitor':<22} {'avail':>6} {'sampled':>8} {'dc_ref':>7} {'land':>5} "
          f"{'expl':>5} {'regl':>5} {'other':>6} {'none':>5}  method")
    for row in sorted(rollup, key=lambda r: -(r.get("total_available") or 0)):
        dc_ref = row["dc_landing"] + row["dc_explicit"]
        avail = row.get("total_available")
        print(
            f"  {name_of.get(row['competitor_id'], '?'):<22} "
            f"{(avail if avail is not None else '?'):>6} {row['ads_sampled']:>8} "
            f"{dc_ref:>7} {row['dc_landing']:>5} {row['dc_explicit']:>5} "
            f"{row['regional']:>5} {row['other_market']:>6} {row['no_geo']:>5}  "
            f"{row['sample_method']}"
        )
    partial = [r for r in rollup if r["sample_method"] != "census"]
    if partial:
        # Said out loud every run. A rate from an impression-sorted sample describes the
        # highest-spend creative, not the page, and extrapolating it is the single
        # easiest way to hand a client a number that is wrong by an order of magnitude.
        print(
            f"\n  {len(partial)} of {len(rollup)} competitors were SAMPLED, not censused.\n"
            "  The tier counts are out of `sampled`, not `avail`. Do not extrapolate:\n"
            "  the sample is sorted by impressions and is biased toward the top spenders."
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
