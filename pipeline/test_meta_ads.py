#!/usr/bin/env python3
"""
classify_meta_ads.build_rows with a brand that publishes from two pages.

Bouldering Project runs ads from its corporate page and its DC location page. Both must
land on the one brand, and an ad from the location page is local whatever its copy says.

    python3 test_meta_ads.py
"""
import sys

import classify_meta_ads as cm
from geo import GeoClassifier

COMPS = [{"id": "bp", "name": "Bouldering Project", "domain": "boulderingproject.com",
          "single_market": False},
         {"id": "mov", "name": "Movement", "domain": "movementgyms.com", "single_market": False}]
PAGES = {
    "102483551673520": {"id": "ch-corp", "competitor_id": "bp", "external_id": "102483551673520",
                        "scope": "national", "max_ads": None},
    "109943621587165": {"id": "ch-dc", "competitor_id": "bp", "external_id": "109943621587165",
                        "scope": "local", "max_ads": None},
    "39916060837": {"id": "ch-mov", "competitor_id": "mov", "external_id": "39916060837",
                    "scope": "national", "max_ads": None},
}


def ad(aid, page, body):
    return {"adArchiveID": aid, "pageID": page, "startDateFormatted": "2026-09-22",
            "snapshot": {"body": {"text": body}, "displayFormat": "IMAGE", "pageId": page}}


def main() -> int:
    ads = [ad("1", "102483551673520", "Climb with us."),
           ad("2", "109943621587165", "Come climb with us."),
           ad("3", "39916060837", "Yoga at Movement."),
           ad("4", "999", "A stranger's ad.")]
    sig, roll, unmatched = cm.build_rows(ads, COMPS, PAGES, "cl", GeoClassifier(),
                                         {"bp": 2, "mov": 1})
    by = {s["external_ref"]: s for s in sig}
    fails = []
    if not (by["1"]["competitor_id"] == by["2"]["competitor_id"] == "bp"):
        fails.append("both pages map to the one brand")
    if by["2"]["source_scope"] != "local" or by["2"]["channel_id"] != "ch-dc":
        fails.append("a location-page ad is local and keeps its own channel")
    if by["1"]["source_scope"] != "national" or by["3"]["source_scope"] != "national":
        fails.append("national pages stay national")
    bp = next(r for r in roll if r["competitor_id"] == "bp")
    if bp["ads_sampled"] != 2 or bp["sample_method"] != "census":
        fails.append(f"brand rollup counts both pages: {bp}")
    if unmatched != {"999": 1}:
        fails.append("an unmonitored page is dropped and reported")
    from supa import only
    comps = [{"id": "a", "name": "VIDA"}, {"id": "b", "name": "Bouldering Project"}]
    if [c["id"] for c in only(comps, "bouldering project")] != ["b"] or only(comps, None) != comps:
        fails.append("--competitor narrows to one brand, blank keeps everyone")
    try:
        only(comps, "Nobody"); fails.append("an unknown --competitor must stop the run")
    except SystemExit:
        pass
    for f in fails:
        print("  FAIL ", f)
    print("All meta ad checks passed." if not fails else f"{len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
