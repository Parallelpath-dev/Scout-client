#!/usr/bin/env python3
"""
Scout — the weekly briefing synthesizer for the client portal.

Reads one week of portal.signals for one client and writes one portal.briefings row.

    python3 synthesizer.py --client bouldering-project --digest-only   # no model call
    python3 synthesizer.py --client bouldering-project --dry-run       # model, no write
    python3 synthesizer.py --client bouldering-project                 # the real thing
    python3 synthesizer.py --client bouldering-project --week 2026-09-21

THE SHAPE OF IT
---------------
Three stages, and only the middle one is a model.

  1. DIGEST (code). The week's signals are reduced to structured fields the model is
     allowed to cite. Web changes and emails arrive already ranked: each row carries
     `rank`, the output of web_change.surface_rank / email_classify.surface_rank,
     computed by the collector. This file sorts on that tuple and never re-scores it.
     Ads arrive as the weekly rollup plus the few ads worth naming. There is no third
     ranking here.

  2. ANALYST then STRATEGIST (model). Two calls, as in the internal tool, with the
     executive output profile appended to both. The Analyst may only fill defined
     fields and may only cite signal ids present in the digest. The Strategist writes
     one recommendation per development from the client brain.

  3. ASSEMBLE and GATE (code). Developments are ordered by the best surface rank
     among the signals they cite, cut to the profile's ceiling, and given their source
     link and any caveat carried on those signals. The pressure score is not the
     model's: momentum.py counts it and calibrate.py scores it against each
     competitor's own last 12 weeks, before the Analyst runs, and the Analyst is told
     the result to explain. Then
     validate_briefing.validate() decides: pass publishes, fail writes the row HELD
     (published_at = NULL) and exits 1 so the workflow goes red.

Brevity comes from the prompt constraints in executive_profile.py. Never from lowering
max_tokens, which truncates mid-sentence and breaks JSON parsing.

WHAT THE FULL REPORT HOLDS
--------------------------
`summary`, `developments` and `pressure_score` are columns. `full_report` holds the
rest, keyed by the portal's own section ids so each tab reads its own key:

    pressure   market and per-competitor momentum: status, score, trend, metrics,
               metric z-scores, events, driver, prior score, delta
    sections   search  {keyword_movement, demand_shifts}
               paid    {live_ad_creative, spend_signals}
               social  {audience_cadence, content_themes}
               owned   {website_changes, email_programs}
    coverage   what we could and could not see this week, written by code
    validation the gate's verdict, so a held week says why
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import momentum as mo
import validate_briefing as vb
from executive_profile import DEV_RANGE, profile_block
from supa import Supa
from week_window import fetch_week_signals, parse_week

MODEL = os.environ.get("SCOUT_MODEL", "claude-sonnet-4-6")

RANKED_TYPES = ("web_change", "email")
SEARCH_TYPES = ("tracked_keyword_positions", "client_keyword_positions", "domain_overview")
UNRANKED = (9, 9, 9)

# Per-competitor caps on what reaches the model. The rollup carries the totals; these
# are only the ads worth naming.
MAX_GEO_ADS = 8
MAX_NEW_ADS = 6
MAX_RANKED = 20
EXCERPT = 240


# ── Stage 1: digest ──────────────────────────────────────────────────────────


def _excerpt(*texts: Any) -> str | None:
    """First real copy among the candidates. Template tokens are not copy."""
    for t in texts:
        if isinstance(t, str) and t.strip() and "{{" not in t:
            return re.sub(r"\s+", " ", t).strip()[:EXCERPT]
    return None


def _date(v: Any) -> date | None:
    if not v:
        return None
    s = str(v)
    if s.isdigit():  # epoch seconds
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc).date()
        except (ValueError, OverflowError):
            return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _rank(s: dict[str, Any]) -> tuple[int, ...]:
    r = (s.get("data") or {}).get("rank")
    return tuple(r) if isinstance(r, list) and r else UNRANKED


def _surfaces(s: dict[str, Any]) -> bool:
    d = s.get("data") or {}
    # web_change writes `surfaces`, email writes `surfaced`. Same meaning.
    return bool(d.get("surfaces", d.get("surfaced", False)))


def _truncate_json(v: Any, n: int) -> Any:
    s = json.dumps(v, default=str)
    return v if len(s) <= n else s[:n] + "…"


def build_digest(
    signals: list[dict[str, Any]],
    competitors: list[dict[str, Any]],
    rollups: list[dict[str, Any]],
    prior_rollups: list[dict[str, Any]],
    email_channels: set[str],
    client_name: str,
    wk: date,
    email_live: bool = True,
    unreadable: list[str] | None = None,
) -> dict[str, Any]:
    """Everything the Analyst is allowed to see, and nothing it is not.

    Pure: no network, no database, so the whole shape is pinned by fixtures.
    """
    comp = {c["id"]: c for c in competitors}

    def name(cid: str | None) -> str:
        return comp[cid]["name"] if cid in comp else f"{client_name} (client)"

    # ── ranked: web changes and emails, sorted on the collectors' own rank ──
    ranked, quiet = [], {"web_change": 0, "email": 0}
    for s in signals:
        if s["signal_type"] not in RANKED_TYPES:
            continue
        if not _surfaces(s):
            quiet[s["signal_type"]] += 1
            continue
        d = s.get("data") or {}
        item = {
            "signal_id": s["id"],
            "competitor": name(s.get("competitor_id")),
            "kind": s["signal_type"],
            "type": d.get("headline_type") or d.get("email_type"),
            "materiality": d.get("materiality"),
            "geo_relevance": s.get("geo_relevance"),
            "source_scope": s.get("source_scope"),
            "evidence": (d.get("evidence") or [])[:4],
            "applies_locally": d.get("applies_locally"),
            "caveat": d.get("caveat"),
            "source_url": s.get("source_url"),
            "_rank": _rank(s),
        }
        if s["signal_type"] == "web_change":
            item.update(page=d.get("label") or d.get("url"),
                        price_before=d.get("price_before"), price_after=d.get("price_after"))
        else:
            item.update(subject=d.get("subject"), prices=d.get("prices"),
                        sent=str(s.get("observed_at") or "")[:10])
        ranked.append(item)
    ranked.sort(key=lambda i: i["_rank"])
    ranked = ranked[:MAX_RANKED]
    for i in ranked:
        i.pop("_rank")

    # ── paid: the weekly rollup, then the ads worth naming ──────────────────
    prior = {r["competitor_id"]: r for r in prior_rollups}
    ads_by_comp: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        if s["signal_type"] == "ad_active":
            ads_by_comp.setdefault(s["competitor_id"], []).append(s)

    paid = []
    for r in rollups:
        cid = r["competitor_id"]
        c = comp.get(cid, {})
        ads = ads_by_comp.get(cid, [])
        p = prior.get(cid)
        dc_ref = (r.get("dc_landing") or 0) + (r.get("dc_explicit") or 0)
        entry: dict[str, Any] = {
            "competitor": name(cid),
            "single_market": bool(c.get("single_market")),
            "ads_in_library": r.get("total_available"),
            "ads_classified": r.get("ads_sampled"),
            "sample_method": r.get("sample_method"),
            "dc_referencing_floor": dc_ref,
            "regional": r.get("regional"),
            "other_market": r.get("other_market"),
            "recruitment_ads": sum(1 for a in ads if (a.get("data") or {}).get("is_recruitment")),
            "formats": {},
            "prior_week": None,
        }
        if p:
            entry["prior_week"] = {
                "ads_in_library": p.get("total_available"),
                "dc_referencing_floor": (p.get("dc_landing") or 0) + (p.get("dc_explicit") or 0),
            }
        for a in ads:
            f = (a.get("data") or {}).get("display_format") or "unknown"
            entry["formats"][f] = entry["formats"].get(f, 0) + 1

        def ad_item(a: dict[str, Any]) -> dict[str, Any]:
            d = a.get("data") or {}
            ts = d.get("text_scanned") or {}
            return {
                "signal_id": a["id"],
                "geo_relevance": a.get("geo_relevance"),
                "geo_evidence": a.get("geo_evidence"),
                "copy": _excerpt(d.get("body"), d.get("title"), *ts.values()),
                "cta": d.get("cta_text"),
                "format": d.get("display_format"),
                "started": str(_date(d.get("start_date")) or "") or None,
                "landing": (d.get("landing_urls") or [None])[0],
                "campaign_scope_hint_INFERENCE": d.get("campaign_scope_hint"),
            }

        acquisition = [a for a in ads if not (a.get("data") or {}).get("is_recruitment")]
        geo = [a for a in acquisition
               if a.get("geo_relevance") in ("dc_landing", "dc_explicit", "regional")]
        cutoff = wk - timedelta(days=7)
        new = [a for a in acquisition
               if (_date((a.get("data") or {}).get("start_date")) or date.min) >= cutoff
               and a not in geo]
        entry["geo_referencing_ads"] = [ad_item(a) for a in geo[:MAX_GEO_ADS]]
        started = [a for a in acquisition
                   if (_date((a.get("data") or {}).get("start_date")) or date.min) >= cutoff]
        # Meta files one message under many ad IDs (VIDA: 28 IDs, 8 messages, 23 Sep).
        # The briefing counts messages, the same unit the pressure score counts.
        entry["new_messages_since_last_week"] = len(
            {mo._message_key(a.get("data") or {}) or f"id:{a['id']}" for a in started})
        entry["new_ad_ids_since_last_week_OVERCOUNTS"] = len(started)
        seen_msgs: dict[str, dict[str, Any]] = {}
        for a in new:
            k = mo._message_key(a.get("data") or {}) or f"id:{a['id']}"
            if k in seen_msgs:
                seen_msgs[k]["ad_ids_with_this_message"] += 1
            elif len(seen_msgs) < MAX_NEW_ADS:
                seen_msgs[k] = dict(ad_item(a), ad_ids_with_this_message=1)
        entry["new_ads"] = list(seen_msgs.values())
        paid.append(entry)
    paid.sort(key=lambda e: -(e["ads_in_library"] or e["ads_classified"] or 0))

    # ── search: position tracking and domain overviews ──────────────────────
    search = []
    for s in signals:
        if s["signal_type"] not in SEARCH_TYPES:
            continue
        d = s.get("data") or {}
        item: dict[str, Any] = {
            "signal_id": s["id"],
            "competitor": name(s.get("competitor_id")),
            "signal_type": s["signal_type"],
        }
        kws = d.get("keywords")
        if isinstance(kws, list):
            item["keywords"] = [
                {k: kw.get(k) for k in ("keyword", "position", "previous_position", "volume",
                                         "landing", "url") if kw.get(k) is not None}
                for kw in kws if isinstance(kw, dict)
            ][:15]
        else:
            item["data"] = _truncate_json(
                {k: v for k, v in d.items() if k not in ("accuracy", "caveat")}, 700)
        # A directional figure stays labelled directional everywhere it travels.
        if d.get("accuracy"):
            item["accuracy"] = d["accuracy"]
        if d.get("caveat"):
            item["caveat"] = d["caveat"]
        search.append(item)

    # ── organic social: per channel, then the posts worth naming ────────────
    posts_by_ch: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        if s["signal_type"] == "social_post":
            posts_by_ch.setdefault(s.get("channel_id") or "", []).append(s)
    social = []
    for s in signals:
        if s["signal_type"] != "social_profile":
            continue
        d = s.get("data") or {}
        ps = posts_by_ch.get(s.get("channel_id") or "", [])

        def eng(p: dict[str, Any]) -> int:
            pd = p.get("data") or {}
            return sum(int(pd.get(k) or 0) for k in ("likes", "comments", "shares"))

        local = [p for p in ps if p.get("geo_relevance") in ("dc_landing", "dc_explicit", "regional")]
        top = sorted((p for p in ps if p not in local), key=eng, reverse=True)
        social.append({
            "competitor": name(s.get("competitor_id")),
            "platform": d.get("platform"),
            "account": d.get("handle"),
            "account_scope": s.get("source_scope"),
            "location": d.get("location_label"),
            "followers": d.get("followers"),
            "posts_this_week": d.get("posts_in_week"),
            "count_is_floor": bool(d.get("capped")),
            "avg_engagement": d.get("avg_engagement"),
            "latest_post": str(d.get("latest_post_at") or "")[:10] or None,
            "signal_id": s["id"],
            "posts": [
                {"signal_id": p["id"], "geo_relevance": p.get("geo_relevance"),
                 "geo_evidence": p.get("geo_evidence"),
                 "text": _excerpt((p.get("data") or {}).get("text")),
                 "format": (p.get("data") or {}).get("format"),
                 "engagement": eng(p), "url": p.get("source_url"),
                 "posted": str((p.get("data") or {}).get("posted_at") or "")[:10]}
                for p in (local[:4] + top[:3])
            ],
        })
    social.sort(key=lambda x: (x["competitor"], x["platform"] or ""))

    # ── anything else: any signal type this file does not know yet ──────────
    known = set(RANKED_TYPES) | set(SEARCH_TYPES) | {"ad_active", "social_post", "social_profile"}
    other = [
        {"signal_id": s["id"], "competitor": name(s.get("competitor_id")),
         "signal_type": s["signal_type"], "source_scope": s.get("source_scope"),
         "geo_relevance": s.get("geo_relevance"),
         "data": _truncate_json(s.get("data") or {}, 500)}
        for s in signals if s["signal_type"] not in known
    ][:40]

    # ── coverage: what we could and could not see, stated by code ───────────
    coverage = []
    counts: dict[str, int] = {}
    for s in signals:
        counts[s["signal_type"]] = counts.get(s["signal_type"], 0) + 1
    no_email = sorted(c["name"] for c in competitors if c["id"] not in email_channels)
    emailed = {s.get("competitor_id") for s in signals if s["signal_type"] == "email"}
    silent = sorted(c["name"] for c in competitors
                    if c["id"] in email_channels and c["id"] not in emailed)
    if no_email:
        coverage.append(f"No email list to monitor for {', '.join(no_email)}.")
    if not email_live:
        coverage.append("Email monitoring began after the week this briefing covers, so "
                        "competitor email is not reflected yet.")
    elif silent:
        coverage.append(f"No email received this week from {', '.join(silent)}.")
    if paid:
        coverage.append("Ad counts that reference DC are a floor. Meta publishes no targeting "
                        "data, so an ad naming no place may still run here.")
    if any(i.get("accuracy") == "directional" for i in search):
        coverage.append("Paid keyword counts are directional estimates, not observed spend.")
    if not social:
        coverage.append("Organic social was not collected this week.")
    else:
        local_accounts = {x["competitor"] for x in social if x["account_scope"] == "local"}
        corporate_only = sorted({x["competitor"] for x in social} - local_accounts)
        if corporate_only:
            coverage.append(f"No location account exists for {', '.join(corporate_only)}, "
                            f"so their social is the regional or corporate feed.")
        floors = sorted({f"{x['competitor']} {x['platform']}" for x in social
                         if x["count_is_floor"]})
        if floors:
            coverage.append(f"Post counts are a floor for {', '.join(floors)}.")
    for u in unreadable or []:
        coverage.append(u)
    single = sorted(c["name"] for c in competitors if c.get("single_market"))
    if single:
        coverage.append(f"{', '.join(single)} operate only in this market, so all of their "
                        f"activity is local whatever the copy says.")

    return {
        "client": client_name,
        "week_of": wk.isoformat(),
        "competitors": [
            {"name": c["name"], "single_market": bool(c.get("single_market")),
             "prices_by_location": bool(c.get("prices_by_location")),
             "monitored_location": c.get("display_local") or c.get("monitored_site")}
            for c in competitors
        ],
        "ranked_changes": ranked,
        "not_surfaced": quiet,
        "paid": paid,
        "search": search,
        "social": social,
        "other_signals": other,
        "signal_counts": counts,
        "coverage": coverage,
    }


def digest_signal_ids(digest: dict[str, Any]) -> set[str]:
    """Every id the model was shown. Anything it cites outside this set was invented."""
    ids: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if isinstance(v.get("signal_id"), str):
                ids.add(v["signal_id"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(digest)
    return ids


def alias_digest(digest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The digest the model sees, with every signal_id swapped for a short ref.

    The second live run was held because the model copied one UUID with one character
    wrong. It cannot mistype "s14". Returns (aliased digest, ref -> real id).
    """
    ref_of: dict[str, str] = {}

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            out = {}
            for k, x in v.items():
                if k == "signal_id" and isinstance(x, str):
                    if x not in ref_of:
                        ref_of[x] = f"s{len(ref_of) + 1}"
                    out[k] = ref_of[x]
                else:
                    out[k] = walk(x)
            return out
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    aliased = walk(digest)
    return aliased, {r: i for i, r in ref_of.items()}


def unalias(analysis: dict[str, Any], real: dict[str, str]) -> dict[str, Any]:
    """Refs back to real ids in every signal_ids list. An unknown ref is left as written,
    so the gate sees it and fails it as fabrication."""

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: ([real.get(i, i) if isinstance(i, str) else i for i in x]
                        if k == "signal_ids" and isinstance(x, list) else walk(x))
                    for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(analysis)


# ── Stage 2: the model ───────────────────────────────────────────────────────

ANALYST_SYSTEM = """You are the Scout Analyst for a client-facing competitive briefing.
You receive a structured digest of one week of competitor signals and fill a JSON schema.
No recommendations. What happened, what changed, what it means for the client.

Hard rules:
1. Only reference competitors, numbers, offers and dates present in the digest. Never
   introduce a fact from memory, including facts about these brands you believe are true.
2. Every development and every section item cites the signal_id values it rests on,
   copied exactly from the digest. Never invent or alter an id.
3. A section with nothing to cite is an empty list. Never write an item to say there is
   no data, monitoring had not started, or something was not visible: code already tells
   the reader what we could not see, in coverage.
4. Respond with one JSON object matching the schema. No prose outside it, no markdown.

How to read the digest:
- ranked_changes is already in priority order, set by rules that put materiality ahead
  of geography. A national price change outranks a local cosmetic edit. Keep that order
  when choosing developments unless a paid or search item is more material.
- applies_locally "unknown" means the change may not be true in this market. Never state
  such a price as the local price. The caveat on the item travels with it.
- dc_referencing_floor counts ads whose copy names this market. It is a floor on local
  activity, never a count of ads targeted here. Say "ads that reference DC", never
  "ads targeting DC".
- For a single_market competitor, never report a low DC-referencing share as absence.
  Every ad they run is in-market.
- ads_in_library is the page total. ads_classified is how many were read. Never present
  ads_classified as the number of ads running unless sample_method is census.
- A field ending _INFERENCE is the advertiser's own naming convention, not platform data.
  Label anything built on it as inference.
- accuracy "directional" figures are estimates. Say so wherever one appears.
- recruitment_ads are hiring, not acquisition. They are not competitive pressure.
- Launches are counted in messages: new_messages_since_last_week. Write "launched 8 new
  ad messages". Never quote new_ad_ids_since_last_week_OVERCOUNTS as ads launched: Meta
  files one message under many ad IDs, so that number inflates a refresh into a surge.
- Never claim a first, largest, biggest, most, record or highest unless the digest
  shows that comparison. The pressure score is the comparison with a competitor's own
  normal: write "furthest above its own normal", never "its largest push".
- A number that appears in two fields of one development is the same number in both.
- Directional figures (Semrush keyword counts, traffic estimates) are never compared
  across competitors: no highest, lowest, most, leads or ranks. Report each alone.
- Say where something ran only from the signals that show it. An award in social posts
  is "posted on social", never "across paid and social" unless an ad carries it too.
- Name a competitor's location in full, never a short form a DC reader could take for a
  neighbourhood: a /columbia/ landing page is "Columbia, MD".
- Cite every signal a claim rests on. A claim from a social post cites that post.
- promotions: every concrete offer a competitor is running this week: a price, a trial,
  a free pass or class, a waived fee, a credit, a discount, a limited-time deal. One item
  per distinct offer, in plain terms ("$35 two-week trial", never "great value"). Cite
  every signal carrying it. An amenity or a brand message is not a promotion.
- campaign_signals: flag a competitor only when the SAME theme shows up on two or more
  channels this week. Channels are paid ads, organic social, email and website; several
  social platforms are one channel. Cite signals from each channel, one evidence line per
  channel. Volume alone is never a campaign: many ads on one message is one channel.
  Different topics on different channels are not a campaign. No shared theme, empty list.
- Keyword search volumes, if quoted, are the DC figures in the digest or none at all.
- social: account_scope "local" is a location account and its posts are ground-level
  DC content. "regional" and "national" are the DMV or corporate feed; only their posts
  whose geo_relevance names DC are local. Never describe a corporate feed as the
  competitor's DC activity. count_is_floor true means say "at least N posts".
- followers null means unknown, never zero.

The digest's pressure section is computed by code and is final. Do not re-score it.
It is CALIBRATED: 50 is a normal week for these competitors, 65 is about one usual
swing above normal, 80 about two. It measures how unusual the week is, not how large a
competitor is. While status is "calibrating" there is no score yet; say nothing about
pressure beyond the events. When you mention it, name the driver and what moved.
Each competitor and the market also carry a score per channel in components, with its
basis. basis "own": say "above its own normal". basis "set": the competitor has too
little history yet, so it is compared with the rest of the set this week; say "above
the rest of the set this week", never "above its own normal". basis "blend": say
"above normal". Paid compares new launches, never the number of ads running.
The score measures only the metrics in scored_on. Credit a score to those metrics and
nothing else: if scored_on is ["social_posts"], the score is about organic posting, and
ads, however many launched, did not move it."""

ANALYST_SCHEMA = """{
  "summary": "<answers first: does anything competitors did change the client's plans? then what. aim for 60 words, hard limit 75>",
  "developments": [
    {
      "competitor": "<name>",
      "headline": "<competitor + verb, aim for 8 words, hard limit 10>",
      "so_what": "<why it matters to the client, aim for 20 words, hard limit 25>",
      "observed": "<what the signals show, stated as observation, aim for 35 words, hard limit 40>",
      "confidence": "high|medium|low",
      "signal_ids": ["<id>", "<id>"]
    }
  ],
  "sections": {
    "overview": {
      "promotions": [{"competitor": "<name>", "offer": "<the offer in plain terms, aim for 12 words, hard limit 20>",
                      "signal_ids": ["<refs of the ads, posts, emails or pages carrying it>"]}],
      "campaign_signals": [{"competitor": "<name>", "theme": "<the shared message, aim for 10 words, hard limit 15>",
                            "evidence": ["<what one channel shows, aim for 20 words, hard limit 30>", "<the next channel>"],
                            "signal_ids": ["<refs from at least two channels>"]}]
    },
    "search": {
      "keyword_movement": [{"keyword": "<kw>", "observation": "<aim for 35 words, hard limit 40>", "signal_ids": ["<id>"]}],
      "demand_shifts": []
    },
    "paid": {
      "live_ad_creative": [{"competitor": "<name>", "message": "<what the ads lead with>", "format": "<format>", "signal_ids": ["<id>"]}],
      "spend_signals": [{"competitor": "<name>", "observation": "<aim for 35 words, hard limit 40>", "signal_ids": ["<id>"]}]
    },
    "social": {"audience_cadence": [], "content_themes": []},
    "owned": {
      "website_changes": [{"competitor": "<name>", "observation": "<aim for 35 words, hard limit 40>", "applies_locally": "yes|unknown|no", "signal_ids": ["<id>"]}],
      "email_programs": [{"competitor": "<name>", "observation": "<aim for 35 words, hard limit 40>", "signal_ids": ["<id>"]}]
    }
  }
}"""

STRATEGIST_SYSTEM = """You are the Scout Strategist. You receive this week's ranked
developments, the findings for each tab of the dashboard (search, paid, social, owned),
and the client's strategic context. You write one recommended action for each
development, and up to three recommendations for each tab. You never introduce events
or data that are not in the developments or the tab findings.

Hard rules:
1. One action per development, specific enough to brief someone on Monday.
   Tab recommendations: zero to three per tab, each built on that tab's findings and
   citing the refs of the findings it rests on, copied exactly. A tab with nothing
   worth acting on gets an empty list. Never repeat a development's recommendation.
2. Respect the client context. Where it marks something inferred or assumed, label any
   recommendation resting on it as inference.
3. Anything the client context says is raised on a call and never in writing stays out
   of your output entirely. That includes any comparison of the client's own locations
   competing with each other for members.
4. Respond with one JSON object matching the schema. No prose outside it, no markdown."""

STRATEGIST_SCHEMA = """{
  "recommendations": [
    {"index": <the development's index>, "recommendation": "<aim for 25 words, hard limit 30>"}
  ],
  "section_recommendations": {
    "search": [{"observation": "<the finding it answers, aim for 15 words, hard limit 20>",
                "recommendation": "<the action, aim for 25 words, hard limit 30>",
                "why": "<the business outcome for the client, aim for 25 words, hard limit 40>",
                "signal_ids": ["<ref>"]}],
    "paid": [],
    "social": [],
    "owned": []
  }
}"""


def extract_json(raw: str) -> dict[str, Any]:
    """Model output to a dict. Tolerates a markdown fence; nothing else."""
    s = raw.strip()
    if s.startswith("```"):
        s = "\n".join(l for l in s.splitlines() if not l.strip().startswith("```")).strip()
    try:
        out = json.loads(s)
    except json.JSONDecodeError:
        a, b = s.find("{"), s.rfind("}")
        if a < 0 or b <= a:
            raise
        out = json.loads(s[a : b + 1])
    if not isinstance(out, dict):
        raise ValueError("model returned JSON that is not an object")
    return out


ModelFn = Callable[[str, str, int, float], str]


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"   # the API's own versioned contract, not an SDK release
RETRY_STATUS = {429, 500, 502, 503, 504, 529}


def anthropic_model(system: str, user: str, max_tokens: int, temperature: float,
                    *, post=None, sleep=None) -> str:
    """One Messages API call over plain HTTP.

    No SDK, on purpose. The first live run died because the runner installed a newer
    SDK (1.8.0) that rejects `temperature`, and `anthropic>=0.40` let it. The HTTP
    contract is versioned by the anthropic-version header and does not move under us.

    `temperature` is accepted for the call sites' sake and not sent: it is the argument
    that broke, and the output is shaped by the schema, not by sampling.
    """
    import time

    import requests

    post = post or requests.post
    sleep = sleep or time.sleep
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    for attempt in range(4):
        r = post(ANTHROPIC_URL, headers=headers, json=body, timeout=300)
        if r.status_code in RETRY_STATUS and attempt < 3:
            sleep(min(60, 5 * 2 ** attempt))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:400]}")
        data = r.json()
        if data.get("stop_reason") == "max_tokens":
            raise RuntimeError("model hit max_tokens; output is truncated, refusing to parse it")
        return "".join(b.get("text", "") for b in data.get("content") or []
                       if b.get("type") == "text")
    raise RuntimeError("Anthropic API: retries exhausted")


def gate_rules(profile: str) -> str:
    """What validate_briefing.py fails, told to the model in its own words.

    Built from the validator's own constants. Every held week so far was a rule the
    gate enforced and the prompt never stated; this makes that impossible to repeat.
    """
    lines = [
        "",
        "",
        "## THE REVIEW GATE",
        "Code checks every text field before a client sees it. One breach anywhere holds",
        "the whole briefing. These are checked mechanically, so follow them literally:",
        "- No em dashes and no en dashes anywhere, including ranges. Write \"Sept 17 to 21\",",
        "  \"$35 to $50\". Use commas or full stops between clauses.",
        "- Never put \"not\" and then \"but\" or \"it's\" in one sentence. State what happened:",
        "  \"Movement kept its price and added yoga\", never \"did not change price but added yoga\".",
        "- Never use these phrases: " + ", ".join(f'"{x.strip()}"' for x in vb.BANNED_PHRASES) + ".",
        "- Never end a field on a connecting word (" + "and, the, to, of, a, in, with, for, that"
        + ") or a hyphen. Each field ends as a finished sentence or phrase.",
        "- Never leave a text field empty. Omit the item instead.",
    ]
    if profile == "executive":
        lines += [
            "- Hard word ceilings, counted by splitting on spaces: summary "
            f"{vb.CAPS['summary']}, headline {vb.CAPS['headline']}, so_what {vb.CAPS['so_what']}, "
            f"recommendation {vb.CAPS['recommendation']}, every other text field "
            f"{vb.CAPS['_default']}. Aim well under them.",
            "- The summary never begins with: "
            + ", ".join(f'"{x}"' for x in vb.GENERIC_OPENERS) + ".",
            "- A development never has confidence \"low\" and never rests on one signal. Omit",
            "  it instead. Every development cites at least two refs.",
            f"- Between {vb.DEV_MIN} and {vb.DEV_MAX} developments.",
        ]
    return "\n".join(lines)


def _never_block(terms: list[str]) -> str:
    if not terms:
        return ""
    return ("\n\nNever write any of these words, in any field, for any reason: "
            + ", ".join(f'"{t}"' for t in terms)
            + ". A briefing that contains one is held and the client receives nothing.")


def run_analyst(model: ModelFn, digest: dict[str, Any], profile: str,
                never: list[str] | None = None, repair: str = "") -> dict[str, Any]:
    user = (
        f"Client: {digest['client']}. Week of {digest['week_of']}.\n\n"
        f"## DIGEST\n{json.dumps(digest, indent=1, default=str)}\n\n"
        f"## SCHEMA\n{ANALYST_SCHEMA}"
        + repair
    )
    return extract_json(model(ANALYST_SYSTEM + profile_block(profile, "analyst")
                              + gate_rules(profile) + _never_block(never or []),
                              user, 10000, 0.1))


def run_strategist(
    model: ModelFn, devs: list[dict[str, Any]], brain: str, coverage: list[str],
    client_name: str, profile: str, never: list[str] | None = None, repair: str = "",
    sections: dict[str, Any] | None = None,
) -> tuple[dict[int, str], dict[str, list[dict[str, Any]]]]:
    """(recommendation per development index, recommendations per tab).

    sections are the Analyst's tab findings with signal refs, so a tab recommendation
    cites refs the gate can check against the digest like any other claim.
    """
    if not devs and not sections:
        return {}, {}
    brief = [{"index": i, **{k: d.get(k) for k in
              ("competitor", "headline", "so_what", "observed", "caveat")}}
             for i, d in enumerate(devs)]
    user = (
        f"Client: {client_name}.\n\n## CLIENT CONTEXT\n{brain}\n\n"
        f"## THIS WEEK'S DEVELOPMENTS\n{json.dumps(brief, indent=1)}\n\n"
        f"## THIS WEEK'S FINDINGS BY TAB\n{json.dumps(sections or {}, indent=1)}\n\n"
        f"## WHAT WE COULD NOT SEE\n{json.dumps(coverage)}\n\n"
        f"## SCHEMA\n{STRATEGIST_SCHEMA}"
        + repair
    )
    out = extract_json(model(STRATEGIST_SYSTEM + profile_block(profile, "strategist")
                             + gate_rules(profile) + _never_block(never or []),
                             user, 8000, 0.3))
    recs = {}
    for r in out.get("recommendations") or []:
        if isinstance(r, dict) and isinstance(r.get("index"), int) and r.get("recommendation"):
            recs[r["index"]] = str(r["recommendation"]).strip()
    tab_recs: dict[str, list[dict[str, Any]]] = {}
    raw_tabs = out.get("section_recommendations")
    if isinstance(raw_tabs, dict):
        for tab in ("search", "paid", "social", "owned"):
            items = raw_tabs.get(tab)
            if isinstance(items, list):
                tab_recs[tab] = [x for x in items if isinstance(x, dict)][:3]
    return recs, tab_recs


def alias_ids(v: Any, ref_of: dict[str, str]) -> Any:
    """Real ids to refs in every signal_ids list: the inverse of unalias."""
    if isinstance(v, dict):
        return {k: ([ref_of.get(i, i) if isinstance(i, str) else i for i in x]
                    if k == "signal_ids" and isinstance(x, list) else alias_ids(x, ref_of))
                for k, x in v.items()}
    if isinstance(v, list):
        return [alias_ids(x, ref_of) for x in v]
    return v


# ── Stage 3: assemble ────────────────────────────────────────────────────────


def order_developments(
    devs: list[dict[str, Any]], signals_by_id: dict[str, dict[str, Any]], ceiling: int
) -> list[dict[str, Any]]:
    """Order by the best collector rank among the signals each development cites.

    A development resting only on unranked signals (ads, search) keeps the Analyst's
    order behind the ranked ones. Cut to the ceiling: drop the fourth and fifth, never
    shorten all of them.
    """
    def key(pair: tuple[int, dict[str, Any]]) -> tuple:
        i, d = pair
        ranks = [_rank(signals_by_id[x]) for x in (d.get("signal_ids") or [])
                 if x in signals_by_id and signals_by_id[x]["signal_type"] in RANKED_TYPES]
        return (min(ranks) if ranks else UNRANKED, i)

    good = [d for d in devs if isinstance(d, dict)]
    return [d for _, d in sorted(enumerate(good), key=key)][:ceiling]


def enrich(dev: dict[str, Any], signals_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Attach what code knows better than the model: the link and the caveat."""
    cited = [signals_by_id[x] for x in (dev.get("signal_ids") or []) if x in signals_by_id]
    # Always from a cited signal, never the model's: a plausible invented URL is the
    # worst kind of source link, because it looks checkable.
    dev.pop("source_url", None)
    for s in cited:
        if s.get("source_url"):
            dev["source_url"] = s["source_url"]
            break
    for s in cited:
        d = s.get("data") or {}
        if d.get("applies_locally") == "unknown":
            dev["applies_locally"] = "unknown"
            if d.get("caveat"):
                dev["caveat"] = d["caveat"]
            break
    # A directional caveat (Semrush) belongs to a development built on directional
    # data. One keyword signal among eight ads must not hang a search caveat on an ad
    # story (week of 21 Sep: VIDA's launches carried Semrush's caveat).
    directional = [s for s in cited if (s.get("data") or {}).get("accuracy") == "directional"]
    if "caveat" not in dev and cited and len(directional) * 2 >= len(cited):
        for s in directional:
            if (s.get("data") or {}).get("caveat"):
                dev["caveat"] = s["data"]["caveat"]
                break
    return dev


COMPONENT_NAMES = {"paid": "paid", "search": "search", "web": "website",
                   "email": "email", "social": "organic social"}


def pressure_coverage(p: dict[str, Any]) -> list[str]:
    """Say, from code, what each score is compared against this week.

    Every channel is scored from its first week. Until a competitor has four weeks of
    its own history on a channel, that channel is compared with the rest of the set in
    the same week, and the reader should know which comparison a number rests on.
    """
    set_based: set[str] = set()
    for c in p.get("competitors") or []:
        for name, comp in (c.get("components") or {}).items():
            if comp.get("basis") in ("set", "blend"):
                set_based.add(name)
    if not set_based:
        return []
    names = ", ".join(COMPONENT_NAMES.get(x, x) for x in sorted(set_based))
    return [f"{names[0].upper() + names[1:]} scores compare each competitor with the rest of "
            f"the set this week. Each moves to that competitor's own normal as four weeks "
            f"of its history build up. Paid compares new launches only, never how many "
            f"ads a brand runs, so size alone never reads as pressure."]


def pressure_for_digest(p: dict[str, Any]) -> dict[str, Any]:
    """The part of the momentum result the Analyst sees: enough to explain, no more."""
    def slim(r: dict[str, Any]) -> dict[str, Any]:
        z = r.get("metric_z") or {}
        return {k: r.get(k) for k in ("status", "score", "trend", "metrics")} | {
            # The only metrics this score measures. A metric with too little history
            # (fewer than four weeks) is shown in metrics but moves nothing.
            "scored_on": sorted(k for k, v in z.items() if v is not None),
            # Per channel: a 0-100 score and what it is compared against. "own" is the
            # competitor's own normal, "set" the rest of the set this week, "blend" both.
            "components": {k: v for k, v in (r.get("components") or {}).items()
                           if v.get("score") is not None},
            "events": [{"kind": e["kind"], "signal_id": e["signal_id"], "note": e["note"]}
                       for e in r.get("events") or []]}
    return {
        "market": slim(p["market"]),
        "driver": p.get("driver"),
        "competitors": [{"competitor": c["competitor"], **slim(c)} for c in p["competitors"]],
    }


def synthesize(
    *,
    client: dict[str, Any],
    digest: dict[str, Any],
    signals: list[dict[str, Any]],
    prior_score: int | None,
    pressure: dict[str, Any],
    model: ModelFn,
) -> tuple[dict[str, Any], vb.Report]:
    """Digest in, briefing row out. No database, so it runs end to end in tests.

    pressure is momentum.score_week()'s result, computed before the model runs.
    """
    profile = client.get("output_profile") or "operator"
    lo, hi = DEV_RANGE.get(profile, DEV_RANGE["operator"])
    by_id = {s["id"]: s for s in signals}

    never_terms = list((client.get("config") or {}).get("never_in_writing") or [])
    digest = dict(digest, pressure=pressure_for_digest(pressure),
                  coverage=digest["coverage"] + pressure_coverage(pressure))
    aliased, real = alias_digest(digest)
    ref_of = {i: r for r, i in real.items()}

    # One repair round. A near miss (a word over a cap, a banned phrase) goes back to the
    # model with the exact failures; the gate then judges the second answer as strictly
    # as the first. Still failing after that, the week is held as before.
    repair = srepair = ""
    for attempt in (1, 2):
        raw = run_analyst(model, aliased, profile, never_terms, repair)
        row, rep = _assemble(unalias(raw, real), client=client, digest=digest,
                             by_id=by_id, profile=profile, lo=lo, hi=hi,
                             prior_score=prior_score, pressure=pressure, model=model,
                             never_terms=never_terms, strategist_repair=srepair,
                             ref_of=ref_of, real=real)
        row["full_report"]["validation"]["attempts"] = attempt
        if rep.ok or attempt == 2:
            return row, rep
        repair = _repair_block(raw, rep.failures, row["developments"], ref_of)
        recs = [f for f in rep.failures if ".recommendation" in f]
        srepair = ("\n\n## YOUR PREVIOUS RECOMMENDATIONS FAILED REVIEW\n"
                   + "\n".join(f"- {f}" for f in _name_devs(recs, row["developments"]))
                   + "\nWrite every recommendation again within the rules.") if recs else ""
    raise AssertionError("unreachable")


def _repair_block(raw: dict[str, Any], failures: list[str], devs: list[dict[str, Any]],
                  ref_of: dict[str, str]) -> str:
    lines = []
    for f in _name_devs([f for f in failures if ".recommendation" not in f], devs):
        for real_id, ref in ref_of.items():
            f = f.replace(real_id, ref)
        lines.append(f"- {f}")
    return ("\n\n## YOUR PREVIOUS ANSWER FAILED REVIEW\n"
            "Return the complete corrected JSON object. Fix every failure listed; change "
            "nothing else. Word caps are hard limits: rewrite shorter, do not trim "
            "mid-sentence. Cite only refs that appear in the digest.\n"
            + "\n".join(lines)
            + "\n\n## YOUR PREVIOUS ANSWER\n" + json.dumps(raw, indent=1))


CHANNEL_OF = {"ad_active": "paid", "social_post": "social", "social_profile": "social",
              "email": "email", "web_change": "web"}


def _seen(sig: dict[str, Any]) -> str | None:
    d = sig.get("data") or {}
    v = d.get("start_date") or d.get("posted_at") or d.get("received_at") or sig.get("collected_at")
    return str(v)[:10] if v else None


def promotion_meta(items: Any, by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Channels and first/last seen for each promotion, from the signals it cites."""
    out = []
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        cited = [by_id[i] for i in (x.get("signal_ids") or []) if i in by_id]
        dates = sorted(d for d in (_seen(c) for c in cited) if d)
        chans = sorted({CHANNEL_OF[c["signal_type"]] for c in cited if c.get("signal_type") in CHANNEL_OF})
        out.append(dict(x, channels=chans, first_seen=dates[0] if dates else None,
                        last_seen=dates[-1] if dates else None))
    return out


def campaign_channels(items: Any, by_id: dict[str, dict[str, Any]]
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep a campaign signal only if its cited signals really span two channels.

    The model names the theme; code checks the evidence. Channels come from the cited
    signals' types, never from what the model says, and confidence follows from how
    many there are: two is medium, three or more is high.
    """
    keep, drop = [], []
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        chans = sorted({CHANNEL_OF[by_id[i]["signal_type"]]
                        for i in (x.get("signal_ids") or [])
                        if i in by_id and by_id[i].get("signal_type") in CHANNEL_OF})
        if len(chans) < 2:
            drop.append(x)
            continue
        keep.append(dict(x, channels=chans, confidence="high" if len(chans) >= 3 else "medium"))
    return keep, drop


def _name_devs(failures: list[str], devs: list[dict[str, Any]]) -> list[str]:
    """dev[n] is the gate's position after ordering; the model needs the headline."""
    out = []
    for f in failures:
        m = re.match(r"dev\[(\d+)\]", f)
        if m and int(m.group(1)) < len(devs):
            f += f' (the development headlined "{devs[int(m.group(1))].get("headline")}")'
        out.append(f)
    return out


def _assemble(analysis: dict[str, Any], *, client: dict[str, Any], digest: dict[str, Any],
              by_id: dict[str, Any], profile: str, lo: int, hi: int,
              prior_score: int | None, pressure: dict[str, Any], model: ModelFn,
              never_terms: list[str], strategist_repair: str = "",
              ref_of: dict[str, str] | None = None, real: dict[str, str] | None = None,
              ) -> tuple[dict[str, Any], vb.Report]:
    raw_devs = [d for d in (analysis.get("developments") or []) if isinstance(d, dict)]
    suppressed = []
    if profile == "executive":
        # One signal is omitted, not compressed into a confident headline. Dropping it
        # here costs one development; leaving it for the gate would hold the week.
        keep = []
        for d in raw_devs:
            ids = {x for x in (d.get("signal_ids") or []) if isinstance(x, str)}
            low = str(d.get("confidence") or "").lower() == "low"
            (keep if len(ids) >= 2 and not low else suppressed).append(d)
        raw_devs = keep
    devs = order_developments(raw_devs, by_id, hi)
    devs = [enrich(d, by_id) for d in devs]

    found = analysis.get("sections") if isinstance(analysis.get("sections"), dict) else {}
    recs, tab_recs = run_strategist(model, devs, client.get("brain") or "", digest["coverage"],
                                    client["name"], profile, never_terms, strategist_repair,
                                    sections=alias_ids(found, ref_of or {}))
    tab_recs = unalias(tab_recs, real or {})
    for i, d in enumerate(devs):
        if i in recs:
            d["recommendation"] = recs[i]

    score = pressure["market"]["score"]
    summary = str(analysis.get("summary") or "").strip()
    merged = {t: (dict(v) if isinstance(v, dict) else {}) for t, v in found.items()}
    for t, items in tab_recs.items():
        merged.setdefault(t, {})["recommendations"] = items
    campaigns, not_campaigns = campaign_channels(
        (merged.get("overview") or {}).get("campaign_signals"), by_id)
    merged.setdefault("overview", {})["campaign_signals"] = campaigns
    merged["overview"]["promotions"] = promotion_meta(merged["overview"].get("promotions"), by_id)
    # The same shape filter and uncited-drop as the findings, and then the same gate.
    sections, uncited = _sections(merged)

    row = {
        "client_id": client["id"],
        "week_of": digest["week_of"],
        "pressure_score": score,
        "summary": summary,
        "developments": devs,
        "full_report": {
            "version": 1,
            "profile": profile,
            "model": MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pressure": {
                "method": "momentum-v2",
                "status": pressure["market"]["status"],
                "score": score,
                "trend": pressure["market"]["trend"],
                "market": pressure["market"],
                "competitors": pressure["competitors"],
                "driver": pressure["driver"],
                "prior_score": prior_score,
                "delta": (score - prior_score) if (score is not None and prior_score is not None) else None,
            },
            "sections": sections,
            "coverage": digest["coverage"],
        },
    }

    never = vb.NEVER_IN_WRITING + vb.terms_to_patterns(
        (client.get("config") or {}).get("never_in_writing"))
    rep = vb.validate({"summary": summary, "developments": devs, "sections": sections},
                      digest_signal_ids(digest), profile, never_in_writing=never)
    row["full_report"]["validation"] = {
        "ok": rep.ok, "failures": rep.failures, "warnings": rep.warnings,
    }
    for d in suppressed:
        row["full_report"]["validation"]["warnings"].append(
            f"suppressed single-signal or low-confidence development: "
            f"{str(d.get('headline'))[:80]}")
    for x in not_campaigns:
        row["full_report"]["validation"]["warnings"].append(
            f"dropped campaign signal on one channel: {str(x.get('theme'))[:80]}")
    for where, x in uncited:
        row["full_report"]["validation"]["warnings"].append(
            f"dropped uncited {where} item: {str(x.get('observation') or x.get('message'))[:80]}")
    if len(devs) < lo:
        row["full_report"]["validation"]["warnings"].append(
            f"only {len(devs)} development(s); the summary must say the week was quiet")
    return row, rep


def _sections(s: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, dict]]]:
    """Keep exactly the promised keys per tab, so the page can rely on them.

    An item citing no signal is a coverage note written as a finding ("email monitoring
    had not started"). Coverage already says it, from code, so the item is dropped and
    logged rather than holding the whole week at the gate. An item citing an id that is
    not in the digest is kept: that is fabrication, and the gate must see it.
    """
    shape = {
        "overview": ("promotions", "campaign_signals"),
        "search": ("keyword_movement", "demand_shifts", "recommendations"),
        "paid": ("live_ad_creative", "spend_signals", "recommendations"),
        "social": ("audience_cadence", "content_themes", "recommendations"),
        "owned": ("website_changes", "email_programs", "recommendations"),
    }
    out: dict[str, Any] = {}
    dropped: list[tuple[str, dict]] = []
    for tab, keys in shape.items():
        src = s.get(tab) if isinstance(s.get(tab), dict) else {}
        out[tab] = {}
        for k in keys:
            keep = []
            for x in src.get(k) or []:
                if not isinstance(x, dict):
                    continue
                ids = [i for i in (x.get("signal_ids") or []) if isinstance(i, str) and i]
                (keep.append(x) if ids else dropped.append((f"{tab}.{k}", x)))
            out[tab][k] = keep
    return out, dropped


# ── database ─────────────────────────────────────────────────────────────────


def load(sb: Supa, slug: str, wk: date) -> dict[str, Any]:
    clients = sb.get("public", "clients", {
        "slug": f"eq.{slug}", "select": "id,name,slug,brain,output_profile,config"})
    if not clients:
        raise SystemExit(f"no client with slug {slug}")
    client = clients[0]
    cid = client["id"]
    everyone = sb.get("portal", "competitors", {
        "client_id": f"eq.{cid}", "active": "eq.true",
        "select": "id,name,domain,single_market,prices_by_location,display_local,monitored_site,is_client",
        "order": "name"})
    # The client's own row is a benchmark. It is collected like a competitor and kept out
    # of everything competitive: the digest, the pressure score, the set reference.
    competitors = [c for c in everyone if not c.get("is_client")]
    client_self = next((c for c in everyone if c.get("is_client")), None)
    comp_ids = ",".join(c["id"] for c in competitors) or "00000000-0000-0000-0000-000000000000"
    email_ch = sb.get("portal", "channels", {
        "competitor_id": f"in.({comp_ids})", "purpose": "eq.email", "active": "eq.true",
        "select": "competitor_id"})
    social_ch = sb.get("portal", "channels", {
        "competitor_id": f"in.({comp_ids})", "purpose": "eq.organic_social",
        "active": "eq.true", "select": "id,competitor_id"})
    # Channels retired because the platform will not show them to a logged-out reader.
    # Recorded as `UNREADABLE: <reason>` in notes, and said out loud in coverage, so a
    # missing channel is a stated gap rather than a silent one.
    retired = sb.get("portal", "channels", {
        "competitor_id": f"in.({comp_ids})", "purpose": "eq.organic_social",
        "active": "eq.false", "notes": "like.UNREADABLE*",
        "select": "competitor_id,platform,location_label,notes"})
    cname = {c["id"]: c["name"] for c in competitors}
    unreadable = [
        f"{cname.get(r['competitor_id'], '?')} {r.get('location_label') or ''} "
        f"{r['platform'].title()} cannot be read: "
        f"{r['notes'].split(':', 1)[1].strip().rstrip('.')}.".replace("  ", " ")
        for r in retired]
    rollups = sb.get("portal", "ad_geo_weekly", {
        "client_id": f"eq.{cid}", "week_of": f"eq.{wk.isoformat()}", "select": "*"})
    prior_rollups = sb.get("portal", "ad_geo_weekly", {
        "client_id": f"eq.{cid}", "week_of": f"eq.{(wk - timedelta(days=7)).isoformat()}",
        "select": "*"})
    prior = sb.get("portal", "briefings", {
        "client_id": f"eq.{cid}", "week_of": f"lt.{wk.isoformat()}",
        "select": "pressure_score", "order": "week_of.desc", "limit": "1"})
    all_signals = fetch_week_signals(sb, cid, wk)
    self_id = client_self["id"] if client_self else None
    signals = [x for x in all_signals if not self_id or x.get("competitor_id") != self_id]
    self_signals = [x for x in all_signals if self_id and x.get("competitor_id") == self_id]
    if self_id:
        rollups = [r for r in rollups if r.get("competitor_id") != self_id]
        prior_rollups = [r for r in prior_rollups if r.get("competitor_id") != self_id]
    self_social = sb.get("portal", "channels", {
        "competitor_id": f"eq.{self_id}", "purpose": "eq.organic_social",
        "active": "eq.true", "select": "id"}) if self_id else []
    history_rows = sb.get("portal", "pressure_weekly", {
        "client_id": f"eq.{cid}",
        "week_of": f"gte.{(wk - timedelta(weeks=mo_lookback())).isoformat()}",
        "and": f"(week_of.lt.{wk.isoformat()})",
        "select": "competitor_id,week_of,metrics", "order": "week_of"})
    history: dict[str | None, list[dict[str, float]]] = {}
    for h in history_rows:
        history.setdefault(h.get("competitor_id"), []).append(h.get("metrics") or {})
    snapshots = sb.get("portal", "signals", {
        "client_id": f"eq.{cid}", "week_of": f"eq.{wk.isoformat()}",
        "signal_type": "eq.page_snapshot", "select": "id", "limit": "1"})
    # Email "ran" for a week only if the portal inbox was already receiving before the
    # week the briefing covers began: week W reads email sent in [W-7, W) (see
    # week_window.py). Before the webhook existed, no email is unknown, not zero, and a
    # zero there would sit in every competitor's median for a quarter.
    first_mail = sb.get("portal", "inbound_emails", {
        "client_id": f"eq.{cid}",
        "select": "received_at", "order": "received_at.asc", "limit": "1"})
    ran = set()
    if first_mail and str(first_mail[0]["received_at"])[:10] <= (wk - timedelta(days=7)).isoformat():
        ran.add("email")
    if rollups:
        ran.add("ads")
    if snapshots:
        ran.add("web")
    if any(s["signal_type"] == "tracked_keyword_positions" for s in signals):
        ran.add("search")
    if any(s["signal_type"] == "social_profile" for s in signals):
        ran.add("social")
    return {
        "client": client, "competitors": competitors, "rollups": rollups,
        "prior_rollups": prior_rollups, "signals": signals,
        "email_channels": {c["competitor_id"] for c in email_ch},
        "prior_score": prior[0]["pressure_score"] if prior else None,
        "history": history, "ran": ran, "unreadable": unreadable,
        "social_channels": {c: {x["id"] for x in social_ch if x["competitor_id"] == c}
                            for c in {x["competitor_id"] for x in social_ch}},
        "watch_terms": ((client.get("config") or {}).get("watch_terms") or []),
        "client_self": client_self, "self_signals": self_signals,
        "self_social_channels": {self_id: {x["id"] for x in self_social}} if self_id else {},
    }


def mo_lookback() -> int:
    from calibrate import LOOKBACK
    return LOOKBACK


def compute_pressure(ctx: dict[str, Any], wk: date) -> tuple[dict[str, Any], dict]:
    per_comp = mo.week_metrics(ctx["signals"], ctx["competitors"], wk, ran=ctx["ran"],
                               email_channels=ctx["email_channels"],
                               watch_terms=ctx["watch_terms"],
                               social_channels=ctx.get("social_channels"))
    pressure = mo.score_week(per_comp, ctx["history"], ctx["competitors"])
    me = ctx.get("client_self")
    if me:
        mine = mo.week_metrics(ctx.get("self_signals") or [], [me], wk, ran=ctx["ran"],
                               email_channels=set(), watch_terms=[],
                               social_channels=ctx.get("self_social_channels"))
        b = mo.score_benchmark(mine[me["id"]], ctx["history"].get(me["id"], []), per_comp)
        pressure["benchmark"] = {"competitor_id": me["id"], "competitor": me["name"],
                                 "metrics": mine[me["id"]]["metrics"], "events": [], **b}
    return pressure, per_comp


def pressure_rows(client_id: str, wk: date, p: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per competitor plus the market row (competitor_id NULL)."""
    def row(cid: str | None, r: dict[str, Any]) -> dict[str, Any]:
        return {"client_id": client_id, "competitor_id": cid, "week_of": wk.isoformat(),
                "metrics": r["metrics"], "metric_z": r.get("metric_z") or {},
                "events": r.get("events") or [], "event_points": r.get("event_points") or 0,
                "score": r["score"], "status": r["status"], "trend": r.get("trend"),
                "components": r.get("components") or {}, "basis": r.get("basis") or {},
                "method": "momentum-v2"}
    rows = [row(None, p["market"])] + [row(c["competitor_id"], c) for c in p["competitors"]]
    if p.get("benchmark"):
        rows.append(row(p["benchmark"]["competitor_id"], p["benchmark"]))
    return rows


def benchmark_rows(rows: list[dict], self_id: str) -> list[dict]:
    """The --benchmark-only write: the client's own row and nothing else, so scoring the
    benchmark mid-week never moves a competitor's or the market's published score."""
    return [r for r in rows if r.get("competitor_id") == self_id]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--week", help="ISO date inside the target week. Defaults to this week.")
    ap.add_argument("--digest-only", action="store_true",
                    help="build and print the digest, call no model, write nothing")
    ap.add_argument("--dry-run", action="store_true", help="call the model, write nothing")
    ap.add_argument("--benchmark-only", action="store_true",
                    help="score the client's own benchmark row for the week and store that "
                         "row only: no model, no briefing, competitors' rows untouched")
    ap.add_argument("--backfill-pressure", type=int, default=0, metavar="N",
                    help="before this week, compute and store pressure history for the N "
                         "previous weeks from whatever signals they hold (no briefings)")
    args = ap.parse_args()

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 2
    sb = Supa(url, key)
    wk = parse_week(args.week)
    if args.backfill_pressure:
        for i in range(args.backfill_pressure, 0, -1):   # oldest first: each week's
            w = wk - timedelta(weeks=i)                   # history includes the last
            bctx = load(sb, args.client, w)
            if not bctx["signals"]:
                print(f"[synth] backfill {w}: no signals, skipped")
                continue
            bp, _ = compute_pressure(bctx, w)
            print(f"[synth] backfill {w}: ran={sorted(bctx['ran'])} market="
                  f"{bp['market']['status']} {bp['market']['metrics']}")
            if not (args.dry_run or args.digest_only):
                sb.upsert("portal", "pressure_weekly",
                          pressure_rows(bctx["client"]["id"], w, bp),
                          on_conflict="client_id,competitor_id,week_of")
    ctx = load(sb, args.client, wk)
    client = ctx["client"]

    digest = build_digest(ctx["signals"], ctx["competitors"], ctx["rollups"],
                          ctx["prior_rollups"], ctx["email_channels"], client["name"], wk,
                          email_live="email" in ctx["ran"], unreadable=ctx.get("unreadable"))
    print(f"[synth] {client['name']} · week of {wk} · profile={client.get('output_profile')}")
    print(f"[synth] signals: {json.dumps(digest['signal_counts'])}")
    print(f"[synth] ranked changes: {len(digest['ranked_changes'])} · "
          f"paid rows: {len(digest['paid'])} · search rows: {len(digest['search'])}")
    for line in digest["coverage"]:
        print(f"[synth] coverage: {line}")
    pressure, _ = compute_pressure(ctx, wk)
    m = pressure["market"]
    print(f"[synth] pressure: market {m['status']} score={m['score']} trend={m['trend']} "
          f"history={m['history_weeks']}w events={len(m['events'])} driver={pressure['driver']}")
    for c in pressure["competitors"]:
        print(f"[synth]   {c['competitor']:<10} {c['status']:<11} score={c['score']} "
              f"events={[e['kind'] for e in c['events']]} metrics={c['metrics']}")
    if pressure.get("benchmark"):
        b = pressure["benchmark"]
        print(f"[synth]   benchmark {b['competitor']}: {b['status']} score={b['score']} "
              f"components={ {k: v.get('score') for k, v in (b.get('components') or {}).items()} } "
              "(kept out of the market score and the digest)")

    if args.benchmark_only:
        me = ctx.get("client_self")
        if not me or not pressure.get("benchmark"):
            print("[synth] no client benchmark row (competitors.is_client) to score",
                  file=sys.stderr)
            return 1
        rows = benchmark_rows(pressure_rows(client["id"], wk, pressure), me["id"])
        sb.upsert("portal", "pressure_weekly", rows,
                  on_conflict="client_id,competitor_id,week_of")
        print(f"[synth] stored the benchmark row only ({len(rows)}); briefing untouched")
        return 0
    if args.digest_only:
        print(json.dumps(dict(digest, pressure=pressure_for_digest(pressure)), indent=1,
                         default=str))
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY must be set", file=sys.stderr)
        return 2
    if not ctx["signals"]:
        print("[synth] no signals for this week. Writing nothing: an empty briefing is "
              "worse than last week's staying up.", file=sys.stderr)
        return 1
    brain = (client.get("brain") or "").strip()
    if len(brain) < 200 or brain.upper().startswith("AWAITING"):
        print("[synth] client brain is missing or still the placeholder. The Strategist "
              "reads it directly; refusing to write generic recommendations.", file=sys.stderr)
        return 1

    # History first, and before the model runs: a model failure must not leave a hole
    # in every median, and a held week is still a week of competitor behaviour.
    if not args.dry_run:
        sb.upsert("portal", "pressure_weekly", pressure_rows(client["id"], wk, pressure),
                  on_conflict="client_id,competitor_id,week_of")

    row, rep = synthesize(client=client, digest=digest, signals=ctx["signals"],
                          prior_score=ctx["prior_score"], pressure=pressure,
                          model=anthropic_model)
    row["published_at"] = datetime.now(timezone.utc).isoformat() if rep.ok else None

    print(f"[synth] pressure {row['pressure_score']} · {len(row['developments'])} developments")
    print(rep.render())
    if args.dry_run:
        print(json.dumps(row, indent=1, default=str))
        print("[synth] dry run, nothing written")
        return 0 if rep.ok else 1

    sb.upsert("portal", "briefings", [row], on_conflict="client_id,week_of")
    if rep.ok:
        print(f"[synth] PUBLISHED week of {wk}")
        return 0
    print(f"[synth] HELD week of {wk}. The row is written with published_at = NULL and the "
          f"client cannot see it. Fix and re-run, or publish by hand after review.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
