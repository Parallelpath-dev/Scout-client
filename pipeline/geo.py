"""
Scout — geo classifier for competitor ad creative.

WHAT THIS DOES AND DOES NOT DO
------------------------------
The Meta Ad Library exposes no targeting, no audience location, no DMA and no delivery
region for commercial ads, anywhere. US commercial ads are not in the API at all. There
is therefore no observable answer to "is this ad aimed at DC".

What IS observable is whether an ad's creative REFERENCES this market: its landing URL,
its body copy, its headline, its CTA. That is what this module classifies.

A dc_referencing count is a FLOOR on local activity. It is never a count of ads targeted
at DC, and `none` is not evidence that an ad is not running here. Every label this module
emits is a statement about content, never about delivery.

TIERS, strongest first
----------------------
  dc_landing    the ad sends people to a location page for one of that brand's DC-market
                gyms. The advertiser has pointed a specific local asset at a person.
  dc_explicit   the creative names a DC-market place.
  regional      the creative names the commuter shed or an umbrella term (DMV, NoVA).
  other_market  the creative names a market that is not this one. Informative: it is why
                a low DC count can be correct rather than a collection failure.
  none          no geographic reference found. NOT a negative finding.

First tier to hit wins, and the matched string is carried out as evidence so a human can
audit any single classification without re-running anything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

TERMS_PATH = Path(__file__).with_name("geo_terms.json")

TIER_NONE = "none"
TIER_DC_LANDING = "dc_landing"
TIER_DC_EXPLICIT = "dc_explicit"
TIER_REGIONAL = "regional"
TIER_OTHER = "other_market"

# Matches the portal.geo_relevance domain. Anything not in here is a bug, not a new tier.
VALID_TIERS = {TIER_DC_LANDING, TIER_DC_EXPLICIT, TIER_REGIONAL, TIER_OTHER, TIER_NONE}


@dataclass
class GeoVerdict:
    """One classification, with the evidence that produced it."""

    relevance: str = TIER_NONE
    evidence: str | None = None
    # Every tier that matched, not only the winner. A weekly report never shows this, but
    # it is what you read when a classification looks wrong and you need to know whether
    # the rule fired on the wrong thing or never fired at all.
    all_matches: dict[str, list[str]] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {"geo_relevance": self.relevance, "geo_evidence": self.evidence}


def _load_terms(path: Path = TERMS_PATH) -> dict[str, Any]:
    with path.open() as fh:
        return json.load(fh)


def _strip_docs(obj: Any) -> Any:
    """Drop the _doc keys so config can be self-documenting without polluting matching."""
    if isinstance(obj, dict):
        return {k: _strip_docs(v) for k, v in obj.items() if k != "_doc"}
    return obj


class GeoClassifier:
    def __init__(self, terms: dict[str, Any] | None = None):
        raw = terms if terms is not None else _load_terms()
        self.t = _strip_docs(raw)

        self._dc_landing_urls: dict[str, list[str]] = self.t["dc_landing_url_patterns"]
        self._dc_landing_query: list[str] = self.t["dc_landing_query_hints"]["any"]
        self._dc_path_any: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in self.t["dc_landing_path_any"]["regex"]
        ]

        self._dc_terms: list[str] = self.t["dc_explicit"]["terms"]
        self._dc_ambiguous: dict[str, list[str]] = self.t["dc_explicit"]["ambiguous"]

        self._regional_terms: list[str] = self.t["regional"]["terms"]
        self._regional_ambiguous: dict[str, list[str]] = self.t["regional"]["ambiguous"]

        self._other_terms: list[str] = self.t["other_market"]["terms"]

        # Pre-compile. A weekly run classifies a few hundred ads against a few hundred
        # terms; compiling once per term instead of once per (term, ad) is the difference
        # between a second and a minute.
        self._rx: dict[str, re.Pattern[str]] = {}
        for term in (
            self._dc_terms
            + self._regional_terms
            + self._other_terms
            + list(self._dc_ambiguous)
            + list(self._regional_ambiguous)
        ):
            self._rx.setdefault(term, self._compile(term))

    @staticmethod
    def _compile(term: str) -> re.Pattern[str]:
        r"""Word-boundary match, tolerant of the punctuation ad copy actually contains.

        "u street" must match "U Street", "U-Street" and "U  Street" but not "menu streets".
        \b on each end handles the ends; the inner space becomes [\s\-.,]+ to handle the middle.
        """
        parts = [re.escape(p) for p in term.split()]
        body = r"[\s\-.,]+".join(parts)
        return re.compile(rf"\b{body}\b", re.IGNORECASE)

    # ── field-level matching ────────────────────────────────────────────────

    def _hit(self, rx_key: str, text: str) -> str | None:
        m = self._rx[rx_key].search(text)
        return m.group(0) if m else None

    def _has_qualifier(self, text: str, allowed: Iterable[str]) -> str | None:
        """An ambiguous term only counts when one of ITS OWN disambiguators is present.

        The generic qualifier list is deliberately NOT consulted here. It contains words
        like "class", "join" and "open", which appear in essentially every gym ad, so
        falling back to it qualified "VA" in "va va voom, our new class schedule" and
        turned ordinary prose into a regional hit. Each ambiguous term carries the
        disambiguators that are actually diagnostic for it, and nothing else counts.

        Strictness is the right bias: the count is reported to a client as a floor, so a
        missed hit understates a number already framed as an understatement, while a
        false hit corrupts one presented as evidence.
        """
        for q in allowed:
            if re.search(rf"\b{re.escape(q)}\b", text, re.IGNORECASE):
                return q
        return None

    def _scan(
        self,
        fields: dict[str, str],
        terms: list[str],
        ambiguous: dict[str, list[str]] | None = None,
    ) -> list[str]:
        """Return evidence strings for every term hit across the supplied fields."""
        found: list[str] = []
        ambiguous = ambiguous or {}

        for name, text in fields.items():
            if not text:
                continue
            for term in terms:
                hit = self._hit(term, text)
                if hit:
                    found.append(f'{name}: "{hit}"')
            for term, disambiguators in ambiguous.items():
                hit = self._hit(term, text)
                if not hit:
                    continue
                q = self._has_qualifier(text, disambiguators)
                if q:
                    found.append(f'{name}: "{hit}" (qualified by "{q}")')
                # No qualifier: deliberately silent. An unqualified "Columbia" is
                # more likely Columbia MD or Columbia University than Columbia Heights,
                # and a wrong dc_explicit is worse than a missed one, because the whole
                # number is presented to a client as a floor.
        return found

    # ── URL matching ────────────────────────────────────────────────────────

    def _landing_hits(self, urls: Iterable[str], competitor_domain: str | None) -> list[str]:
        found: list[str] = []
        patterns = self._dc_landing_urls.get((competitor_domain or "").lower(), [])

        for url in urls:
            if not url:
                continue
            low = url.lower()
            # Match on host+path+query, not the raw string, so a place name appearing in
            # a tracking parameter's *value* still counts but one in the fragment does not.
            parts = urlsplit(low)
            hay = f"{parts.netloc}{parts.path}?{parts.query}"

            for pat in patterns:
                if pat in hay:
                    found.append(f'landing_url: "{pat}" in {url[:120]}')

            # Generic place-in-path patterns, applied to every domain. This is what
            # catches a location page nobody added to the per-brand list yet.
            path_and_query = f"{parts.path}?{parts.query}"
            for rx in self._dc_path_any:
                m = rx.search(path_and_query)
                if m:
                    found.append(f'landing_path: "{m.group(0)}" in {url[:120]}')

            for pat in self._dc_landing_query:
                # Require a delimiter after the pattern. A bare substring test lets
                # "utm_campaign=dc" match "utm_campaign=dco_test", and DCO is a Meta
                # term that turns up in real campaign names.
                if re.search(rf"{re.escape(pat)}(?![a-z0-9])", hay):
                    found.append(f'landing_url_param: "{pat}" in {url[:120]}')
        return found

    # ── public entry point ──────────────────────────────────────────────────

    def classify(
        self,
        *,
        text_fields: dict[str, str],
        urls: Iterable[str] = (),
        competitor_domain: str | None = None,
    ) -> GeoVerdict:
        """Classify one ad.

        text_fields  {field_name: text} — body, title, caption, cta_text, link_description,
                     and one entry per carousel card. Field names appear in the evidence
                     string, so name them the way you want to read them in an audit.
        urls         every landing URL on the ad, including carousel card links.
        competitor_domain  used to select the landing-page patterns for that brand.
        """
        matches: dict[str, list[str]] = {}

        landing = self._landing_hits(urls, competitor_domain)
        if landing:
            matches[TIER_DC_LANDING] = landing

        dc = self._scan(text_fields, self._dc_terms, self._dc_ambiguous)
        if dc:
            matches[TIER_DC_EXPLICIT] = dc

        reg = self._scan(text_fields, self._regional_terms, self._regional_ambiguous)
        if reg:
            matches[TIER_REGIONAL] = reg

        other = self._scan(text_fields, self._other_terms)
        if other:
            matches[TIER_OTHER] = other

        for tier in (TIER_DC_LANDING, TIER_DC_EXPLICIT, TIER_REGIONAL, TIER_OTHER):
            if tier in matches:
                return GeoVerdict(
                    relevance=tier,
                    evidence="; ".join(matches[tier][:4]),
                    all_matches=matches,
                )

        return GeoVerdict(relevance=TIER_NONE, evidence=None, all_matches=matches)


# ── mapping the actor's output onto the classifier ──────────────────────────

# Dynamic-creative ads carry Meta's own template tokens in the top-level copy fields:
# body_text is literally "{{product.brand}}" and title is "{{product.name}}". Measured
# against 7,305 real ads already in public.signals, 66% of bodies and 70% of titles are
# templates, and 69% of ads are DCO. Every one of those had its real copy in
# creative_assets. A classifier reading only the top level would be classifying two
# thirds of the market on placeholder text.
TEMPLATE_TOKEN = re.compile(r"\{\{[^}]*\}\}")


def _clean(v: Any) -> str:
    """Coerce to text and strip template tokens. Returns '' for anything unusable."""
    if isinstance(v, dict) and isinstance(v.get("text"), str):
        v = v["text"]
    if not isinstance(v, str):
        return ""
    return TEMPLATE_TOKEN.sub(" ", v).strip()


def extract_ad_fields(ad: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    """Pull text and URLs out of one ad.

    Accepts BOTH shapes, because there are two in play and they will both keep existing:

      normalized  what the internal pipeline stores in public.signals.data.ads[] —
                  flat keys (body_text, title, link_url) with carousel cards under
                  `creative_assets`.
      raw         what apify/facebook-ads-scraper returns directly — everything nested
                  under `snapshot`, cards under `snapshot.cards`.

    Defensive throughout. Every one of these keys is nullable in real data, and a shape
    change upstream should cost a field, not a run.
    """
    snap = ad.get("snapshot") if isinstance(ad.get("snapshot"), dict) else {}

    text_fields: dict[str, str] = {
        "body": _clean(ad.get("body_text")) or _clean(snap.get("body")),
        "title": _clean(ad.get("title")) or _clean(snap.get("title")),
        "caption": _clean(ad.get("caption")) or _clean(snap.get("caption")),
        "link_description": _clean(ad.get("link_description"))
        or _clean(snap.get("linkDescription")),
        "cta_text": _clean(ad.get("cta_text")) or _clean(snap.get("ctaText")),
        # page_name is deliberately NOT scanned. "Onelife Fitness Washington DC" as a page
        # name would mark every one of their ads dc_explicit forever.
    }

    urls: list[str] = []
    for u in (ad.get("link_url"), snap.get("linkUrl")):
        if isinstance(u, str) and u:
            urls.append(u)

    # Cards. `creative_assets` in the normalized shape, `snapshot.cards` in the raw one.
    cards = ad.get("creative_assets")
    if not isinstance(cards, list):
        cards = snap.get("cards") if isinstance(snap.get("cards"), list) else []

    seen_card_text: set[str] = set()
    for i, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        for keys, label in (
            (("body",), "body"),
            (("title",), "title"),
            (("caption",), "caption"),
            (("link_description", "linkDescription"), "link_description"),
            (("cta_text", "ctaText"), "cta_text"),
        ):
            val = ""
            for k in keys:
                val = _clean(card.get(k))
                if val:
                    break
            # DCO ads routinely repeat identical copy across every card. Deduping keeps
            # the evidence string readable instead of the same phrase four times.
            if val and val not in seen_card_text:
                seen_card_text.add(val)
                text_fields[f"card{i}_{label}"] = val
        for k in ("link_url", "linkUrl"):
            u = card.get(k)
            if isinstance(u, str) and u:
                urls.append(u)
                break

    for i, extra in enumerate(snap.get("extraTexts") or []):
        val = _clean(extra)
        if val:
            text_fields[f"extra_text{i}"] = val
    for extra in snap.get("extraLinks") or []:
        if isinstance(extra, str):
            urls.append(extra)

    # Dedupe URLs, preserving order. DCO ads repeat one URL across every card.
    seen_u: set[str] = set()
    urls = [u for u in urls if not (u in seen_u or seen_u.add(u))]

    return {k: v for k, v in text_fields.items() if v}, urls


# ── signals that are not geo, but travel with it ────────────────────────────

_RECRUITMENT_URL = re.compile(r"(careers?\.|/careers?|/jobs?|/hiring|greenhouse\.io|workday|lever\.co|icims)", re.I)
_RECRUITMENT_COPY = re.compile(
    r"\b(now hiring|we're hiring|we are hiring|join our team|join the team|apply now|"
    r"open roles?|job openings?|career opportunit|benefits package)\b",
    re.I,
)


def is_recruitment(text_fields: dict[str, str], urls: Iterable[str]) -> bool:
    """True when the ad is hiring, not selling.

    Real data made the case for this: BluePearl runs ads to
    careers.bluepearlvet.com/us/en/richmond-advanced-veterinarian. Counted naively, a
    recruitment push reads as competitive marketing pressure. For a client weighing how
    hard a competitor is pushing acquisition, that is the wrong conclusion from the
    right number.
    """
    for u in urls:
        if isinstance(u, str) and _RECRUITMENT_URL.search(u):
            return True
    return any(_RECRUITMENT_COPY.search(t) for t in text_fields.values())


_CAMPAIGN_PARAM = re.compile(r"utm_campaign=([^&]+)", re.I)
# Tokenised, not regexed with \b. Campaign names are underscore-delimited
# ("Prospecting_Awareness_English_Local_TSI_Disruptor") and an underscore is a word
# character, so \b never fires between "_" and "Local". Splitting on non-alphanumerics
# is both correct and readable, where the regex quietly matched nothing.
_LOCAL_TOKENS = {"local", "store", "stores", "geo", "dma", "market", "regional"}
# Scope words only. An earlier version included funnel and flighting words like
# "awareness", "brand" and "always", which made the real Mattress Firm campaign
# "Prospecting_Awareness_English_Local_TSI_Disruptor" read as both local and national
# and therefore as nothing. A campaign stage is not a geography.
_NATIONAL_TOKENS = {"national", "natl", "nationwide", "countrywide"}


def campaign_scope_hint(urls: Iterable[str]) -> str | None:
    """'local' | 'national' | None, read from the advertiser's own UTM campaign name.

    Real Mattress Firm ads carry utm_campaign=Prospecting_Awareness_English_Local_TSI_
    Disruptor and ..._English_National_MFRM_PurePromo. That is the advertiser labelling
    their own intent, and it is the closest thing to targeting information that exists
    anywhere in this dataset.

    It is a NAMING CONVENTION, not a platform field. It says nothing about which locale,
    it is trivially inconsistent between advertisers, and a brand that does not use UTMs
    returns None. Carry it as a labelled inference or not at all. It must never be
    promoted into geo_relevance, which is reserved for what the creative actually says.
    """
    local = national = False
    for u in urls:
        if not isinstance(u, str):
            continue
        m = _CAMPAIGN_PARAM.search(u)
        if not m:
            continue
        tokens = {t for t in re.split(r"[^A-Za-z0-9]+", m.group(1).lower()) if t}
        if tokens & _LOCAL_TOKENS:
            local = True
        if tokens & _NATIONAL_TOKENS:
            national = True
    # A campaign naming both, or an ad set mixing both, says nothing usable. Returning
    # None there is the honest answer, not a coin flip.
    if local and not national:
        return "local"
    if national and not local:
        return "national"
    return None


_DEFAULT: GeoClassifier | None = None


def classify_ad(ad: dict[str, Any], competitor_domain: str | None = None) -> GeoVerdict:
    """Convenience wrapper: one raw actor item in, one verdict out."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = GeoClassifier()
    text_fields, urls = extract_ad_fields(ad)
    return _DEFAULT.classify(
        text_fields=text_fields, urls=urls, competitor_domain=competitor_domain
    )
