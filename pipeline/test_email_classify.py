"""
Tests for inbound competitor email classification.

Fixtures are modelled on the real senders: Movement (national list, prices by home gym),
VIDA (U Street, DC only, mails from uacompanies.com), Sportrock (Alexandria, DC metro
only), Onelife (Tenleytown, multi-state). Prices are the real ones — $124, $116, $113,
$90, $87 first month.

Two failures these exist to prevent, both of which would quietly corrupt a number we
hand a client:

  1. The CAN-SPAM footer. Every marketing email carries the sender's mailing address.
     VIDA's says Washington, DC. Counted naively, "VIDA sent 4 DC-relevant emails" means
     "VIDA sent 4 emails", and the DC column becomes a volume column wearing a disguise.

  2. Our own double opt-in confirmations. They arrive at the alias, they are full of
     welcome-offer language, and they are entirely our own doing.

Run:  python3 test_email_classify.py
"""

import sys

from email_classify import (
    CONFIRMATION,
    EVENT,
    MATERIAL,
    MINOR,
    NEWSLETTER,
    NOISE,
    OFFER,
    OPENING,
    PRICE,
    TRANSACTIONAL,
    classify_email,
    html_to_text,
    should_surface,
    split_footer,
    surface_rank,
)

FAILS: list[str] = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n    expected: {want}\n    got:      {got}")


# The real shape of a footer: unsubscribe link, then the legally required address.
VIDA_FOOTER = (
    "\n\nYou're receiving this email because you signed up at vidafitness.com.\n"
    "Unsubscribe | Manage preferences\n"
    "VIDA Fitness, 1612 U St NW, Washington, DC 20009\n"
    "© 2026 Urban Adventures Companies"
)

MOVEMENT_FOOTER = (
    "\n\nUnsubscribe from these emails.\n"
    "Movement Gyms, 1235 S Clark St, Arlington, VA 22202\n"
)


# ── the footer problem, which is the whole reason this module splits the body ──

body = "Our new summer hours start Monday. Come climb with us." + VIDA_FOOTER
content, footer = split_footer(body)
check("the footer is found", "1612 U St NW" in footer, True)
check("the content is not swallowed", "new summer hours" in content, True)
check("the address is not left in the content", "Washington, DC 20009" in content, False)

v = classify_email(
    subject="Summer hours are here",
    text_body="Our new summer hours start Monday. Come climb with us." + VIDA_FOOTER,
    competitor_domain="vidafitness.com",
    single_market=True,
)
check("a footer address does not make an email DC-relevant", v.geo_relevance, "none")
check("but the footer match is recorded", v.geo_from_footer_only, True)
check("and says so in the evidence", "footer only" in (v.geo_evidence or ""), True)

# The same brand, actually saying something about the market.
v = classify_email(
    subject="Now open in Columbia Heights",
    text_body="Our newest club opens on 14th Street in Columbia Heights this month."
    + VIDA_FOOTER,
    competitor_domain="vidafitness.com",
    single_market=True,
)
check("a real DC mention in the content does count", v.geo_relevance, "dc_explicit")
check("and is not flagged as footer-only", v.geo_from_footer_only, False)

# An unsubscribe link in a preheader, near the top, is not the footer.
early = "Unsubscribe here. " + ("Climb with us this weekend. " * 40)
content, footer = split_footer(early)
check("an early unsubscribe is not treated as the footer", footer, "")


# ── our own confirmations, which must never count as competitor activity ──────

v = classify_email(
    subject="Please confirm your subscription",
    text_body="Thanks for signing up! Click below to confirm your subscription and "
    "get a free guest pass on your first visit.",
    competitor_domain="sportrock.com",
    single_market=True,
)
check("an opt-in confirmation is recognised", v.headline_type, CONFIRMATION)
check("and is noise despite the free pass language", v.materiality, NOISE)
check("and never surfaces", should_surface(v), False)


# ── offers ────────────────────────────────────────────────────────────────────

v = classify_email(
    subject="Last chance: $0 enrollment ends Sunday",
    text_body="Join this week and pay no enrollment fee." + MOVEMENT_FOOTER,
    competitor_domain="movementgyms.com",
)
check("an offer in the subject is detected", OFFER in v.email_types, True)
check("an offer is material", v.materiality, MATERIAL)
check("an offer surfaces", should_surface(v), True)

v = classify_email(
    subject="This week at Sportrock",
    text_body="Bring a friend — guest passes are free all week." ,
    competitor_domain="sportrock.com",
    single_market=True,
)
check("an offer in the body is detected", OFFER in v.email_types, True)


# ── price, and the Movement caveat ────────────────────────────────────────────

v = classify_email(
    subject="Memberships from $116/month",
    text_body="Unlimited climbing, yoga and fitness from $116/month." + MOVEMENT_FOOTER,
    competitor_domain="movementgyms.com",
    channel_scope="national",
    prices_by_home_gym=True,
)
check("a price is detected", PRICE in v.email_types, True)
check("the price is captured", "116" in v.prices, True)
check("a national price may not apply here", v.applies_locally, "unknown")
check("and carries the home-gym caveat", "prices by home gym" in (v.caveat or ""), True)

# The same email from a brand that only operates here needs no such hedge.
v = classify_email(
    subject="Memberships from $90/month",
    text_body="All Sportrock locations, $90/month." ,
    competitor_domain="sportrock.com",
    channel_scope="national",
    single_market=True,
)
check("a single-market brand's list is local", v.applies_locally, "yes")
check("and needs no caveat", v.caveat, None)

check("bare numbers are not prices",
      classify_email(subject="Open 7 days, est. 2011", text_body="Climb 7 days a week.",
                     single_market=True).email_types == [NEWSLETTER], True)


# ── openings, which is the event that matters most right now ─────────────────

v = classify_email(
    subject="Coming soon to Tenleytown",
    text_body="Our newest location is opening soon. Presale memberships available.",
    competitor_domain="onelifefitness.com",
)
check("an opening is detected", OPENING in v.email_types, True)
check("an opening is material", v.materiality, MATERIAL)


# ── events sit below offers and prices ───────────────────────────────────────

v = classify_email(
    subject="Comp night is back",
    text_body="Our monthly bouldering competition returns Friday. Register your spot.",
    competitor_domain="sportrock.com",
    single_market=True,
)
check("an event is detected", EVENT in v.email_types, True)
check("an event is minor, not material", v.materiality, MINOR)
check("but still surfaces", should_surface(v), True)


# ── the noise floor ──────────────────────────────────────────────────────────

v = classify_email(
    subject="Meet our coaches",
    text_body="This month we're introducing three of our route setters." + VIDA_FOOTER,
    competitor_domain="vidafitness.com",
    single_market=True,
)
check("a content newsletter is a newsletter", v.headline_type, NEWSLETTER)
check("and does not surface", should_surface(v), False)

v = classify_email(
    subject="Your booking is confirmed",
    text_body="Your intro class is confirmed for Saturday at 10am.",
    competitor_domain="sportrock.com",
    single_market=True,
)
check("a transactional email is recognised", TRANSACTIONAL in v.email_types, True)
check("and is noise", v.materiality, NOISE)
check("and does not surface", should_surface(v), False)


# ── precedence and ordering ──────────────────────────────────────────────────

v = classify_email(
    subject="New class schedule, plus $0 enrollment this week",
    text_body="Our new class schedule is live. And join by Friday for $0 enrollment.",
    competitor_domain="sportrock.com",
    single_market=True,
)
check("an offer outranks a schedule in the same email", v.headline_type, OFFER)

national_offer = classify_email(
    subject="$0 enrollment this month",
    text_body="No enrollment fee when you join in September.",
    competitor_domain="movementgyms.com",
    channel_scope="national",
)
local_newsletter = classify_email(
    subject="Meet the U Street team",
    text_body="Three of our U Street coaches share their favourite routes.",
    competitor_domain="vidafitness.com",
    single_market=True,
)
check(
    "a national offer outranks a local newsletter",
    surface_rank(national_offer) < surface_rank(local_newsletter),
    True,
)


# ── HTML, because most of these arrive as tables ─────────────────────────────

t = html_to_text(
    "<table><tr><td><h1>Join today</h1></td><td><p>$116/mo</p></td></tr></table>"
)
check("adjacent cells do not glue into one word", "today$116" in t.replace(" ", ""), False)
check("the words survive", "Join today" in t and "$116/mo" in t, True)

# The glue matters because a run-together word defeats the word-boundary anchors in the
# imported offer patterns. This is the same class of bug as `\b\$0 enrollment`.
v = classify_email(
    subject="",
    html_body="<table><tr><td>Sign up now</td><td>Free trial for new members</td></tr></table>",
    competitor_domain="sportrock.com",
    single_market=True,
)
check("an offer split across table cells is still found", OFFER in v.email_types, True)

check("empty html is empty text", html_to_text(""), "")
check(
    "an email with nothing in it does not crash",
    classify_email(subject=None, text_body=None, html_body=None).headline_type,
    NEWSLETTER,
)


if FAILS:
    print(f"FAIL — {len(FAILS)} of the checks did not pass:\n")
    for f in FAILS:
        print("  " + f + "\n")
    sys.exit(1)

print("All email classification checks passed.")
