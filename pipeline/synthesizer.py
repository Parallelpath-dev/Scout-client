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
        entry["started_in_last_7_days"] = len(
            [a for a in acquisition
             if (_date((a.get("data") or {}).get("start_date")) or date.min) >= cutoff])
        entry["new_ads"] = [ad_item(a) for a in new[:MAX_NEW_ADS]]
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


# ── Stage 2: the model ───────────────────────────────────────────────────────

ANALYST_SYSTEM = """You are the Scout Analyst for a client-facing competitive briefing.
You receive a structured digest of one week of competitor signals and fill a JSON schema.
No recommendations. What happened, what changed, what it means for the client.

Hard rules:
1. Only reference competitors, numbers, offers and dates present in the digest. Never
   introduce a fact from memory, including facts about these brands you believe are true.
2. Every development and every section item cites the signal_id values it rests on,
   copied exactly from the digest. Never invent or alter an id.
3. Respond with one JSON object matching the schema. No prose outside it, no markdown.

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
pressure beyond the events. When you mention it, name the driver and what moved."""

ANALYST_SCHEMA = """{
  "summary": "<answers first: does anything competitors did change the client's plans? then what>",
  "developments": [
    {
      "competitor": "<name>",
      "headline": "<competitor + verb, 10 words max>",
      "so_what": "<why it matters to the client, 25 words max>",
      "observed": "<what the signals show, stated as observation, 40 words max>",
      "confidence": "high|medium|low",
      "signal_ids": ["<id>", "<id>"]
    }
  ],
  "sections": {
    "search": {
      "keyword_movement": [{"keyword": "<kw>", "observation": "<40 words max>", "signal_ids": ["<id>"]}],
      "demand_shifts": []
    },
    "paid": {
      "live_ad_creative": [{"competitor": "<name>", "message": "<what the ads lead with>", "format": "<format>", "signal_ids": ["<id>"]}],
      "spend_signals": [{"competitor": "<name>", "observation": "<40 words max>", "signal_ids": ["<id>"]}]
    },
    "social": {"audience_cadence": [], "content_themes": []},
    "owned": {
      "website_changes": [{"competitor": "<name>", "observation": "<40 words max>", "applies_locally": "yes|unknown|no", "signal_ids": ["<id>"]}],
      "email_programs": [{"competitor": "<name>", "observation": "<40 words max>", "signal_ids": ["<id>"]}]
    }
  }
}"""

STRATEGIST_SYSTEM = """You are the Scout Strategist. You receive this week's ranked
developments and the client's strategic context, and write one recommended action for
each development. You never introduce events or data that are not in the developments.

Hard rules:
1. One action per development, specific enough to brief someone on Monday.
2. Respect the client context. Where it marks something inferred or assumed, label any
   recommendation resting on it as inference.
3. Anything the client context says is raised on a call and never in writing stays out
   of your output entirely. That includes any comparison of the client's own locations
   competing with each other for members.
4. Respond with one JSON object matching the schema. No prose outside it, no markdown."""

STRATEGIST_SCHEMA = """{
  "recommendations": [
    {"index": <the development's index>, "recommendation": "<30 words max>"}
  ]
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


def anthropic_model(system: str, user: str, max_tokens: int, temperature: float) -> str:
    from anthropic import Anthropic  # imported here so tests need no SDK

    resp = Anthropic().messages.create(
        model=MODEL, max_tokens=max_tokens, temperature=temperature,
        system=system, messages=[{"role": "user", "content": user}],
    )
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise RuntimeError("model hit max_tokens; output is truncated, refusing to parse it")
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _never_block(terms: list[str]) -> str:
    if not terms:
        return ""
    return ("\n\nNever write any of these words, in any field, for any reason: "
            + ", ".join(f'"{t}"' for t in terms)
            + ". A briefing that contains one is held and the client receives nothing.")


def run_analyst(model: ModelFn, digest: dict[str, Any], profile: str,
                never: list[str] | None = None) -> dict[str, Any]:
    user = (
        f"Client: {digest['client']}. Week of {digest['week_of']}.\n\n"
        f"## DIGEST\n{json.dumps(digest, indent=1, default=str)}\n\n"
        f"## SCHEMA\n{ANALYST_SCHEMA}"
    )
    return extract_json(model(ANALYST_SYSTEM + profile_block(profile, "analyst")
                              + _never_block(never or []), user, 10000, 0.1))


def run_strategist(
    model: ModelFn, devs: list[dict[str, Any]], brain: str, coverage: list[str],
    client_name: str, profile: str, never: list[str] | None = None,
) -> dict[int, str]:
    if not devs:
        return {}
    brief = [{"index": i, **{k: d.get(k) for k in
              ("competitor", "headline", "so_what", "observed", "caveat")}}
             for i, d in enumerate(devs)]
    user = (
        f"Client: {client_name}.\n\n## CLIENT CONTEXT\n{brain}\n\n"
        f"## THIS WEEK'S DEVELOPMENTS\n{json.dumps(brief, indent=1)}\n\n"
        f"## WHAT WE COULD NOT SEE\n{json.dumps(coverage)}\n\n"
        f"## SCHEMA\n{STRATEGIST_SCHEMA}"
    )
    out = extract_json(model(STRATEGIST_SYSTEM + profile_block(profile, "strategist")
                             + _never_block(never or []), user, 8000, 0.3))
    recs = {}
    for r in out.get("recommendations") or []:
        if isinstance(r, dict) and isinstance(r.get("index"), int) and r.get("recommendation"):
            recs[r["index"]] = str(r["recommendation"]).strip()
    return recs


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
    if "caveat" not in dev:
        for s in cited:
            d = s.get("data") or {}
            if d.get("accuracy") == "directional" and d.get("caveat"):
                dev["caveat"] = d["caveat"]
                break
    return dev


def pressure_for_digest(p: dict[str, Any]) -> dict[str, Any]:
    """The part of the momentum result the Analyst sees: enough to explain, no more."""
    def slim(r: dict[str, Any]) -> dict[str, Any]:
        return {k: r.get(k) for k in ("status", "score", "trend", "metrics", "metric_z")} | {
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
    digest = dict(digest, pressure=pressure_for_digest(pressure))
    analysis = run_analyst(model, digest, profile, never_terms)
    raw_devs = [d for d in (analysis.get("developments") or []) if isinstance(d, dict)]
    suppressed = []
    if profile == "executive":
        # One signal is omitted, not compressed into a confident headline. Dropping it
        # here costs one development; leaving it for the gate would hold the week.
        keep = []
        for d in raw_devs:
            ids = {x for x in (d.get("signal_ids") or []) if isinstance(x, str)}
            (keep if len(ids) >= 2 else suppressed).append(d)
        raw_devs = keep
    devs = order_developments(raw_devs, by_id, hi)
    devs = [enrich(d, by_id) for d in devs]

    recs = run_strategist(model, devs, client.get("brain") or "", digest["coverage"],
                          client["name"], profile, never_terms)
    for i, d in enumerate(devs):
        if i in recs:
            d["recommendation"] = recs[i]

    score = pressure["market"]["score"]
    summary = str(analysis.get("summary") or "").strip()

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
                "method": "momentum-v1",
                "status": pressure["market"]["status"],
                "score": score,
                "trend": pressure["market"]["trend"],
                "market": pressure["market"],
                "competitors": pressure["competitors"],
                "driver": pressure["driver"],
                "prior_score": prior_score,
                "delta": (score - prior_score) if (score is not None and prior_score is not None) else None,
            },
            "sections": _sections(analysis.get("sections") or {}),
            "coverage": digest["coverage"],
        },
    }

    never = vb.NEVER_IN_WRITING + vb.terms_to_patterns(
        (client.get("config") or {}).get("never_in_writing"))
    sections = row["full_report"]["sections"]
    rep = vb.validate({"summary": summary, "developments": devs, "sections": sections},
                      digest_signal_ids(digest), profile, never_in_writing=never)
    row["full_report"]["validation"] = {
        "ok": rep.ok, "failures": rep.failures, "warnings": rep.warnings,
    }
    for d in suppressed:
        row["full_report"]["validation"]["warnings"].append(
            f"suppressed single-signal development: {str(d.get('headline'))[:80]}")
    if len(devs) < lo:
        row["full_report"]["validation"]["warnings"].append(
            f"only {len(devs)} development(s); the summary must say the week was quiet")
    return row, rep


def _sections(s: dict[str, Any]) -> dict[str, Any]:
    """Keep exactly the promised keys per tab, so the page can rely on them."""
    shape = {
        "search": ("keyword_movement", "demand_shifts"),
        "paid": ("live_ad_creative", "spend_signals"),
        "social": ("audience_cadence", "content_themes"),
        "owned": ("website_changes", "email_programs"),
    }
    out = {}
    for tab, keys in shape.items():
        src = s.get(tab) if isinstance(s.get(tab), dict) else {}
        out[tab] = {k: [x for x in (src.get(k) or []) if isinstance(x, dict)] for k in keys}
    return out


# ── database ─────────────────────────────────────────────────────────────────


def load(sb: Supa, slug: str, wk: date) -> dict[str, Any]:
    clients = sb.get("public", "clients", {
        "slug": f"eq.{slug}", "select": "id,name,slug,brain,output_profile,config"})
    if not clients:
        raise SystemExit(f"no client with slug {slug}")
    client = clients[0]
    cid = client["id"]
    competitors = sb.get("portal", "competitors", {
        "client_id": f"eq.{cid}", "active": "eq.true",
        "select": "id,name,domain,single_market,prices_by_location,display_local,monitored_site",
        "order": "name"})
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
    signals = fetch_week_signals(sb, cid, wk)
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
    }


def mo_lookback() -> int:
    from calibrate import LOOKBACK
    return LOOKBACK


def compute_pressure(ctx: dict[str, Any], wk: date) -> tuple[dict[str, Any], dict]:
    per_comp = mo.week_metrics(ctx["signals"], ctx["competitors"], wk, ran=ctx["ran"],
                               email_channels=ctx["email_channels"],
                               watch_terms=ctx["watch_terms"],
                               social_channels=ctx.get("social_channels"))
    return mo.score_week(per_comp, ctx["history"], ctx["competitors"]), per_comp


def pressure_rows(client_id: str, wk: date, p: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per competitor plus the market row (competitor_id NULL)."""
    def row(cid: str | None, r: dict[str, Any]) -> dict[str, Any]:
        return {"client_id": client_id, "competitor_id": cid, "week_of": wk.isoformat(),
                "metrics": r["metrics"], "metric_z": r.get("metric_z") or {},
                "events": r.get("events") or [], "event_points": r.get("event_points") or 0,
                "score": r["score"], "status": r["status"], "trend": r.get("trend"),
                "method": "momentum-v1"}
    return [row(None, p["market"])] + [row(c["competitor_id"], c) for c in p["competitors"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--week", help="ISO date inside the target week. Defaults to this week.")
    ap.add_argument("--digest-only", action="store_true",
                    help="build and print the digest, call no model, write nothing")
    ap.add_argument("--dry-run", action="store_true", help="call the model, write nothing")
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
