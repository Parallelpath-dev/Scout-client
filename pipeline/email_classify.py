"""
Scout — inbound competitor email classification.

WHAT THIS IS FOR
----------------
We hold a mailbox alias per competitor. What arrives is a mixed stream: real promotions,
class schedules, newsletters that say nothing, and our own double opt-in confirmations
bouncing back. Kyle needs the first category and would stop reading if handed the rest.

Two axes again, the same two the ad and web-change classifiers use:

  WHAT IT IS        offer, price, opening, event, newsletter, transactional, confirmation.
  WHERE IT APPLIES  the five geo tiers, via geo.py — the same vocabulary, not a copy.

The vocabularies for offers, prices, locations and schedules are IMPORTED from
web_change, not restated. A competitor's "$0 enrollment through Sunday" reads identically
on a pricing page and in a promo email, and the day those two definitions drift is the
day the web tab and the email tab disagree about the same promotion.

THE FOOTER PROBLEM
------------------
Every marketing email ends with a physical mailing address, because CAN-SPAM requires
one. VIDA's says "1612 U St NW, Washington, DC 20009". Classified naively, every single
VIDA email — including one about a national app launch — scores dc_explicit on its
footer, and the DC number becomes a count of emails received rather than a count of
emails about DC.

So the body is split before anything is classified, and geography is read from the
content only. Footer matches are kept as evidence and explicitly not allowed to set a
tier. This is the email version of the standing-banner problem in web_change: a thing
that is present in every message carries no information.

Pure module: no network, no database, no HTML fetching. Everything testable from strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any

from geo import TIER_NONE, GeoClassifier
from web_change import (
    HOURS_TERMS,
    LOCATION_TERMS,
    MONEY,
    OFFER_TERMS,
    SCHEDULE_TERMS,
    prices_in,
)

# ── what an email is, most consequential first ──────────────────────────────
# An email that is several of these reports the first. "Our new class schedule, plus $0
# enrollment this week" is an offer.
OFFER = "offer"
PRICE = "price"
OPENING = "opening"
EVENT = "event"
NEWSLETTER = "newsletter"
TRANSACTIONAL = "transactional"
CONFIRMATION = "confirmation"

TYPE_ORDER = [OFFER, PRICE, OPENING, EVENT, NEWSLETTER, TRANSACTIONAL, CONFIRMATION]

MATERIAL = "material"
MINOR = "minor"
NOISE = "noise"

# Our own double opt-in confirmations, arriving back at the alias we signed up with.
# These are artifacts of our collection method, not competitor behaviour, and counting
# one as a competitor promotion would be embarrassing in a way that is hard to walk back.
CONFIRMATION_TERMS = re.compile(
    r"(confirm (?:your )?(?:subscription|email|signup|sign[- ]up)|"
    r"verify your email|"
    r"please confirm|"
    r"you(?:'| a)?re (?:almost |nearly )?(?:in|subscribed|on the list)|"
    r"click (?:the link )?(?:below )?to confirm|"
    r"thanks? for (?:subscribing|signing up)(?=.{0,80}confirm))",
    re.I,
)

# Receipts, password resets, booking notices. Real mail from the competitor's booking
# system rather than their marketing stream, and zero competitive content.
#
# Note what is NOT here: a bare "waitlist". "Join the waitlist for our Columbia Heights
# location" is the single most valuable email we could receive, and an earlier draft of
# this list silenced it.
TRANSACTIONAL_TERMS = re.compile(
    r"\b(your receipt|order (?:confirmation|number)|invoice|payment (?:received|failed|method)|"
    r"reset your password|password (?:reset|changed)|"
    r"your (?:booking|reservation|class|spot) is confirmed|"
    r"check[- ]?in reminder|membership (?:agreement|renewal notice))\b",
    re.I,
)

EVENT_TERMS = re.compile(
    r"\b(competition|comp night|league|clinic|workshop|tournament|"
    r"member (?:night|social|party)|open house|community (?:night|event)|"
    r"film (?:tour|festival)|fundraiser|youth (?:camp|program)|summer camp|"
    r"yoga (?:series|workshop)|guest (?:coach|instructor))\b",
    re.I,
)

# Where the legally-required footer starts. Whichever of these appears earliest and
# late enough in the message is treated as the boundary.
FOOTER_MARKERS = [
    re.compile(r"\bunsubscribe\b", re.I),
    re.compile(r"\bmanage (?:your )?(?:email )?preferences\b", re.I),
    re.compile(r"\bopt[- ]?out\b", re.I),
    re.compile(r"\byou(?:'| a)?re receiving this (?:email|message)\b", re.I),
    re.compile(r"\bview (?:this|it) in (?:your )?browser\b", re.I),
    re.compile(r"©\s*\d{4}", re.I),
    # A bare US mailing address line, which is what the requirement actually produces.
    re.compile(r"\d{1,6}\s+[\w.\- ]+,\s*(?:[A-Za-z .]+,\s*)?[A-Z]{2}\s+\d{5}", re.I),
]

# Only treat a marker as the footer boundary if it falls in the last part of the message.
# "Unsubscribe" in a preheader at character 40 is not the footer.
FOOTER_MIN_POSITION = 0.45

_TAG = re.compile(r"<[^>]+>")
_STYLE_BLOCK = re.compile(r"<(script|style|head)\b.*?</\1>", re.I | re.S)
_BR = re.compile(r"<\s*(br|/p|/div|/tr|/h[1-6])\s*/?>", re.I)


def html_to_text(html: str) -> str:
    """Crude but sufficient. Marketing HTML is tables and inline styles; we want the words.

    Block-level tags become newlines first so that two adjacent table cells do not run
    together into a word that was never in the email ("JoinToday", "$116/moUnlimited").
    That matters: a glued word can defeat a \\b anchor in any of the imported patterns.
    """
    if not html:
        return ""
    s = _STYLE_BLOCK.sub(" ", html)
    s = _BR.sub("\n", s)
    s = _TAG.sub(" ", s)
    s = unescape(s)
    s = re.sub(r"[ \t ]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def split_footer(text: str) -> tuple[str, str]:
    """Return (content, footer). Footer is '' when no boundary is found.

    Splitting on the EARLIEST qualifying marker, not the latest, because the address
    block usually sits below the unsubscribe link and we want both on the footer side.
    """
    if not text:
        return "", ""
    cut = None
    floor = int(len(text) * FOOTER_MIN_POSITION)
    for rx in FOOTER_MARKERS:
        for m in rx.finditer(text):
            if m.start() >= floor:
                cut = m.start() if cut is None else min(cut, m.start())
                break
    if cut is None:
        return text, ""
    return text[:cut].strip(), text[cut:].strip()


@dataclass
class EmailVerdict:
    email_types: list[str] = field(default_factory=list)
    materiality: str = NOISE
    geo_relevance: str = TIER_NONE
    geo_evidence: str | None = None
    geo_from_footer_only: bool = False
    evidence: list[str] = field(default_factory=list)
    prices: list[str] = field(default_factory=list)
    applies_locally: str = "yes"
    caveat: str | None = None

    @property
    def headline_type(self) -> str | None:
        for t in TYPE_ORDER:
            if t in self.email_types:
                return t
        return None


_geo = GeoClassifier()


def classify_email(
    *,
    subject: str | None,
    text_body: str | None = None,
    html_body: str | None = None,
    competitor_domain: str | None = None,
    channel_scope: str = "national",
    single_market: bool = False,
    prices_by_home_gym: bool = False,
) -> EmailVerdict:
    """Classify one inbound competitor email.

    channel_scope        what the mailing list covers, per portal.channels.scope.
    single_market        True for brands that only operate here. Their "national" list
                         is a DC list; saying a VIDA promo may not apply in DC is wrong.
    prices_by_home_gym   True for Movement. A price in a national email may not be the
                         DC price, and a number the client acts on that is not real in
                         their market is worse than no number.
    """
    v = EmailVerdict()

    subject = subject or ""
    body = text_body or html_to_text(html_body or "")
    content, footer = split_footer(body)

    # The subject line is content, never footer.
    scan = f"{subject}\n{content}"

    # ── confirmations first, and they short-circuit ──────────────────────────
    # Ours, not theirs. Nothing below should get a chance to call it an offer just
    # because the welcome copy mentions a free trial.
    if CONFIRMATION_TERMS.search(scan):
        v.email_types = [CONFIRMATION]
        v.materiality = NOISE
        v.evidence.append("our own opt-in confirmation")
        return v

    # Transactional short-circuits too. A booking confirmation comes from the gym's
    # reservation system, not its marketing stream, and marketing blasts do not say
    # "your spot is confirmed". Letting the rules below run over one only finds the
    # product words that happen to live in that copy.
    if TRANSACTIONAL_TERMS.search(scan):
        v.email_types = [TRANSACTIONAL]
        v.materiality = NOISE
        v.evidence.append("transactional, not marketing")
        return v

    for rx, label in (
        (OFFER_TERMS, OFFER),
        (LOCATION_TERMS, OPENING),
        (EVENT_TERMS, EVENT),
        (SCHEDULE_TERMS, EVENT),
        (HOURS_TERMS, EVENT),
    ):
        m = rx.search(scan)
        if m and label not in v.email_types:
            v.email_types.append(label)
            v.evidence.append(f'{label}: "{m.group(0).strip()}"')

    # A price is only news when it is attached to something. A membership page URL in a
    # footer or a "$5 guest pass" aside is not a repricing, but it is worth recording.
    v.prices = prices_in(scan)
    if v.prices and MONEY.search(scan):
        v.email_types.append(PRICE)
        v.evidence.append("prices: " + ", ".join("$" + p for p in v.prices[:5]))

    if not v.email_types:
        v.email_types.append(NEWSLETTER)
        v.evidence.append("no offer, price, opening or event language")

    # ── geography, from the content only ─────────────────────────────────────
    content_verdict = _geo.classify(
        text_fields={"subject": subject, "body": content},
        urls=(),
        competitor_domain=competitor_domain,
    )
    v.geo_relevance = content_verdict.relevance
    v.geo_evidence = content_verdict.evidence

    if v.geo_relevance == TIER_NONE and footer:
        # Record what the footer says without letting it set the tier. Every marketing
        # email carries the sender's address by law; a term present in all of them
        # distinguishes none of them.
        footer_verdict = _geo.classify(
            text_fields={"footer": footer}, urls=(), competitor_domain=competitor_domain
        )
        if footer_verdict.relevance != TIER_NONE:
            v.geo_from_footer_only = True
            v.geo_evidence = (
                f"footer only (mailing address), not counted: {footer_verdict.evidence}"
            )

    # ── materiality ──────────────────────────────────────────────────────────
    if {OFFER, PRICE, OPENING} & set(v.email_types):
        v.materiality = MATERIAL
    elif EVENT in v.email_types:
        v.materiality = MINOR
    elif NEWSLETTER in v.email_types:
        v.materiality = MINOR
    else:
        v.materiality = NOISE

    # ── does it apply here ───────────────────────────────────────────────────
    if single_market:
        v.applies_locally = "yes"
    elif channel_scope == "local":
        v.applies_locally = "yes"
    elif prices_by_home_gym and PRICE in v.email_types:
        v.applies_locally = "unknown"
        v.caveat = (
            "This brand prices by home gym. The figure is from a national list and may "
            "not be the DC price. Verify against the local location page before acting."
        )
    elif channel_scope == "national" and v.geo_relevance == TIER_NONE:
        v.applies_locally = "unknown"
        v.caveat = (
            "Sent from a national list with no reference to this market. It may or may "
            "not have reached DC subscribers."
        )

    return v


def should_surface(v: EmailVerdict) -> bool:
    """Whether an email earns a line in the briefing.

    Newsletters are stored and hidden. A run of them is how you notice a competitor
    starting to mail weekly, which the volume count picks up; individually they say
    nothing and would crowd out the three emails that matter.
    """
    return v.materiality == MATERIAL or (
        v.materiality == MINOR and v.email_types and v.headline_type == EVENT
    )


def surface_rank(v: EmailVerdict) -> tuple[int, int, int]:
    """Sort key for the briefing. Lower sorts first.

    Materiality outranks geography, matching web_change. A competitor repricing on a
    national list matters more than a local newsletter, even though only one is local.
    """
    mat = {MATERIAL: 0, MINOR: 1, NOISE: 2}.get(v.materiality, 2)
    geo = {"dc_landing": 0, "dc_explicit": 1, "regional": 2, "none": 3, "other_market": 4}
    typ = TYPE_ORDER.index(v.headline_type) if v.headline_type in TYPE_ORDER else 9
    return (mat, geo.get(v.geo_relevance, 3), typ)


def as_signal_data(v: EmailVerdict, *, subject: str | None, from_address: str | None) -> dict[str, Any]:
    """The jsonb payload for a portal.signals row."""
    return {
        "subject": subject,
        "from": from_address,
        "email_type": v.headline_type,
        "email_types": v.email_types,
        "materiality": v.materiality,
        "evidence": v.evidence,
        "prices": v.prices,
        "applies_locally": v.applies_locally,
        "caveat": v.caveat,
        "geo_from_footer_only": v.geo_from_footer_only,
    }
