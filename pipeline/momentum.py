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
       ads_launched   distinct messages among acquisition ads started this week. Meta
                      campaigns duplicate one message into many ad IDs: VIDA relaunched
                      5 messages as 23 IDs on 23 Sep 2026. Counted by ID, a refresh reads
                      as a quadrupling. ads_active stays counted by ID, since duplication
                      habits are part of each competitor's own normal.
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

import math
import re

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


_WS = re.compile(r"\s+")


def _message_key(d: dict[str, Any]) -> str | None:
    """What an ad says, normalised, so duplicates of one message count once. Template
    tokens are not copy; a DCO ad whose top level is a token falls back to its cards."""
    cands = [d.get("body"), d.get("title")] + list((d.get("text_scanned") or {}).values())
    for c in cands:
        if isinstance(c, str) and c.strip() and "{{" not in c:
            return _WS.sub(" ", c).strip().lower()[:160]
    return None


def geo_weight(signal: dict[str, Any], single_market: bool) -> float:
    # A location page is local by definition, whatever the copy says. No competitor
    # has one yet (11 Sep); Bouldering Project's DC page does.
    if single_market or signal.get("source_scope") == "local":
        return 1.0
    return GEO_WEIGHT.get(signal.get("geo_relevance") or "none", 0.25)


def week_metrics(
    signals: list[dict[str, Any]], competitors: list[dict[str, Any]], wk: date,
    *, ran: set[str], email_channels: set[str], watch_terms: list[str] | None = None,
    social_channels: dict[str, set[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """{competitor_id: {"metrics": {...}, "events": [...]}} for one week.

    ran: which collectors ran this week, from "ads", "web", "email", "search". Passed
    in rather than inferred, because a collector that ran and found nothing leaves no
    signal behind: the web collector's proof of life is its page snapshots, which are
    not evidence and are not in the window.

    social_channels: {competitor_id: {organic channel ids}}. social_posts is only
    recorded when every one of a competitor's channels was collected; with one
    platform's actor down, a partial count would read as a real drop.

    Only competitors appear; the client's own search rows are not pressure.
    """
    comp = {c["id"]: c for c in competitors}
    out = {cid: {"metrics": {}, "events": []} for cid in comp}
    terms = [t.lower() for t in (watch_terms or []) if t]
    seen_types: dict[str, set[str]] = {cid: set() for cid in comp}
    launched: dict[str, dict[str, float]] = {cid: {} for cid in comp}   # message -> weight
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
        # Content about another market is not pressure here, and neither is an event
        # in it: "now open in Denver" must not make Movement this week's driver.
        elsewhere = s.get("geo_relevance") == "other_market" and not sm

        if t == "ad_active":
            if d.get("is_recruitment"):
                continue
            w = geo_weight(s, sm)
            add(cid, "ads_active", w)
            started = _date(d.get("start_date"))
            if started and started >= launched_from:
                msg = _message_key(d) or f"id:{s['id']}"
                launched[cid][msg] = max(w, launched[cid].get(msg, 0.0))

        elif t == "web_change":
            if not d.get("surfaces"):
                continue
            pts = MATERIALITY_POINTS.get(d.get("materiality"), 0.0)
            add(cid, "web", 0.0 if elsewhere else
                pts * APPLIES_WEIGHT.get(d.get("applies_locally") or "yes", 1.0))
            types = set() if elsewhere else set(d.get("change_types") or [])
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
            add(cid, "email", 0.0 if elsewhere else
                MATERIALITY_POINTS.get(d.get("materiality"), 1.0) * w)
            if not elsewhere and kind == "offer":
                event(cid, "offer", s, d.get("subject") or "offer email")
            if not elsewhere and kind == "opening":
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

    for cid, msgs in launched.items():
        if msgs:
            add(cid, "ads_launched", sum(msgs.values()))

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
        # A social profile row is the collector's proof it reached a channel. All of a
        # competitor's channels, or the metric is unknown this week.
        if "social" in ran and "social_profile" in seen_types[cid]:
            m.setdefault("social_posts", 0.0)
        if social_channels is not None and "social_posts" in m:
            got = {s.get("channel_id") for s in signals
                   if s.get("competitor_id") == cid and s["signal_type"] == "social_profile"}
            if not social_channels.get(cid, set()) <= got:
                m.pop("social_posts")
    return out


def market_metrics(per_comp: dict[str, dict[str, Any]]) -> dict[str, float]:
    tot: dict[str, float] = {}
    for row in per_comp.values():
        for k, v in row["metrics"].items():
            tot[k] = round(tot.get(k, 0.0) + v, 3)
    return tot


COMPONENTS = {
    "paid": ("ads_active", "ads_launched"),
    "search": ("search",),
    "web": ("web",),
    "email": ("email",),
    "social": ("social_posts",),
}
LOG_FLOOR = 0.25          # about a 28% difference, on the log scale
FULL_OWN = 4              # weeks of own history at which the set drops out entirely


# Compared against the set only on what they did this week, never on how big they are.
# A count of running ads is mostly the size of the advertiser (Onelife: 266 against a
# set median near 22), so it joins through the competitor's own history from week 2.
NO_SET = {"ads_active"}


def _set_z(k: str, v: float, peers: list[float]) -> float | None:
    """How far this competitor sits from the rest of the set this week.

    On a log scale: counts across a set are skewed, and on the raw scale one launch
    against a set median of two reads like a surge. On the log scale 11 launches
    against a median of two reads about two usual swings up, not off the chart.
    """
    if len(peers) < 2 or k in NO_SET:
        return None
    return robust_z(math.log1p(v), [math.log1p(x) for x in peers],
                    abs_floor=LOG_FLOOR, min_history=2)


def _blend(own: float | None, ref: float | None, n_own: int) -> tuple[float | None, str]:
    w = min(n_own, FULL_OWN) / FULL_OWN if own is not None else 0.0
    if ref is None:
        return (own, "own") if own is not None and n_own >= FULL_OWN else (
            (own * w if own is not None and w else None), "own")
    if w >= 1.0:
        return own, "own"
    if w == 0.0:
        return ref, "set"
    return w * own + (1 - w) * ref, "blend"


def score_row(
    metrics: dict[str, float],
    history: list[dict[str, float]],
    events: list[dict[str, Any]],
    reference: dict[str, float | None] | None = None,
) -> dict[str, Any]:
    """Score one row (a competitor, or the market).

    Every metric has a z from its first week. Against its own history once that exists;
    before that against `reference`, the z it has relative to the set this week. The two
    are blended by how many weeks of own history there are, so there is no cliff:
    week 1 is all set, week 5 is all own.

    history: that row's metrics for previous weeks, oldest first, this week excluded.
    """
    reference = reference or {}
    zs: dict[str, float | None] = {}
    basis: dict[str, str] = {}
    for k, v in metrics.items():
        h = [x.get(k) for x in history]
        n_own = sum(1 for x in h if x is not None)
        own = robust_z(v, h, min_history=1, **FLOORS.get(k, {})) if n_own else None
        zs[k], basis[k] = _blend(own, reference.get(k), n_own)
    live = {k: METRIC_WEIGHT.get(k, 10) for k, z in zs.items() if z is not None}
    bonus = min(EVENT_CAP, sum(e["points"] for e in events))

    components = {}
    for name, keys in COMPONENTS.items():
        ks = [k for k in keys if zs.get(k) is not None]
        if ks:
            cz = sum(zs[k] * METRIC_WEIGHT.get(k, 10) for k in ks) / sum(
                METRIC_WEIGHT.get(k, 10) for k in ks)
            b = {basis[k] for k in ks}
            components[name] = {"score": to_score(cz), "basis": b.pop() if len(b) == 1 else "blend"}
        elif any(k in metrics for k in keys):
            components[name] = {"score": None, "basis": "no comparison"}
        else:
            components[name] = {"score": None, "basis": "not collected"}

    if not live:
        return {"status": "calibrating", "score": None, "z": None, "metric_z": zs,
                "basis": basis, "components": components,
                "event_points": bonus, "trend": None, "history_weeks": len(history)}
    z = sum(zs[k] * w for k, w in live.items()) / sum(live.values())
    # Trend on the combined level, so it survives the ramp being absorbed as normal.
    # Only over weeks that carry every metric in the score: a metric that started being
    # collected recently would otherwise read as the market heating up.
    def level(m: dict[str, float]) -> float:
        return sum(m[k] * METRIC_WEIGHT.get(k, 10) for k in live)
    complete = [h for h in history if all(h.get(k) is not None for k in live)]
    tr = trend([level(h) for h in complete] + [level(metrics)])
    return {"status": "scored", "score": to_score(z, bonus), "z": round(z, 3),
            "metric_z": {k: (round(v, 3) if v is not None else None) for k, v in zs.items()},
            "basis": basis, "components": components,
            "event_points": bonus, "trend": tr, "history_weeks": len(history)}


def score_week(
    per_comp: dict[str, dict[str, Any]],
    history: dict[str | None, list[dict[str, float]]],
    competitors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score every competitor and the market. history keys: competitor id, None = market."""
    names = {c["id"]: c["name"] for c in competitors}

    def ref_for(cid: str) -> dict[str, float | None]:
        out = {}
        for k, v in per_comp[cid]["metrics"].items():
            peers = [r["metrics"][k] for c, r in per_comp.items()
                     if c != cid and r["metrics"].get(k) is not None]
            out[k] = _set_z(k, v, peers)
        return out

    comps = []
    all_events = []
    for cid, row in per_comp.items():
        r = score_row(row["metrics"], history.get(cid, []), row["events"], ref_for(cid))
        comps.append({"competitor_id": cid, "competitor": names.get(cid), "metrics": row["metrics"],
                      "events": row["events"], **r})
        all_events += row["events"]
    mkt = market_metrics(per_comp)
    # The market has no peers. Before it has its own history, a metric's market z is
    # the average of the competitors' z on it: the set running hot or cold as a whole.
    mref: dict[str, float | None] = {}
    for k in mkt:
        vals = [c["metric_z"].get(k) for c in comps if c["metric_z"].get(k) is not None]
        mref[k] = sum(vals) / len(vals) if vals else None
    market = {"metrics": mkt, "events": all_events,
              **score_row(mkt, history.get(None, []), all_events, mref)}

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
        bases = set((top.get("basis") or {}).values())
        where = ("furthest above their own normal" if bases == {"own"} else
                 "furthest above the rest of the set this week" if bases == {"set"} else
                 "furthest above normal: their own history where it exists, "
                 "the rest of the set where it does not")
        driver = {"competitor": top["competitor"], "score": top["score"],
                  "reason": ("events: " + ", ".join(sorted(set(why)))) if why else where}
    return {"market": market, "competitors": sorted(comps, key=lambda c: c["competitor"] or ""),
            "driver": driver}


def score_benchmark(row: dict[str, Any], history: list[dict[str, float]],
                    per_comp: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score the client itself, for reading beside its competitors, never among them.

    Against its own history once it has some, and before that against the competitors'
    values this week, exactly as a competitor would be. It takes no event points (its
    own posts name its own opening, which is not pressure) and it never enters the set
    reference, the market row or the driver.
    """
    ref: dict[str, float | None] = {}
    for k, v in row["metrics"].items():
        peers = [r["metrics"][k] for r in per_comp.values() if r["metrics"].get(k) is not None]
        ref[k] = _set_z(k, v, peers)
    return score_row(row["metrics"], history or [], [], ref)

