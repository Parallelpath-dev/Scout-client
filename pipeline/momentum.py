"""
Scout — competitive pressure, counted by code and calibrated per competitor.

Replaces the model-judged pressure score. The internal tool asks a model for an absolute
0-100 each week with no memory of previous weeks, so it scores how big a competitor is
rather than what changed, and the number sits still (EVP read 42 in seven of nine
weeks). Here the model scores nothing. It is told the result and explains it.

THREE STEPS
-----------
1. COUNT. Per competitor per week, a handful of metrics from signals, each observation
   weighted by how much it is about THIS market:

       ads_active     acquisition ads running           (hiring ads excluded)
       ads_launched   acquisition ads started this week
       web            surfaced page changes, material 3 / minor 1
       email          emails, material 3 / other 1       (confirmations excluded)
       search         visibility on the DC-tracked keywords, sum of (21 - position)
       social_posts   organic posts published in the week

   Geo weight: 1.0 when the content names DC or lands on a DC page, or the brand
   operates only in this market; 0.5 regional; 0.25 no geography; 0 another market.
   A web change that may not apply here (applies_locally unknown) counts half.
   A social post counts in full from a location account (channel scope local), and
   otherwise by what it says, like an ad: the regional DMV feed and the corporate feed
   count only when the post names DC.

2. CALIBRATE each metric against that competitor's own last 12 weeks (calibrate.py),
   and combine the metric z-scores by channel weight. A big advertiser having a normal
   week reads 50. The market row does the same on the summed metrics.

3. EVENTS add fixed points on top, because the moves that matter most before an
   opening are too rare to move a median: a price change, an offer, a new location,
   and any mention of the client's watch terms (config.watch_terms, e.g. the site of
   the opening). Capped, so one busy week cannot pin the scale.

Until a metric has four weeks of history it contributes nothing, and a row with no
calibrated metric at all is "calibrating": events are still recorded and reported,
the score is NULL. No number is shown before there is a normal to compare it to.

Why not backfill paid history from the start dates of the ads running now: those are
the survivors. Ads that started eight weeks ago and have since ended are gone, so
older weeks would count low and every current week would read as a ramp.

Pure: no network, no database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from calibrate import robust_z, to_score, trend

GEO_WEIGHT = {"dc_landing": 1.0, "dc_explicit": 1.0, "regional": 0.5,
              "none": 0.25, "other_market": 0.0}
APPLIES_WEIGHT = {"yes": 1.0, "unknown": 0.5, "no": 0.0}
MATERIALITY_POINTS = {"material": 3.0, "minor": 1.0}
QUIET_EMAIL = {"confirmation", "transactional"}

# How much each metric counts in the combined z. Channel shares follow the internal
# tool (social 30, owned 25, paid 20, search 15, news 10); social and news have no
# collector yet, so their weight is shared out over the metrics that exist.
METRIC_WEIGHT = {
    "social_posts": 30,
    "ads_active": 10, "ads_launched": 10,
    "web": 12.5, "email": 12.5,
    "search": 15,
}
# Smallest move worth calling a move, per metric (see calibrate.py on floors).
FLOORS = {
    "ads_active": dict(abs_floor=2.0, rel_floor=0.10),
    "ads_launched": dict(abs_floor=1.0, rel_floor=0.25),
    "web": dict(abs_floor=1.0),
    "email": dict(abs_floor=1.0, rel_floor=0.25),
    "search": dict(abs_floor=3.0, rel_floor=0.10),
    "social_posts": dict(abs_floor=1.0, rel_floor=0.25),
}

EVENT_POINTS = {"watch_term": 20, "location": 15, "price": 10, "offer": 10}
EVENT_CAP = 30


def _date(v: Any) -> date | None:
    if not v:
        return None
    s = str(v)
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s), tz=timezone.utc).date()
        except (ValueError, OverflowError):
            return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def geo_weight(signal: dict[str, Any], single_market: bool) -> float:
    if single_market:
        return 1.0
    return GEO_WEIGHT.get(signal.get("geo_relevance") or "none", 0.25)


def week_metrics(
    signals: list[dict[str, Any]], competitors: list[dict[str, Any]], wk: date,
    *, ran: set[str], email_channels: set[str], watch_terms: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """{competitor_id: {"metrics": {...}, "events": [...]}} for one week.

    ran: which collectors ran this week, from "ads", "web", "email", "search". Passed
    in rather than inferred, because a collector that ran and found nothing leaves no
    signal behind: the web collector's proof of life is its page snapshots, which are
    not evidence and are not in the window.

    Only competitors appear; the client's own search rows are not pressure.
    """
    comp = {c["id"]: c for c in competitors}
    out = {cid: {"metrics": {}, "events": []} for cid in comp}
    terms = [t.lower() for t in (watch_terms or []) if t]
    seen_types: dict[str, set[str]] = {cid: set() for cid in comp}
    launched_from = wk - timedelta(days=7)

    def add(cid: str, k: str, v: float) -> None:
        m = out[cid]["metrics"]
        m[k] = round(m.get(k, 0.0) + v, 3)

    def event(cid: str, kind: str, s: dict[str, Any], note: str) -> None:
        out[cid]["events"].append({"kind": kind, "points": EVENT_POINTS[kind],
                                   "signal_id": s["id"], "note": note[:160]})

    for s in signals:
        cid = s.get("competitor_id")
        if cid not in comp:
            continue
        t = s["signal_type"]
        d = s.get("data") or {}
        seen_types[cid].add(t)
        sm = bool(comp[cid].get("single_market"))

        if t == "ad_active":
            if d.get("is_recruitment"):
                continue
            w = geo_weight(s, sm)
            add(cid, "ads_active", w)
            started = _date(d.get("start_date"))
            if started and started >= launched_from:
                add(cid, "ads_launched", w)

        elif t == "web_change":
            if not d.get("surfaces"):
                continue
            pts = MATERIALITY_POINTS.get(d.get("materiality"), 0.0)
            add(cid, "web", pts * APPLIES_WEIGHT.get(d.get("applies_locally") or "yes", 1.0))
            types = set(d.get("change_types") or [])
            if "price" in types:
                event(cid, "price", s, "; ".join(d.get("evidence") or [])[:160])
            if "offer" in types:
                event(cid, "offer", s, "; ".join(d.get("evidence") or [])[:160])
            if "location" in types:
                event(cid, "location", s, "; ".join(d.get("evidence") or [])[:160])

        elif t == "email":
            kind = d.get("email_type")
            if kind in QUIET_EMAIL:
                continue
            w = 1.0 if sm or s.get("geo_relevance") in ("dc_landing", "dc_explicit") else 0.5
            add(cid, "email", MATERIALITY_POINTS.get(d.get("materiality"), 1.0) * w)
            if kind == "offer":
                event(cid, "offer", s, d.get("subject") or "offer email")
            if kind == "opening":
                event(cid, "location", s, d.get("subject") or "opening email")

        elif t == "social_post":
            if sm or s.get("source_scope") == "local":
                w = 1.0
            else:
                w = max(GEO_WEIGHT.get(s.get("geo_relevance") or "none", 0.25),
                        0.5 if s.get("source_scope") == "regional" else 0.25)
                if s.get("geo_relevance") == "other_market":
                    w = 0.0
            add(cid, "social_posts", w)

        elif t == "tracked_keyword_positions":
            vis = 0.0
            for kw in d.get("keywords") or []:
                p = kw.get("position") if isinstance(kw, dict) else None
                if isinstance(p, (int, float)) and p > 0:
                    vis += max(0.0, 21 - p)
            add(cid, "search", vis)

        # Watch terms, on any signal type. The ad classifier's evidence and the email's
        # subject are where a new-location name turns up first.
        if terms:
            hay = " ".join(str(x) for x in (
                s.get("geo_evidence"), d.get("subject"), d.get("body"), d.get("title"),
                d.get("text") if t == "social_post" else None,
                " ".join(d.get("evidence") or []) if isinstance(d.get("evidence"), list) else "",
            ) if x).lower()
            hit = next((t2 for t2 in terms if t2 in hay), None)
            if hit and not any(e["kind"] == "watch_term" and e["signal_id"] == s["id"]
                               for e in out[cid]["events"]):
                event(cid, "watch_term", s, f'mentions "{hit}"')

    # A collector that ran and found nothing is a real zero. A collector that did not
    # run, or a channel that does not exist, is unknown and must not drag a median down.
    for cid in comp:
        m = out[cid]["metrics"]
        if "ads" in ran:
            m.setdefault("ads_active", 0.0)
            m.setdefault("ads_launched", 0.0)
        if "web" in ran:
            m.setdefault("web", 0.0)
        if "email" in ran and cid in email_channels:
            m.setdefault("email", 0.0)
        if "search" in ran and "tracked_keyword_positions" in seen_types[cid]:
            m.setdefault("search", 0.0)
        # A social profile row is the collector's proof it reached this competitor.
        if "social" in ran and "social_profile" in seen_types[cid]:
            m.setdefault("social_posts", 0.0)
    return out


def market_metrics(per_comp: dict[str, dict[str, Any]]) -> dict[str, float]:
    tot: dict[str, float] = {}
    for row in per_comp.values():
        for k, v in row["metrics"].items():
            tot[k] = round(tot.get(k, 0.0) + v, 3)
    return tot


def score_row(
    metrics: dict[str, float],
    history: list[dict[str, float]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Calibrate one row (a competitor, or the market) against its own history.

    history: that row's metrics for previous weeks, oldest first, this week excluded.
    """
    zs: dict[str, float | None] = {}
    for k, v in metrics.items():
        zs[k] = robust_z(v, [h.get(k) for h in history], **FLOORS.get(k, {}))
    live = {k: METRIC_WEIGHT.get(k, 10) for k, z in zs.items() if z is not None}
    bonus = min(EVENT_CAP, sum(e["points"] for e in events))
    if not live:
        return {"status": "calibrating", "score": None, "z": None, "metric_z": zs,
                "event_points": bonus, "trend": None, "history_weeks": len(history)}
    z = sum(zs[k] * w for k, w in live.items()) / sum(live.values())
    # Trend on the combined level, so it survives the ramp being absorbed as normal.
    def level(m: dict[str, float]) -> float:
        return sum(m.get(k, 0.0) * METRIC_WEIGHT.get(k, 10) for k in live)
    tr = trend([level(h) for h in history] + [level(metrics)])
    return {"status": "scored", "score": to_score(z, bonus), "z": round(z, 3),
            "metric_z": {k: (round(v, 3) if v is not None else None) for k, v in zs.items()},
            "event_points": bonus, "trend": tr, "history_weeks": len(history)}


def score_week(
    per_comp: dict[str, dict[str, Any]],
    history: dict[str | None, list[dict[str, float]]],
    competitors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score every competitor and the market. history keys: competitor id, None = market."""
    names = {c["id"]: c["name"] for c in competitors}
    comps = []
    all_events = []
    for cid, row in per_comp.items():
        r = score_row(row["metrics"], history.get(cid, []), row["events"])
        comps.append({"competitor_id": cid, "competitor": names.get(cid), "metrics": row["metrics"],
                      "events": row["events"], **r})
        all_events += row["events"]
    mkt = market_metrics(per_comp)
    market = {"metrics": mkt, "events": all_events,
              **score_row(mkt, history.get(None, []), all_events)}

    # The driver is the competitor furthest above their own normal, events included.
    # While calibrating, the one with the most event points; with neither, nobody.
    def drive(c: dict[str, Any]) -> float:
        if c["score"] is not None:
            return c["score"]
        return 50 + c["event_points"] if c["event_points"] else -1
    ranked = sorted(comps, key=drive, reverse=True)
    top = ranked[0] if ranked and drive(ranked[0]) > 50 else None
    driver = None
    if top:
        why = [e["kind"] for e in top["events"]]
        driver = {"competitor": top["competitor"], "score": top["score"],
                  "reason": ("events: " + ", ".join(sorted(set(why)))) if why
                            else "furthest above their own normal"}
    return {"market": market, "competitors": sorted(comps, key=lambda c: c["competitor"] or ""),
            "driver": driver}
