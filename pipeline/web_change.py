"""
Scout — web change classification.

WHAT THIS IS FOR
----------------
Page monitoring fails in one predictable way: it reports everything. Cookie banners,
rotating testimonials, a swapped hero image, a timestamp in a footer. Bury three real
findings in forty diffs and the reader stops opening the tab, which is worse than not
collecting at all.

So every change gets scored on two independent axes:

  WHAT CHANGED   price, offer, location, hours, schedule, content, cosmetic.
  WHERE IT APPLIES  the same five geo tiers the ad classifier uses, via geo.py.

Neither alone is enough. A cosmetic diff on a DC page is still noise. A price change on
a national page is still news. Only the pair decides whether something surfaces.

THE CAVEAT THAT HAS TO TRAVEL WITH THE NUMBER
---------------------------------------------
Movement prices by home gym. A price change on their national memberships page may not
apply in DC at all. Any such change is emitted with `applies_locally: "unknown"` and a
caveat string, because a price the client acts on that is not real in their market is
worse than no price at all. The channel row carries the flag; this module reads it.

This module is pure: no network, no database. Everything here is testable from fixtures,
which matters because the judgment lives in the thresholds and those are what will need
tuning once real diffs arrive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

# ── change types, most consequential first ──────────────────────────────────
# Order matters: a diff that touches several of these reports the first one, because
# "they changed their price and also their hero image" is a price change.
PRICE = "price"
OFFER = "offer"
LOCATION = "location"
HOURS = "hours"
SCHEDULE = "schedule"
CONTENT = "content"
COSMETIC = "cosmetic"

TYPE_ORDER = [PRICE, OFFER, LOCATION, HOURS, SCHEDULE, CONTENT, COSMETIC]

MATERIAL = "material"
MINOR = "minor"
COSMETIC_ONLY = "cosmetic"

# A money figure: $124, $124.00, $1,249. Deliberately not matching a bare number —
# "15 days" and "2026" are not prices, and an unanchored number match makes every page
# look like it repriced every week.
MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)")

# Offer language. These are what a promo looks like in the wild across all five of
# these brands' pages, gathered from their live copy rather than imagined.
# NOTE the lookbehind instead of a leading \b. A word boundary cannot match between a
# space and a "$", both being non-word characters, so `\b\$0 enrollment` silently never
# fires — and "$0 enrollment fee" is exactly the promo these gyms actually run. The
# lookbehind still stops "carefree" matching "free", which is what the \b was for.
OFFER_TERMS = re.compile(
    r"(?<![A-Za-z])(free|waived|no (?:enrollment|joining|initiation) fee|\$0 enrollment|"
    r"first month|intro(?:ductory)?|trial|day pass|guest pass|promo|"
    r"limited time|ends? (?:soon|\w+day)|save \$?\d+|% off|percent off|"
    r"special offer|sign[- ]?up bonus|founding member|pre[- ]?sale)\b",
    re.I,
)

LOCATION_TERMS = re.compile(
    r"\b(now open|opening soon|coming soon|new location|now hiring at|"
    r"temporarily closed|permanently closed|relocat(?:ed|ing)|grand opening)\b",
    re.I,
)

HOURS_TERMS = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*[-–—to]+\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)|"
    r"open (?:daily|24|late)|closed (?:on )?(?:mon|tue|wed|thu|fri|sat|sun)|holiday hours)\b",
    re.I,
)

SCHEDULE_TERMS = re.compile(
    r"\b(class schedule|timetable|book a class|reserve (?:your )?spot|"
    r"new class|program schedule|session times|registration (?:open|closes))\b",
    re.I,
)

# Text that changes on its own between two loads of an unchanged page. Stripped before
# anything is compared, or every page looks like it changed every week.
VOLATILE = [
    re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)\b", re.I),          # clock times in footers
    re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+\w+\s+\d{1,2}\b", re.I),
    re.compile(r"\bcopyright\s*(?:©)?\s*\d{4}\b", re.I),
    re.compile(r"\b\d+\s+(?:people|members|others)\s+(?:viewing|booked|joined)\b", re.I),
    re.compile(r"[?&](?:utm_[a-z]+|fbclid|gclid|_ga)=[^\s\"'&]+", re.I),
    re.compile(r"\bcsrf[_-]?token[=:]\s*\S+", re.I),
]

# Fields worth diffing, and what a change in each one tends to mean. `nav` is included
# because a new nav item is how a new location or a new programme first appears.
COMPARED_FIELDS = ("title", "meta_description", "h1", "h2s", "hero", "ctas", "nav", "body")


def _flatten(v: Any) -> str:
    if isinstance(v, list):
        return " | ".join(str(x) for x in v)
    return "" if v is None else str(v)


def _denoise(text: str) -> str:
    """Strip the parts of a page that differ between two identical loads."""
    for rx in VOLATILE:
        text = rx.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def prices_in(text: str) -> list[str]:
    """Money figures found in a blob, normalised, in order of appearance.

    Returns strings rather than floats on purpose: "$87" and "$87.00" are the same
    price and should not read as a change, but "$1,249" must not become 1.249 through
    a careless float parse.
    """
    out: list[str] = []
    for m in MONEY.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        val = raw[:-3] if raw.endswith(".00") else raw
        out.append(val)
    return out


@dataclass
class ChangeVerdict:
    changed: bool = False
    change_types: list[str] = field(default_factory=list)
    materiality: str = COSMETIC_ONLY
    similarity: float = 1.0
    evidence: list[str] = field(default_factory=list)
    price_before: list[str] = field(default_factory=list)
    price_after: list[str] = field(default_factory=list)
    applies_locally: str = "yes"     # yes | unknown | no
    caveat: str | None = None

    @property
    def headline_type(self) -> str | None:
        for t in TYPE_ORDER:
            if t in self.change_types:
                return t
        return None


def classify_change(
    old: dict[str, Any] | None,
    new: dict[str, Any],
    *,
    channel_scope: str = "local",
    prices_by_home_gym: bool = False,
    single_market: bool = False,
) -> ChangeVerdict:
    """Compare two page snapshots and decide what, if anything, to report.

    channel_scope        'local' | 'regional' | 'national' — what the PAGE governs.
    prices_by_home_gym   True for Movement. Forces applies_locally='unknown' on any
                         price change found on a national page.
    single_market        True for brands that only operate here, where even a national
                         page is local by definition.
    """
    v = ChangeVerdict()

    if old is None:
        # First sight of a page is not a change. Recording it as one would open every
        # client's first week with a wall of false positives.
        v.changed = False
        v.evidence.append("first snapshot, nothing to compare")
        return v

    old_blob = _denoise(" ".join(_flatten(old.get(f)) for f in COMPARED_FIELDS))
    new_blob = _denoise(" ".join(_flatten(new.get(f)) for f in COMPARED_FIELDS))

    if old_blob == new_blob:
        return v

    v.changed = True
    v.similarity = round(SequenceMatcher(None, old_blob, new_blob).ratio(), 4)

    # ── price ───────────────────────────────────────────────────────────────
    before, after = prices_in(old_blob), prices_in(new_blob)
    if set(before) != set(after):
        v.change_types.append(PRICE)
        v.price_before, v.price_after = before, after
        gone = sorted(set(before) - set(after), key=lambda x: float(x))
        new_p = sorted(set(after) - set(before), key=lambda x: float(x))
        bits = []
        if gone:
            bits.append("removed " + ", ".join("$" + p for p in gone))
        if new_p:
            bits.append("added " + ", ".join("$" + p for p in new_p))
        v.evidence.append("price: " + "; ".join(bits))

    # ── the rest, on text that is new rather than merely present ────────────
    # Matching on the whole new page would re-report a standing "Free trial" banner
    # every week forever. Only language that appeared since last time counts.
    added = _added_text(old_blob, new_blob)
    for rx, label in (
        (OFFER_TERMS, OFFER),
        (LOCATION_TERMS, LOCATION),
        (HOURS_TERMS, HOURS),
        (SCHEDULE_TERMS, SCHEDULE),
    ):
        m = rx.search(added)
        if m:
            v.change_types.append(label)
            v.evidence.append(f'{label}: "{m.group(0).strip()}"')

    if not v.change_types:
        # Something moved but none of the meaningful patterns fired. Volume decides
        # whether that is a rewrite worth a glance or a swapped image.
        if v.similarity < 0.92:
            v.change_types.append(CONTENT)
            v.evidence.append(f"text changed, similarity {v.similarity}")
        else:
            v.change_types.append(COSMETIC)
            v.evidence.append(f"minor edit, similarity {v.similarity}")

    # ── materiality ─────────────────────────────────────────────────────────
    if {PRICE, OFFER, LOCATION} & set(v.change_types):
        v.materiality = MATERIAL
    elif {HOURS, SCHEDULE} & set(v.change_types):
        v.materiality = MINOR
    elif CONTENT in v.change_types:
        v.materiality = MINOR if v.similarity < 0.75 else COSMETIC_ONLY
    else:
        v.materiality = COSMETIC_ONLY

    # ── does it apply here ──────────────────────────────────────────────────
    if single_market:
        # Every page this brand owns is a DC page. They operate nowhere else.
        v.applies_locally = "yes"
    elif channel_scope == "local":
        v.applies_locally = "yes"
    elif prices_by_home_gym and PRICE in v.change_types:
        v.applies_locally = "unknown"
        v.caveat = (
            "This brand prices by home gym. The change is on a national page and may "
            "not apply in DC. Verify against the local location page before acting."
        )
    elif channel_scope == "national":
        v.applies_locally = "unknown"
        v.caveat = (
            "Change is on a national page. It may or may not have reached this market."
        )
    else:
        v.applies_locally = "yes"

    return v


def _added_text(old_blob: str, new_blob: str) -> str:
    """Only the text present in new and absent from old."""
    sm = SequenceMatcher(None, old_blob, new_blob)
    return " ".join(
        new_blob[j1:j2] for tag, _, _, j1, j2 in sm.get_opcodes() if tag in ("insert", "replace")
    )


def should_surface(v: ChangeVerdict) -> bool:
    """Whether a change earns a line in the briefing at all.

    Cosmetic changes are recorded and never shown. They are worth storing because a run
    of them is how you notice a site being rebuilt, and worth hiding because nobody has
    ever acted on a swapped hero image.
    """
    return v.changed and v.materiality in (MATERIAL, MINOR)


def surface_rank(v: ChangeVerdict, geo_relevance: str = "none") -> tuple[int, int, float]:
    """Sort key for the briefing. Lower sorts first.

    Materiality outranks geography, deliberately. A competitor repricing nationally
    matters more to Kyle than a DC page rewording its hero, even though only one of
    them is local.
    """
    mat = {MATERIAL: 0, MINOR: 1, COSMETIC_ONLY: 2}.get(v.materiality, 2)
    geo = {"dc_landing": 0, "dc_explicit": 1, "regional": 2, "none": 3, "other_market": 4}
    typ = TYPE_ORDER.index(v.headline_type) if v.headline_type in TYPE_ORDER else 9
    return (mat, geo.get(geo_relevance, 3), typ)
