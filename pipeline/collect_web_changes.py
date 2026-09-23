#!/usr/bin/env python3
"""
Scout — web change collector.

Fetches every watched page, compares it against last week's snapshot, classifies what
changed, and writes the result to `portal.signals`.

WHAT IT WATCHES AND WHY BOTH
----------------------------
Two pages per competitor, and the pair is the point:

  the LOCATION page   their DC site. A change here applies here, full stop.
  the PRICING page    usually national or regional. Higher value, lower certainty.

Watching only the local pages would make "they repriced nationally and skipped DC" an
unsayable finding, the same way counting only DC-referencing ads would have made
Movement's zero meaningless. Watching only the national ones loses the thing that is
definitely true.

THE NOISE PROBLEM, WHICH IS THE REAL ONE
----------------------------------------
Page monitoring fails by reporting everything. A cookie banner, a rotating testimonial,
a timestamp. Three real findings buried in forty diffs means nobody opens the tab, which
is worse than not collecting.

So `web_change.py` scores every diff on what changed and whether it applies here, and
only material and minor changes surface. Cosmetic ones are stored and never shown —
worth keeping, because a run of them is how you notice a site being rebuilt, and worth
hiding, because nobody has ever acted on a swapped hero image.

USAGE
-----
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...
    pip install -r requirements.txt && playwright install chromium

    python3 collect_web_changes.py --client bouldering-project --dry-run
    python3 collect_web_changes.py --client bouldering-project
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from geo import GeoClassifier
from web_change import classify_change, should_surface, surface_rank

NAV_TIMEOUT_MS = 30_000


# ── Supabase ────────────────────────────────────────────────────────────────


class Supa:
    """Thin PostgREST client. Service role, so RLS does not apply."""

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.h = {"apikey": key, "Authorization": f"Bearer {key}",
                  "Content-Type": "application/json"}

    def _headers(self, schema: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(self.h)
        h["Accept-Profile"] = schema
        h["Content-Profile"] = schema
        if extra:
            h.update(extra)
        return h

    def get(self, schema: str, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        r = requests.get(f"{self.url}/rest/v1/{table}",
                         headers=self._headers(schema), params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def upsert(self, schema: str, table: str, rows: list[dict[str, Any]],
               on_conflict: str | None = None) -> int:
        if not rows:
            return 0
        params = {"on_conflict": on_conflict} if on_conflict else {}
        for i in range(0, len(rows), 250):
            chunk = rows[i:i + 250]
            r = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers=self._headers(schema, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params=params, data=json.dumps(chunk), timeout=120)
            if r.status_code >= 400:
                raise RuntimeError(f"{table} upsert failed {r.status_code}: {r.text[:500]}")
        return len(rows)


# ── fetching ────────────────────────────────────────────────────────────────


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_content(page) -> dict[str, Any]:
    """Pull the parts of a page worth comparing.

    Same shape as the internal collector's extractor so the two stay legible side by
    side, plus `body`, because a price often lives in neither a heading nor a CTA —
    it sits in a pricing table that none of the structural selectors reach.
    """
    out: dict[str, Any] = {}

    def safe(fn, default):
        try:
            return fn()
        except Exception:
            return default

    out["title"] = safe(lambda: page.title(), "")
    out["meta_description"] = safe(
        lambda: (page.query_selector('meta[name="description"]') or None)
        and page.query_selector('meta[name="description"]').get_attribute("content") or "", "")
    out["h1"] = safe(
        lambda: " | ".join(clean_text(h.inner_text()) for h in page.query_selector_all("h1")
                           if h.inner_text().strip()), "")
    out["h2s"] = safe(
        lambda: [clean_text(h.inner_text()) for h in page.query_selector_all("h2")[:8]
                 if h.inner_text().strip()], [])
    out["nav"] = safe(
        lambda: [clean_text(a.inner_text()) for a in page.query_selector_all("nav a")
                 if a.inner_text().strip()][:40], [])

    def hero():
        for sel in ('[class*="hero"]', '[class*="banner"]', '[id*="hero"]', "section:first-of-type"):
            el = page.query_selector(sel)
            if el:
                return clean_text(el.inner_text()[:500])
        return ""
    out["hero"] = safe(hero, "")

    def ctas():
        found = []
        for sel in ('[class*="cta"]', '[class*="btn"]', "button", 'a[class*="button"]'):
            for el in page.query_selector_all(sel)[:12]:
                t = clean_text(el.inner_text())
                if t and len(t) < 60:
                    found.append(t)
        return sorted(set(found))[:12]
    out["ctas"] = safe(ctas, [])

    # Capped, because a diff of two 200KB blobs is slow and the tail of a page is
    # footer boilerplate that changes for no reason.
    out["body"] = safe(lambda: clean_text(page.inner_text("body"))[:12000], "")
    return out


def fetch_all(urls: list[str]) -> dict[str, dict[str, Any] | None]:
    """One browser, every page. None means the fetch failed."""
    from playwright.sync_api import sync_playwright

    results: dict[str, dict[str, Any] | None] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        for url in urls:
            page = ctx.new_page()
            try:
                page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)   # let the late-loading pricing widgets land
                results[url] = extract_content(page)
                print(f"  ok    {url}")
            except Exception as e:
                # A failed fetch must never look like a page that lost all its content.
                # Recording None keeps last week's snapshot as the comparison base.
                results[url] = None
                print(f"  FAIL  {url}  {type(e).__name__}: {str(e)[:100]}")
            finally:
                page.close()
        browser.close()
    return results


# ── shaping ─────────────────────────────────────────────────────────────────


def week_of(d: date | None = None) -> date:
    d = d or datetime.now(timezone.utc).date()
    return d - timedelta(days=d.weekday())


def url_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--dry-run", action="store_true", help="fetch and classify, write nothing")
    ap.add_argument("--week", help="ISO date inside the target week")
    args = ap.parse_args()

    supa_url, supa_key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not supa_url or not supa_key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 2
    sb = Supa(supa_url, supa_key)

    clients = sb.get("public", "clients", {"slug": f"eq.{args.client}", "select": "id,name"})
    if not clients:
        print(f"no client with slug {args.client}", file=sys.stderr)
        return 2
    client_id, client_name = clients[0]["id"], clients[0]["name"]
    print(f"client: {client_name}")

    competitors = sb.get("portal", "competitors", {
        "client_id": f"eq.{client_id}", "active": "eq.true",
        "select": "id,name,domain,single_market,prices_by_location"})
    comp_by_id = {c["id"]: c for c in competitors}

    channels = sb.get("portal", "channels", {
        "platform": "eq.web", "purpose": "eq.web_change", "active": "eq.true",
        "select": "id,competitor_id,url,scope,location_label"})
    channels = [c for c in channels if c["competitor_id"] in comp_by_id and c.get("url")]
    if not channels:
        print("no web pages to watch", file=sys.stderr)
        return 2

    target_week = week_of(date.fromisoformat(args.week)) if args.week else week_of()
    print(f"  {len(channels)} pages across {len(comp_by_id)} competitors, week of {target_week}")

    # Last stored snapshot per page, whatever week it came from. Using "the previous
    # week" instead would compare against nothing after a skipped run and report every
    # page as unchanged, which is the quiet kind of wrong.
    prior = sb.get("portal", "signals", {
        "client_id": f"eq.{client_id}", "signal_type": "eq.page_snapshot",
        "select": "external_ref,data,week_of", "order": "week_of.desc"})
    last_snap: dict[str, dict[str, Any]] = {}
    for row in prior:
        ref = row.get("external_ref")
        if ref and ref not in last_snap:
            last_snap[ref] = row.get("data") or {}

    print("\nfetching")
    fetched = fetch_all([c["url"] for c in channels])

    clf = GeoClassifier()
    collected_at = datetime.now(timezone.utc).isoformat()
    snap_rows, change_rows, report = [], [], []

    for ch in channels:
        url, comp = ch["url"], comp_by_id[ch["competitor_id"]]
        content = fetched.get(url)
        ref = f"page:{url_key(url)}"

        if content is None:
            # Skip entirely. Writing an empty snapshot would make next week's diff read
            # as "this page lost all its content", which is a fabricated finding.
            report.append((comp["name"], ch.get("location_label") or url, "FETCH FAILED",
                           None, None, None))
            continue

        old = (last_snap.get(ref) or {}).get("content")
        verdict = classify_change(
            old, content,
            channel_scope=ch.get("scope") or "local",
            prices_by_home_gym=bool(comp.get("prices_by_location")),
            single_market=bool(comp.get("single_market")),
        )

        # Geo of the page itself, from the same classifier the ads use.
        g = clf.classify(
            text_fields={k: v for k, v in content.items() if isinstance(v, str) and v},
            urls=[url], competitor_domain=comp.get("domain"))

        snap_rows.append({
            "client_id": client_id, "competitor_id": comp["id"], "channel_id": ch["id"],
            "source_scope": ch.get("scope") or "local",
            "geo_relevance": g.relevance, "geo_evidence": g.evidence,
            "signal_type": "page_snapshot", "external_ref": ref,
            "week_of": target_week.isoformat(),
            "data": {"url": url, "label": ch.get("location_label"), "content": content},
            "source_url": url, "collected_at": collected_at,
        })

        if verdict.changed:
            change_rows.append({
                "client_id": client_id, "competitor_id": comp["id"], "channel_id": ch["id"],
                "source_scope": ch.get("scope") or "local",
                "geo_relevance": g.relevance, "geo_evidence": g.evidence,
                "signal_type": "web_change",
                "external_ref": f"change:{url_key(url)}",
                "week_of": target_week.isoformat(),
                "data": {
                    "url": url, "label": ch.get("location_label"),
                    "change_types": verdict.change_types,
                    "headline_type": verdict.headline_type,
                    "materiality": verdict.materiality,
                    "similarity": verdict.similarity,
                    "evidence": verdict.evidence,
                    "price_before": verdict.price_before,
                    "price_after": verdict.price_after,
                    # Carried on the row itself so the caveat can never be separated
                    # from the number it qualifies.
                    "applies_locally": verdict.applies_locally,
                    "caveat": verdict.caveat,
                    "surfaces": should_surface(verdict),
                    "rank": list(surface_rank(verdict, g.relevance)),
                },
                "source_url": url, "collected_at": collected_at,
            })

        report.append((comp["name"], ch.get("location_label") or url,
                       verdict.headline_type if verdict.changed else "—",
                       verdict.materiality if verdict.changed else "",
                       verdict.applies_locally if verdict.changed else "",
                       verdict))

    # ── report ──────────────────────────────────────────────────────────────
    print(f"\n  {'competitor':<12} {'page':<22} {'change':<10} {'materiality':<10} {'local?':<8}")
    for name, label, typ, mat, loc, v in report:
        print(f"  {name:<12} {str(label)[:22]:<22} {str(typ):<10} {str(mat):<10} {str(loc):<8}")
        if v is not None and getattr(v, "evidence", None) and v.changed:
            print(f"               {v.evidence[0][:90]}")
        if v is not None and getattr(v, "caveat", None):
            print(f"               CAVEAT: {v.caveat[:90]}")

    surfacing = [r for r in change_rows if r["data"]["surfaces"]]
    first_run = sum(1 for ch in channels if f"page:{url_key(ch['url'])}" not in last_snap)
    failed = sum(1 for _, _, t, _, _, _ in report if t == "FETCH FAILED")

    print(f"\n  {len(change_rows)} changes detected, {len(surfacing)} worth surfacing")
    if first_run:
        print(f"  {first_run} pages seen for the first time — recorded as a baseline, not a change.")
    if failed:
        print(f"  {failed} pages FAILED to fetch. Their snapshots were not written, so next "
              f"week compares against the last good one rather than against an empty page.")

    if args.dry_run:
        print("\n  dry run, nothing written")
        return 0

    n1 = sb.upsert("portal", "signals", snap_rows,
                   on_conflict="competitor_id,signal_type,external_ref,week_of")
    n2 = sb.upsert("portal", "signals", change_rows,
                   on_conflict="competitor_id,signal_type,external_ref,week_of")
    print(f"\n  wrote {n1} snapshots, {n2} change rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
