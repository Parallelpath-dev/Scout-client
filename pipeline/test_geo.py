"""
Tests for the geo classifier.

Fixtures are hand-written, but the SHAPES and failure modes come from 7,305 real ads
already in public.signals from the internal pipeline. Copy is paraphrased; every case
below exists because the real corpus showed a naive version getting it wrong.



Run:  python3 test_geo.py
"""

import sys

from geo import (
    campaign_scope_hint,
    is_recruitment,
    TIER_DC_EXPLICIT,
    TIER_DC_LANDING,
    TIER_NONE,
    TIER_OTHER,
    TIER_REGIONAL,
    GeoClassifier,
    classify_ad,
    extract_ad_fields,
)

C = GeoClassifier()
FAILS: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILS.append(f"{name}\n    expected: {want}\n    got:      {got}")


def tier(text_fields, urls=(), domain=None):
    return C.classify(text_fields=text_fields, urls=urls, competitor_domain=domain).relevance


def ev(text_fields, urls=(), domain=None):
    return C.classify(text_fields=text_fields, urls=urls, competitor_domain=domain).evidence


# ── tier precedence ─────────────────────────────────────────────────────────
# A landing page beats copy. An ad whose body says "now in Denver" but which links to
# the Tenleytown page is a DC ad with stale copy, not a Denver ad.

check(
    "landing page outranks other-market copy",
    tier(
        {"body": "Our newest club is open in Denver!"},
        urls=["https://onelifefitness.com/gyms/tenleytown"],
        domain="onelifefitness.com",
    ),
    TIER_DC_LANDING,
)

check(
    "explicit DC place outranks regional umbrella",
    tier({"body": "Join us in Columbia Heights, serving the whole DMV."}),
    TIER_DC_EXPLICIT,
)

check(
    "regional outranks other_market",
    tier({"body": "Northern Virginia members get guest passes at our Baltimore clubs."}),
    TIER_REGIONAL,
)


# ── the ambiguity traps ─────────────────────────────────────────────────────
# These are the ones that silently inflate a DC count if you match naively.

check(
    "bare 'Columbia' is not Columbia Heights",
    tier({"body": "Proud partner of Columbia University athletics."}),
    TIER_NONE,
)

check(
    "'Columbia Heights' matches on the full term",
    tier({"body": "Now open in Columbia Heights."}),
    TIER_DC_EXPLICIT,
)

check(
    "'Columbia' qualifies when a disambiguator is present",
    tier({"body": "Our Columbia gym in Washington is hiring."}),
    TIER_DC_EXPLICIT,
)

check(
    "bare 'VA' inside prose does not fire regional",
    tier({"body": "Va va voom, our new class schedule is here."}),
    TIER_NONE,
)

check(
    "'VA' fires regional with a qualifier",
    tier({"body": "Va va voom. Now serving Reston, VA."}),
    TIER_REGIONAL,
)

# Sterling is one of Sportrock's four gyms but it is outer commuter shed, ~40 minutes
# from Columbia Heights. It is regional, not DC market. Calling it dc_explicit would put
# Loudoun County activity into the number Kyle reads as local.
check(
    "Sterling is regional, not dc_explicit",
    tier({"body": "Now open in Sterling. New climbing gym."}),
    TIER_REGIONAL,
)

check(
    "Springfield is regional, not dc_explicit",
    tier({"body": "Springfield members get guest passes."}),
    TIER_REGIONAL,
)

check(
    "'MD' alone is not Maryland",
    tier({"body": "Ask our MD about the new recovery protocol."}),
    TIER_NONE,
)

check(
    "word boundaries hold: 'menu streets' is not 'U Street'",
    tier({"body": "Check the menu streets ahead of your visit."}),
    TIER_NONE,
)

check(
    "punctuation tolerance: 'U-Street' still matches",
    tier({"body": "Our U-Street location has a new sauna."}),
    TIER_DC_EXPLICIT,
)


# ── Baltimore is not this market ────────────────────────────────────────────
# Movement runs Timonium and Hampden. Treating them as regional would put a different
# metro's activity into Kyle's DC number.

check(
    "Timonium is other_market, not regional",
    tier({"body": "Climb with us in Timonium this weekend."}),
    TIER_OTHER,
)

check(
    "Baltimore is other_market",
    tier({"body": "Now open in Baltimore."}),
    TIER_OTHER,
)


# ── the monitored gyms outside the District still count as DC market ────────
# Movement's monitored gym is in Arlington, Sportrock's is in Alexandria. An ad for
# either is locally relevant to a Columbia Heights prospect.

check(
    "Crystal City is dc_explicit",
    tier({"body": "Crystal City members: new hours starting Monday."}),
    TIER_DC_EXPLICIT,
)

check(
    "Alexandria is dc_explicit",
    tier({"body": "Alexandria climbing, seven days a week."}),
    TIER_DC_EXPLICIT,
)

check(
    "Rockville is regional, not dc_explicit",
    tier({"body": "Visit our Rockville location."}),
    TIER_REGIONAL,
)


# ── 'none' is not a negative finding, but it must still be reachable ────────

check(
    "generic national creative is none",
    tier({"body": "Get 50% off your first month. Join today.", "cta_text": "Sign Up"}),
    TIER_NONE,
)


# ── URL handling ────────────────────────────────────────────────────────────

# Brand-specific patterns are domain-scoped. The YMCA's DC branch page is `bid=01`,
# which carries no place name, so only the per-brand list can recognise it and only for
# that brand. A path that DOES contain a place name matches on any domain, by design,
# because that is what catches a location page nobody has added to a list yet.
check(
    "an opaque brand-specific pattern is scoped to its own domain",
    tier(
        {"body": "Join today."},
        urls=["https://ymcadc.org/branch.cfm?bid=01"],
        domain="movementgyms.com",
    ),
    TIER_NONE,
)

check(
    "the same opaque pattern fires for its own brand",
    tier(
        {"body": "Join today."},
        urls=["https://ymcadc.org/branch.cfm?bid=01"],
        domain="ymcadc.org",
    ),
    TIER_DC_LANDING,
)

check(
    "utm campaign hint fires dc_landing",
    tier(
        {"body": "Join today."},
        urls=["https://vidafitness.com/join?utm_campaign=dc-fall&utm_source=fb"],
        domain="vidafitness.com",
    ),
    TIER_DC_LANDING,
)

check(
    "place name in the fragment does not count",
    tier(
        {"body": "Join today."},
        urls=["https://movementgyms.com/join#crystal-city"],
        domain="movementgyms.com",
    ),
    TIER_NONE,
)


# ── evidence is always populated when a tier fires ──────────────────────────

_e = ev({"body": "Now open in Tenleytown."})
check("evidence names the field and the match", ("body" in (_e or "")) and ("Tenleytown" in (_e or "")), True)
check("none carries no evidence", ev({"body": "Join today."}), None)


# ── extraction from the actor's real output shape ───────────────────────────
# Carousel ads carry their copy on cards, not at the top level. Missing that means a
# whole ad format classifies as `none` forever and nobody notices.

# ── shapes and patterns taken from real ads in public.signals ───────────────
# Everything below was written after reading 7,305 real ads already collected by the
# internal pipeline for other clients. The copy is paraphrased, the SHAPES are real, and
# each case is here because the real corpus showed the naive version failing.

# 66% of real bodies and 70% of real titles are Meta dynamic-creative template tokens.
# The real copy is in creative_assets. A classifier reading only the top level would be
# grading two thirds of the market on the string "{{product.brand}}".
DCO_NORMALIZED = {
    "ad_archive_id": "1728696355139811",
    "page_id": "300826405901",
    "page_name": "Onelife Fitness",
    "body_text": "{{product.brand}}",
    "title": "{{product.name}}",
    "link_description": "{{product.description}}",
    "cta_text": "Learn more",
    "display_format": "DCO",
    "cards_count": 2,
    "link_url": "https://onelifefitness.com/join?utm_source=meta&utm_campaign=Prospecting_Awareness_English_National_OLF",
    "creative_assets": [
        {
            "type": "card",
            "title": "Your Tenleytown club",
            "body": "New sauna, new schedule. Come see the Tenleytown location.",
            "link_url": "https://onelifefitness.com/gyms/tenleytown",
        },
        {
            "type": "card",
            "title": "Your Tenleytown club",
            "body": "New sauna, new schedule. Come see the Tenleytown location.",
            "link_url": "https://onelifefitness.com/gyms/tenleytown",
        },
    ],
}

_f, _u = extract_ad_fields(DCO_NORMALIZED)
check("template tokens are stripped from body", _f.get("body"), None)
check("card copy is extracted from creative_assets", "card0_body" in _f, True)
check("identical repeated card copy is deduped", "card1_body" in _f, False)
check("duplicate urls are deduped", len(_u), 2)
check(
    "a DCO ad classifies off its creative_assets",
    classify_ad(DCO_NORMALIZED, "onelifefitness.com").relevance,
    TIER_DC_LANDING,
)

# Real ads put the geography in the URL path while the copy says nothing at all.
# Casper: stores.casper.com/dc/washington/  VEG: veg.com/locations/dc
# Neither path is in any per-brand list, so only the generic patterns catch them.
GEO_IN_PATH_ONLY = {
    "ad_archive_id": "2",
    "page_id": "220098116012",
    "body_text": "Pet emergencies are not scheduled. We are here day and night.",
    "title": "Here when you need us",
    "link_url": "https://www.vidafitness.com/locations/dc",
    "creative_assets": [],
}
check(
    "geo in the path is caught with no geo in the copy",
    classify_ad(GEO_IN_PATH_ONLY, "vidafitness.com").relevance,
    TIER_DC_LANDING,
)

check(
    "a path not on the brand list still matches the generic pattern",
    tier(
        {"body": "Join today."},
        urls=["https://onelifefitness.com/gyms/columbia-heights"],
        domain="onelifefitness.com",
    ),
    TIER_DC_LANDING,
)

check(
    "'/dc' is anchored and does not match '/dc-comics'",
    tier({"body": "Join today."}, urls=["https://example.com/dc-comics/sale"], domain="example.com"),
    TIER_NONE,
)

# "your local store", "visit a local Sleep Expert" appear constantly in national
# creative. "local" is not a geographic reference and must never be in the vocabulary.
check(
    "the word 'local' is not a geo signal",
    tier({"body": "Visit your local store today and talk to a local expert."}),
    TIER_NONE,
)

# page_name is never scanned. A page called "Onelife Fitness Washington DC" would
# otherwise mark every ad that brand runs as dc_explicit, forever.
check(
    "page_name is not scanned",
    classify_ad(
        {"ad_archive_id": "3", "page_name": "Onelife Fitness Washington DC",
         "body_text": "Join today.", "link_url": "https://onelifefitness.com/join"},
        "onelifefitness.com",
    ).relevance,
    TIER_NONE,
)

# Advertisers label their own intent in UTM campaign names. Real Mattress Firm ads carry
# both ..._English_Local_TSI_Disruptor and ..._English_National_MFRM_PurePromo. This is a
# naming convention, not a platform field, and it stays out of geo_relevance.
check(
    "campaign scope hint reads 'local'",
    campaign_scope_hint(
        ["https://x.com/a?utm_campaign=Prospecting_Awareness_English_Local_TSI_Disruptor"]
    ),
    "local",
)
check(
    "campaign scope hint reads 'national'",
    campaign_scope_hint(
        ["https://x.com/a?utm_campaign=Prospecting_Conversion_English_National_MFRM"]
    ),
    "national",
)
check("campaign scope hint is None when both appear", campaign_scope_hint(
    ["https://x.com/a?utm_campaign=Local_x", "https://x.com/b?utm_campaign=National_y"]), None)
check("campaign scope hint is None with no utm", campaign_scope_hint(["https://x.com/a"]), None)
check(
    "campaign scope never leaks into geo_relevance",
    tier({"body": "Join today."},
         urls=["https://onelifefitness.com/join?utm_campaign=Awareness_Local_OLF"],
         domain="onelifefitness.com"),
    TIER_NONE,
)

# Recruitment ads are not acquisition pressure. Real example: BluePearl driving to
# careers.bluepearlvet.com/us/en/richmond-advanced-veterinarian.
_rf = {"body": "Join a caring team where colleagues support each other.", "title": "Now hiring"}
check("recruitment detected from copy", is_recruitment(_rf, []), True)
check(
    "recruitment detected from a careers URL",
    is_recruitment({"body": "Grow with us."}, ["https://careers.example.com/us/en/role"]),
    True,
)
check(
    "an ordinary membership ad is not recruitment",
    is_recruitment({"body": "Join today and get your first month for $87."},
                   ["https://boulderingproject.com/join"]),
    False,
)


CAROUSEL = {
    "adArchiveId": "1234567890",
    "snapshot": {
        "body": {"text": "Three ways to train this fall."},
        "title": None,
        "caption": None,
        "linkDescription": None,
        "ctaText": "Learn More",
        "linkUrl": "https://onelifefitness.com/join",
        "pageName": "Onelife Fitness",
        "cards": [
            {
                "body": "Tenleytown: new sauna",
                "title": "Tenleytown",
                "linkUrl": "https://onelifefitness.com/gyms/tenleytown",
                "ctaText": "Learn More",
            },
            {
                "body": "Springfield: youth programs",
                "title": "Springfield",
                "linkUrl": "https://onelifefitness.com/gyms/springfield",
                "ctaText": "Learn More",
            },
        ],
    },
}

_fields, _urls = extract_ad_fields(CAROUSEL)
check("carousel card copy is extracted", "card0_body" in _fields, True)
check("carousel card urls are extracted", len(_urls), 3)
check(
    "carousel classifies off its cards",
    classify_ad(CAROUSEL, "onelifefitness.com").relevance,
    TIER_DC_LANDING,
)

EMPTY = {"adArchiveId": "1", "snapshot": {}}
check("an empty snapshot does not raise", classify_ad(EMPTY, "sportrock.com").relevance, TIER_NONE)

NULLS = {
    "adArchiveId": "2",
    "snapshot": {
        "body": None,
        "title": None,
        "caption": None,
        "linkDescription": None,
        "ctaText": None,
        "linkUrl": None,
        "cards": None,
        "extraTexts": None,
        "extraLinks": None,
    },
}
check("all-null fields do not raise", classify_ad(NULLS, "vidafitness.com").relevance, TIER_NONE)


check(
    "'utm_campaign=dc' does not match 'utm_campaign=dco_test'",
    tier({"body": "Join today."},
         urls=["https://vidafitness.com/join?utm_campaign=dco_test"],
         domain="vidafitness.com"),
    TIER_NONE,
)

check(
    "real Mattress Firm local campaign name is read as local",
    campaign_scope_hint(
        ["https://x.com/a?utm_campaign=Prospecting_Awareness_English_Local_TSI_Disruptor"]),
    "local",
)


# ── what the first real collection run taught ───────────────────────────────
# 380 real ads, 23 Sep 2026. These cases come from copy that actually ran.

# Five YMCA ads are titled exactly this and scored `none`, because the vocabulary
# had the spelled-out forms and not the one they use.
check(
    "'Metropolitan Washington' is dc_explicit",
    tier({"title": "YMCA of Metropolitan Washington"}),
    TIER_DC_EXPLICIT,
)

# Bare "Washington" stays ambiguous. It is a state, a university, a president and
# a great many street names.
check(
    "'Washington University' is not DC",
    tier({"body": "Proud partner of Washington University athletics."}),
    TIER_NONE,
)
check(
    "'Washington state' is not DC",
    tier({"body": "Now open at our Washington state locations."}),
    TIER_NONE,
)
check(
    "'Washington, DC' still hits",
    tier({"body": "Proud to serve Washington, DC since 1905."}),
    TIER_DC_EXPLICIT,
)

# Movement's real creative, verbatim in shape: national copy, national landing page.
# `none` is the CORRECT answer here and the test exists so nobody "fixes" it later.
check(
    "genuinely placeless national creative stays none",
    tier(
        {"body": "Climbing. Yoga. Fitness. Community. Get 15 days of unlimited access "
                 "for less than the price of two day passes!",
         "title": "2 Weeks Unlimited Climbing"},
        urls=["https://movementgyms.com/trial-membership/"],
        domain="movementgyms.com",
    ),
    TIER_NONE,
)


# ── report ──────────────────────────────────────────────────────────────────

if FAILS:
    print(f"FAIL — {len(FAILS)} of the checks did not pass:\n")
    for f in FAILS:
        print("  " + f + "\n")
    sys.exit(1)

print("All geo classifier checks passed.")
