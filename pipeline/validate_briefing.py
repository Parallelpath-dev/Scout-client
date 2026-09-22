#!/usr/bin/env python3
"""
Scout — briefing validation gate.

Runs after synthesis and before a client can see a briefing. Auto-publish stays
auto-publish: this adds no human step. It adds a machine that refuses to ship a week
that is broken, unevidenced, or off-standard.

On failure the week is HELD (published_at = NULL) and an alert is raised. A held week
is visible to Parallel Path and invisible to the client, which is the correct failure
mode: no briefing beats a wrong one.

    python validate_briefing.py --selftest                  # no DB needed
    python validate_briefing.py --client bouldering-project # validate latest, hold on fail
    python validate_briefing.py --client X --dry-run        # report only, change nothing

Standard library only, except for the optional Supabase path.
"""

import argparse
import json
import os
import re
import sys

# ── Field names ──────────────────────────────────────────────────────────────
# Mirror of the Analyst/Strategist output schema. If synthesizer.py renames a
# field, this must change with it — a rename shows up here as "missing field",
# which is noisy but safe. Silent passes are the thing to avoid.
F_SUMMARY = "summary"
F_DEVELOPMENTS = "developments"
F_HEADLINE = "headline"
F_SO_WHAT = "so_what"
F_RECOMMENDATION = "recommendation"
F_CONFIDENCE = "confidence"
F_SIGNALS = "signal_ids"
F_SOURCE = "source_url"

CAPS = {"summary": 75, "headline": 10, "so_what": 25, "recommendation": 30, "_default": 40}
DEV_MIN, DEV_MAX = 2, 4

# ── Rejected constructions ───────────────────────────────────────────────────
BANNED_PHRASES = [
    "here's the thing", "here's what", "here's why", "here's how",
    "it turns out", "the truth is", "the reality is", "let me be clear",
    "make no mistake", "let that sink in", "full stop.", "at the end of the day",
    "it's worth noting", "in today's", "in a world where", "when it comes to",
    "that said,", "needless to say",
]
JARGON = [  # warnings, not failures
    "double down", "deep dive", "game-changer", "game changer", "lean into",
    "circle back", "moving forward", "on the same page", "take a step back",
    "unpack", "landscape", "navigate",
]
ADVERBS = [  # warnings
    "really", "just", "literally", "genuinely", "honestly", "simply", "actually",
    "deeply", "truly", "fundamentally", "inherently", "inevitably", "interestingly",
    "importantly", "crucially", "significantly", "notably", "considerably",
]
GENERIC_OPENERS = [
    "this week saw", "this week continued", "this week brought", "overall,",
    "in summary", "as always", "competitive activity", "the competitive landscape",
    "there were several", "a number of",
]
ANTITHESIS = re.compile(
    r"\bnot\s+(?:just\s+)?[^.;,]{2,45}[,.]?\s+(?:it'?s|but rather|but)\b", re.I
)
EM_DASH = re.compile(r"[—–]")
# Heuristics for a response that was cut off rather than finished.
TRUNCATED = re.compile(r"(?:\w-|\b(?:and|the|to|of|a|in|with|for|that))\s*$", re.I)


class Report:
    def __init__(self):
        self.failures, self.warnings = [], []

    def fail(self, where, msg):
        self.failures.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")

    @property
    def ok(self):
        return not self.failures

    def render(self):
        out = []
        for f in self.failures:
            out.append(f"  FAIL  {f}")
        for w in self.warnings:
            out.append(f"  warn  {w}")
        if not out:
            out.append("  clean")
        return "\n".join(out)


def words(text):
    return len([w for w in re.split(r"\s+", (text or "").strip()) if w])


def check_text(rep, where, text, cap):
    """Every rule that applies to any client-facing string."""
    if not text or not str(text).strip():
        rep.fail(where, "empty")
        return
    text = str(text)
    n = words(text)
    if n > cap:
        rep.fail(where, f"{n} words, cap is {cap}")
    if EM_DASH.search(text):
        rep.fail(where, "contains an em dash")
    low = text.lower()
    for p in BANNED_PHRASES:
        if p in low:
            rep.fail(where, f"banned phrase: {p!r}")
    if ANTITHESIS.search(text):
        rep.fail(where, "two-beat antithesis (\"not X, it's Y\")")
    if TRUNCATED.search(text.rstrip()):
        rep.fail(where, "looks truncated mid-sentence")
    for j in JARGON:
        if re.search(rf"\b{re.escape(j)}\b", low):
            rep.warn(where, f"jargon: {j!r}")
    for a in ADVERBS:
        if re.search(rf"\b{a}\b", low):
            rep.warn(where, f"adverb: {a!r}")


def validate(briefing, week_signal_ids, profile="executive"):
    """Validate one briefing. week_signal_ids = every signal id collected that week."""
    rep = Report()

    if not isinstance(briefing, dict):
        rep.fail("briefing", "not a JSON object — synthesis or parsing failed")
        return rep

    if profile != "executive":
        # Operator briefings still get the writing rules and the evidence rules,
        # but no caps and no development ceiling.
        for i, dev in enumerate(briefing.get(F_DEVELOPMENTS) or []):
            _check_evidence(rep, f"dev[{i}]", dev, week_signal_ids, suppress_low=False)
        return rep

    # ── summary ──
    summary = briefing.get(F_SUMMARY)
    check_text(rep, "summary", summary, CAPS["summary"])
    if summary:
        low = str(summary).lstrip().lower()
        for opener in GENERIC_OPENERS:
            if low.startswith(opener):
                rep.fail("summary", f"generic opener: {opener!r}")
                break

    # ── developments ──
    devs = briefing.get(F_DEVELOPMENTS)
    if not isinstance(devs, list):
        rep.fail("developments", "missing or not a list")
        return rep

    if len(devs) > DEV_MAX:
        rep.fail("developments", f"{len(devs)} present, cap is {DEV_MAX} — rank and cut, "
                                 f"do not shorten all of them")
    if len(devs) < DEV_MIN:
        # Not a hard failure: a genuinely quiet week is a legitimate result. But the
        # summary has to own it rather than the briefing arriving mysteriously thin.
        rep.warn("developments", f"only {len(devs)} — confirm the summary says so plainly")

    for i, dev in enumerate(devs):
        where = f"dev[{i}]"
        if not isinstance(dev, dict):
            rep.fail(where, "not an object")
            continue
        check_text(rep, f"{where}.headline", dev.get(F_HEADLINE), CAPS["headline"])
        check_text(rep, f"{where}.so_what", dev.get(F_SO_WHAT), CAPS["so_what"])
        if dev.get(F_RECOMMENDATION) is not None:
            check_text(rep, f"{where}.recommendation",
                       dev.get(F_RECOMMENDATION), CAPS["recommendation"])
        for k, v in dev.items():
            if k in (F_HEADLINE, F_SO_WHAT, F_RECOMMENDATION, F_SIGNALS,
                     F_CONFIDENCE, F_SOURCE):
                continue
            if isinstance(v, str) and v.strip():
                check_text(rep, f"{where}.{k}", v, CAPS["_default"])
        _check_evidence(rep, where, dev, week_signal_ids, suppress_low=True)

    return rep


def _check_evidence(rep, where, dev, week_signal_ids, suppress_low):
    """No claim without a signal. This is the rule the whole gate exists for."""
    if not isinstance(dev, dict):
        return
    ids = dev.get(F_SIGNALS) or []
    if not isinstance(ids, list) or not ids:
        rep.fail(where, "cites no signals — every claim must trace to collected data")
        return
    if week_signal_ids is not None:
        unknown = [i for i in ids if i not in week_signal_ids]
        if unknown:
            rep.fail(where, f"cites {len(unknown)} signal id(s) not collected this week "
                            f"— possible fabrication: {unknown[:3]}")
    conf = str(dev.get(F_CONFIDENCE) or "").lower()
    if suppress_low and conf == "low":
        rep.fail(where, "low confidence in an executive briefing — suppress it rather "
                        "than compressing it into a confident headline")
    if suppress_low and len(ids) < 2 and conf != "high":
        rep.fail(where, "single-signal development below high confidence — omit it")
    if not dev.get(F_SOURCE):
        rep.warn(where, "no source_url — every item should be checkable in one click")


# ── Supabase path ────────────────────────────────────────────────────────────

def _client():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def run_for_client(slug, dry_run=False):
    sb = _client()
    c = sb.table("clients").select("id,name,output_profile").eq("slug", slug).single().execute()
    client_id = c.data["id"]
    profile = c.data.get("output_profile") or "operator"

    b = (sb.table("briefings").select("id,week_of,summary,developments,full_report")
         .eq("client_id", client_id).order("week_of", desc=True).limit(1).execute())
    if not b.data:
        print(f"[validate] no briefing for {slug}")
        return 0
    row = b.data[0]

    briefing = {"summary": row.get("summary"), "developments": row.get("developments") or []}
    # full_report is text in this schema; if it parses as JSON it wins, since it is
    # the complete object the model returned.
    try:
        parsed = json.loads(row.get("full_report") or "")
        if isinstance(parsed, dict):
            briefing = parsed
    except (ValueError, TypeError):
        pass

    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=8)).isoformat()
    sig = (sb.table("signals").select("id")
           .eq("client_id", client_id).gte("collected_at", cutoff).execute())
    week_ids = {s["id"] for s in (sig.data or [])}

    rep = validate(briefing, week_ids, profile)
    print(f"[validate] {c.data['name']} · week of {row['week_of']} · profile={profile}")
    print(rep.render())

    if rep.ok:
        print("[validate] PASS")
        return 0

    if dry_run:
        print("[validate] FAIL (dry run — nothing changed)")
        return 1

    sb.table("briefings").update({"published_at": None}).eq("id", row["id"]).execute()
    print(f"[validate] FAIL — briefing {row['id']} HELD. Client cannot see it.")
    print("[validate] Fix and re-run synthesis, or publish manually after review:")
    print(f"           update briefings set published_at = now() where id = '{row['id']}';")
    return 1


# ── Self-test ────────────────────────────────────────────────────────────────

def selftest():
    ids = {"s1", "s2", "s3"}

    good = {
        "summary": "Movement opened a second Crystal City location and Sportrock cut "
                   "its joining fee. Both moves target the same members you are "
                   "recruiting for Columbia Heights.",
        "developments": [
            {"headline": "Movement opened a second Crystal City location",
             "so_what": "Adds capacity 14 minutes from your DC catchment.",
             "recommendation": "Pull forward the Columbia Heights pre-opening campaign "
                               "by two weeks.",
             "confidence": "high", "signal_ids": ["s1", "s2"],
             "source_url": "https://example.com/1"},
            {"headline": "Sportrock cut its joining fee to zero",
             "so_what": "Removes the switching cost for price-sensitive members.",
             "recommendation": "Test a matching offer in Alexandria only.",
             "confidence": "high", "signal_ids": ["s2", "s3"],
             "source_url": "https://example.com/2"},
        ],
    }

    bad = {
        "summary": "This week saw continued competitive activity across the DC "
                   "landscape — it's not just about price, it's about positioning.",
        "developments": [
            {"headline": "Competitive activity increased across the DC market this "
                         "week in several notable ways",
             "so_what": "It turns out the market is genuinely shifting and this really "
                        "matters for how you think about your overall positioning going "
                        "forward into next quarter and beyond.",
             "recommendation": "Consider a strategic review.",
             "confidence": "low", "signal_ids": [], "source_url": None},
            {"headline": "A competitor did something",
             "so_what": "Unclear.", "recommendation": "Watch it.",
             "confidence": "medium", "signal_ids": ["s9"],
             "source_url": "https://example.com/x"},
            {"headline": "Third thing", "so_what": "More.", "recommendation": "And",
             "confidence": "high", "signal_ids": ["s1", "s2"], "source_url": "u"},
            {"headline": "Fourth", "so_what": "More.", "recommendation": "Act.",
             "confidence": "high", "signal_ids": ["s1", "s2"], "source_url": "u"},
            {"headline": "Fifth", "so_what": "More.", "recommendation": "Act.",
             "confidence": "high", "signal_ids": ["s1", "s2"], "source_url": "u"},
        ],
    }

    print("GOOD briefing — expect clean:")
    g = validate(good, ids)
    print(g.render())

    print("\nBAD briefing — expect failures:")
    b = validate(bad, ids)
    print(b.render())

    ok = g.ok and not b.ok
    print(f"\nself-test {'PASSED' if ok else 'FAILED'} "
          f"(good clean: {g.ok}, bad caught: {not b.ok}, "
          f"{len(b.failures)} failures found)")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--client")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.client:
        ap.error("--client or --selftest required")
    sys.exit(run_for_client(a.client, a.dry_run))
