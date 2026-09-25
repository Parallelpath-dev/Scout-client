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
    python validate_briefing.py --client X --week 2026-09-21

synthesizer.py calls validate() directly before it writes. The CLI re-checks the latest
stored briefing, for a week edited by hand or a rule changed after the fact. Both read
the week's signals through week_window.py, so they agree on what "this week" means.

Standard library only, except for the Supabase path, which uses supa.py.
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
# Topics the client context says are raised on a call and never put in writing. A match
# is a failure, not a warning: a held week costs a day, a sentence in a client's inbox
# cannot be taken back. Bouldering Project: the Columbia Heights / Eckington overlap.
NEVER_IN_WRITING = [
    re.compile(r"\bcannibali[sz]", re.I),
    re.compile(r"\b(?:self|intra)[- ]?(?:brand )?competi", re.I),
]
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


def check_text(rep, where, text, cap, never=()):
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
    for rx in never:
        if rx.search(text):
            rep.fail(where, f"topic kept out of writing: {rx.pattern!r}")
    for j in JARGON:
        if re.search(rf"\b{re.escape(j)}\b", low):
            rep.warn(where, f"jargon: {j!r}")
    for a in ADVERBS:
        if re.search(rf"\b{a}\b", low):
            rep.warn(where, f"adverb: {a!r}")


def terms_to_patterns(terms):
    """Client config never_in_writing terms (plain strings) to patterns."""
    return [re.compile(rf"\b{re.escape(t)}\b", re.I) for t in (terms or []) if t]


def validate(briefing, week_signal_ids, profile="executive", never_in_writing=()):
    """Validate one briefing. week_signal_ids = every signal id collected that week.

    briefing may carry "sections" (full_report.sections): every string in them is
    client-readable through the API whether or not a tab renders it yet, so it gets the
    same writing rules and the same evidence rule as a development.
    """
    rep = Report()
    nv = tuple(never_in_writing)
    _check_sections(rep, briefing.get("sections") if isinstance(briefing, dict) else None,
                    week_signal_ids, nv, profile)

    if not isinstance(briefing, dict):
        rep.fail("briefing", "not a JSON object — synthesis or parsing failed")
        return rep

    if profile != "executive":
        # Operator briefings still get the writing rules and the evidence rules,
        # but no caps and no development ceiling.
        big = 10 ** 6
        if briefing.get(F_SUMMARY):
            check_text(rep, "summary", briefing.get(F_SUMMARY), big, nv)
        for i, dev in enumerate(briefing.get(F_DEVELOPMENTS) or []):
            if isinstance(dev, dict):
                for k, v in dev.items():
                    if k not in (F_SIGNALS, F_CONFIDENCE, F_SOURCE) and isinstance(v, str) and v.strip():
                        check_text(rep, f"dev[{i}].{k}", v, big, nv)
            _check_evidence(rep, f"dev[{i}]", dev, week_signal_ids, suppress_low=False)
        return rep

    # ── summary ──
    summary = briefing.get(F_SUMMARY)
    check_text(rep, "summary", summary, CAPS["summary"], nv)
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
        check_text(rep, f"{where}.headline", dev.get(F_HEADLINE), CAPS["headline"], nv)
        check_text(rep, f"{where}.so_what", dev.get(F_SO_WHAT), CAPS["so_what"], nv)
        if dev.get(F_RECOMMENDATION) is not None:
            check_text(rep, f"{where}.recommendation",
                       dev.get(F_RECOMMENDATION), CAPS["recommendation"], nv)
        for k, v in dev.items():
            if k in (F_HEADLINE, F_SO_WHAT, F_RECOMMENDATION, F_SIGNALS,
                     F_CONFIDENCE, F_SOURCE):
                continue
            if isinstance(v, str) and v.strip():
                check_text(rep, f"{where}.{k}", v, CAPS["_default"], nv)
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
    if suppress_low and len(set(ids)) < 2:
        # The executive rule is unconditional: one signal is omitted, however confident
        # the model says it is. A model's "high" is not evidence.
        rep.fail(where, "rests on a single signal — omit it")
    if not dev.get(F_SOURCE):
        rep.warn(where, "no source_url — every item should be checkable in one click")


SECTION_SKIP = {F_SIGNALS, "applies_locally", "format", "keyword", "competitor",
                "channels", "confidence", "first_seen", "last_seen"}


def _check_sections(rep, sections, week_signal_ids, nv, profile):
    if not isinstance(sections, dict):
        return
    cap = CAPS["_default"] if profile == "executive" else 10 ** 6
    for tab, keys in sections.items():
        if not isinstance(keys, dict):
            continue
        for key, items in keys.items():
            for j, item in enumerate(items or []):
                where = f"sections.{tab}.{key}[{j}]"
                if not isinstance(item, dict):
                    rep.fail(where, "not an object")
                    continue
                for k, v in item.items():
                    if k not in SECTION_SKIP and isinstance(v, str) and v.strip():
                        check_text(rep, f"{where}.{k}", v, cap, nv)
                    elif k not in SECTION_SKIP and isinstance(v, list):
                        # Evidence lines are client-readable sentences like any other.
                        for n, line in enumerate(v):
                            if isinstance(line, str) and line.strip():
                                check_text(rep, f"{where}.{k}[{n}]", line, cap, nv)
                    elif k in ("competitor", "keyword") and isinstance(v, str):
                        for rx in nv:
                            if rx.search(v):
                                rep.fail(f"{where}.{k}", f"topic kept out of writing: {rx.pattern!r}")
                ids = item.get(F_SIGNALS)
                if not isinstance(ids, list) or not ids:
                    rep.fail(where, "cites no signals — every claim must trace to collected data")
                elif week_signal_ids is not None:
                    unknown = [x for x in ids if x not in week_signal_ids]
                    if unknown:
                        rep.fail(where, f"cites {len(unknown)} signal id(s) not collected this "
                                        f"week — possible fabrication: {unknown[:3]}")


# ── Supabase path ────────────────────────────────────────────────────────────

def run_for_client(slug, week=None, dry_run=False):
    """Re-validate the stored briefing for a week (default: the latest) and hold it on
    failure. portal schema, through supa.py, like every other script in this repo."""
    from supa import Supa
    from week_window import fetch_week_signals, week_of

    sb = Supa(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    c = sb.get("public", "clients", {"slug": f"eq.{slug}", "select": "id,name,output_profile,config"})
    if not c:
        print(f"[validate] no client with slug {slug}")
        return 2
    client = c[0]
    profile = client.get("output_profile") or "operator"

    params = {"client_id": f"eq.{client['id']}",
              "select": "id,week_of,summary,developments,full_report,published_at",
              "order": "week_of.desc", "limit": "1"}
    if week:
        params["week_of"] = f"eq.{week}"
    rows = sb.get("portal", "briefings", params)
    if not rows:
        print(f"[validate] no briefing for {slug}")
        return 0
    row = rows[0]

    # The columns are the source of truth. full_report is jsonb in this schema and
    # holds the sections, not a second copy of what the client reads.
    briefing = {"summary": row.get("summary"), "developments": row.get("developments") or [],
                "sections": (row.get("full_report") or {}).get("sections")}

    from datetime import date as _date
    wk = week_of(_date.fromisoformat(str(row["week_of"])[:10]))
    week_ids = {s["id"] for s in fetch_week_signals(sb, client["id"], wk, select="id,signal_type,week_of,collected_at")}

    nv = NEVER_IN_WRITING + terms_to_patterns((client.get("config") or {}).get("never_in_writing"))
    rep = validate(briefing, week_ids, profile, never_in_writing=nv)
    print(f"[validate] {client['name']} · week of {row['week_of']} · profile={profile}")
    print(rep.render())

    if rep.ok:
        print("[validate] PASS")
        return 0
    if dry_run:
        print("[validate] FAIL (dry run, nothing changed)")
        return 1

    sb.patch("portal", "briefings", {"id": f"eq.{row['id']}"}, {"published_at": None})
    print(f"[validate] FAIL. Briefing {row['id']} HELD. The client cannot see it.")
    print("[validate] Fix and re-run synthesis, or publish by hand after review:")
    print(f"           update portal.briefings set published_at = now() where id = '{row['id']}';")
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

    print("\nNEVER-IN-WRITING — expect a failure:")
    leak = json.loads(json.dumps(good))
    leak["developments"][0]["so_what"] = "Members may cannibalise the Eckington gym."
    n = validate(leak, ids, never_in_writing=NEVER_IN_WRITING)
    print(n.render())

    ok = g.ok and not b.ok and not n.ok
    print(f"\nself-test {'PASSED' if ok else 'FAILED'} "
          f"(good clean: {g.ok}, bad caught: {not b.ok}, "
          f"{len(b.failures)} failures found)")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--client")
    ap.add_argument("--week", help="week_of (a Monday) to re-check; default latest")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.client:
        ap.error("--client or --selftest required")
    sys.exit(run_for_client(a.client, a.week, a.dry_run))
