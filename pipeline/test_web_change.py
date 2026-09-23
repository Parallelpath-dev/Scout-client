"""
Tests for web change classification.

Fixtures are hand-written but modelled on the real pages being watched: Movement's
national memberships page, Sportrock's flat-rate membership page, VIDA's options page,
the YMCA branch page, Onelife's DC gyms index. The prices are the real ones from the
competitive set — $124, $116, $90, $113, $129, $87 first month.

The thing these tests exist to prevent is the failure mode that kills page monitoring:
reporting everything, so nobody reads any of it.

Run:  python3 test_web_change.py
"""

import sys

from web_change import (
    COSMETIC,
    COSMETIC_ONLY,
    CONTENT,
    HOURS,
    LOCATION,
    MATERIAL,
    MINOR,
    OFFER,
    PRICE,
    classify_change,
    prices_in,
    should_surface,
    surface_rank,
)

FAILS: list[str] = []


def check(name, got, want):
    if got != want:
        FAILS.append(f"{name}\n    expected: {want}\n    got:      {got}")


def snap(**kw):
    base = {"title": "", "meta_description": "", "h1": "", "h2s": [],
            "hero": "", "ctas": [], "nav": [], "body": ""}
    base.update(kw)
    return base


# ── nothing happened ────────────────────────────────────────────────────────

same = snap(h1="Membership", body="Unlimited climbing from $116/month.")
check("identical pages are not a change", classify_change(same, same).changed, False)

check(
    "first sight of a page is not a change",
    classify_change(None, same).changed,
    False,
)

# A page that differs only in the parts that differ on every load. This is the single
# most important test here: without it every page reports a change every week and the
# whole tab becomes noise.
before = snap(body="Unlimited climbing from $116/month. Open 6:00 AM - 10:00 PM. Copyright 2026")
after = snap(body="Unlimited climbing from $116/month. Open 6:00 AM - 10:00 PM. Copyright 2026")
after["body"] = after["body"].replace("Copyright 2026", "Copyright 2026") + "  "
check("whitespace and boilerplate do not register", classify_change(before, after).changed, False)


# ── price ───────────────────────────────────────────────────────────────────

check("money is extracted", prices_in("From $116/mo, first month $87"), ["116", "87"])
check("thousands separators survive", prices_in("Founding rate $1,249/yr"), ["1249"])
check("$116.00 and $116 are the same price", prices_in("$116.00"), ["116"])
check("bare numbers are not prices", prices_in("15 days unlimited, est. 2011"), [])

v = classify_change(
    snap(body="Unlimited membership $116/month."),
    snap(body="Unlimited membership $124/month."),
)
check("a price change is detected", PRICE in v.change_types, True)
check("a price change is material", v.materiality, MATERIAL)
check("the old price is kept", v.price_before, ["116"])
check("the new price is kept", v.price_after, ["124"])
check("a price change surfaces", should_surface(v), True)


# ── the Movement caveat, which is the whole reason scope is a field ─────────
# Movement prices by home gym. A change on their national page may not be real in DC.
# Reporting it without that caveat hands Kyle a number to act on that is not true here.

v = classify_change(
    snap(body="Memberships from $116/month."),
    snap(body="Memberships from $109/month."),
    channel_scope="national",
    prices_by_home_gym=True,
)
check("national price change is material", v.materiality, MATERIAL)
check("but may not apply here", v.applies_locally, "unknown")
check("and carries a caveat", "prices by home gym" in (v.caveat or ""), True)

v = classify_change(
    snap(body="Crystal City membership $116/month."),
    snap(body="Crystal City membership $109/month."),
    channel_scope="local",
    prices_by_home_gym=True,
)
check("the same change on the LOCAL page does apply", v.applies_locally, "yes")
check("and needs no caveat", v.caveat, None)

# Sportrock, VIDA and the YMCA operate only in this market, so even a page that looks
# national is local by definition. Flagging their prices as "may not apply" would be
# wrong and would teach the reader to ignore the caveat.
v = classify_change(
    snap(body="All locations $90/month."),
    snap(body="All locations $95/month."),
    channel_scope="national",
    single_market=True,
)
check("a single-market brand's national page is local", v.applies_locally, "yes")
check("no caveat for a single-market brand", v.caveat, None)


# ── offers ──────────────────────────────────────────────────────────────────

v = classify_change(
    snap(body="Join today. Memberships from $113/month."),
    snap(body="Join today. Memberships from $113/month. $0 enrollment fee this month!"),
)
check("a new offer is detected", OFFER in v.change_types, True)
check("a new offer is material", v.materiality, MATERIAL)

# A standing promo must not re-report every week forever. This is why matching runs on
# ADDED text rather than on the whole new page.
standing = snap(body="Free trial available. Memberships from $113/month.")
standing_plus = snap(body="Free trial available. Memberships from $113/month. Now with towel service.")
v = classify_change(standing, standing_plus)
check("a standing offer does not re-report", OFFER in v.change_types, False)


# ── locations, which is the event that matters most right now ──────────────

v = classify_change(
    snap(nav=["Crystal City", "Rockville"]),
    snap(nav=["Crystal City", "Rockville", "Fairfax — Opening Soon"]),
)
check("a new location is detected", LOCATION in v.change_types, True)
check("a new location is material", v.materiality, MATERIAL)

v = classify_change(
    snap(body="Visit us in Tenleytown."),
    snap(body="Visit us in Tenleytown. Our Capitol Hill club is temporarily closed."),
)
check("a closure is detected", LOCATION in v.change_types, True)


# ── the noise floor ─────────────────────────────────────────────────────────

v = classify_change(
    snap(hero="Climb with us in the heart of the city."),
    snap(hero="Climb with us in the heart of DC."),
)
check("a small reword is cosmetic", v.materiality, COSMETIC_ONLY)
check("and does not surface", should_surface(v), False)

v = classify_change(
    snap(body="Our gym has bouldering walls and a weight room."),
    snap(body="Completely rebuilt page. Yoga studios, sauna, co-working, youth space, "
              "training boards, and a full calendar of community events every week."),
)
check("a full rewrite is content, not cosmetic", CONTENT in v.change_types, True)
check("and is at least minor", v.materiality in (MINOR, MATERIAL), True)
check("so it surfaces", should_surface(v), True)


# ── hours and schedule sit below price and offers ──────────────────────────

v = classify_change(
    snap(body="Open 6:00 AM - 10:00 PM daily."),
    snap(body="Open 5:00 AM - 11:00 PM daily. Holiday hours apply."),
)
check("changed hours are detected", HOURS in v.change_types, True)
check("changed hours are minor, not material", v.materiality, MINOR)
check("but still surface", should_surface(v), True)


# ── precedence and ordering ────────────────────────────────────────────────

v = classify_change(
    snap(hero="Welcome", body="Membership $116/month."),
    snap(hero="Welcome to our newly redesigned site", body="Membership $124/month."),
)
check("price wins over a cosmetic change in the same diff", v.headline_type, PRICE)

# Materiality outranks geography on purpose. A national reprice matters more than a DC
# page rewording its hero, even though only one of them is local.
price_national = classify_change(
    snap(body="$116/month"), snap(body="$124/month"), channel_scope="national")
cosmetic_local = classify_change(
    snap(hero="Climb here"), snap(hero="Climb with us"), channel_scope="local")
check(
    "a national price change outranks a local cosmetic one",
    surface_rank(price_national, "none") < surface_rank(cosmetic_local, "dc_landing"),
    True,
)


if FAILS:
    print(f"FAIL — {len(FAILS)} of the checks did not pass:\n")
    for f in FAILS:
        print("  " + f + "\n")
    sys.exit(1)

print("All web change checks passed.")
