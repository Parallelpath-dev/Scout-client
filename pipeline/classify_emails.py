#!/usr/bin/env python3
"""
Scout — inbound competitor email classifier.

Reads the raw messages that `portal-inbound-email` wrote, classifies each one, and
writes a `portal.signals` row for the ones that are competitor marketing.

THE DIVISION OF LABOUR, AND WHY IT IS THIS WAY ROUND
----------------------------------------------------
The edge function receives and stores. It makes no judgment at all, because a receiver
that can judge is a receiver that can fail on a judgment, and a message it fails on is
gone — Resend has already delivered it, there is no second copy. That is not a
hypothetical: `public.competitor_emails.competitor_name` is NOT NULL, which is why the
internal tool holds 335 messages for its own aliases and none for these four.

This script makes the judgments, hours or days later, where a failure costs a rerun.

RE-CLAIMING
-----------
A message from a sender we had not mapped is stored with match_status 'unmatched' and
left unclassified. Every run tries to claim those again against the current channel
config, so adding one sender domain retroactively picks up everything already sitting in
the table. Unmatched rows are never marked classified — they stay in the queue and get
counted in the summary, so an unrecognised sender is visible rather than silent.

USAGE
-----
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...

    python3 classify_emails.py --client bouldering-project --dry-run
    python3 classify_emails.py --client bouldering-project
    python3 classify_emails.py --client bouldering-project --reclassify   # redo everything
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

from email_classify import (
    CONFIRMATION,
    TRANSACTIONAL,
    as_signal_data,
    classify_email,
    should_surface,
    surface_rank,
)
from supa import Supa

PAGE = 500


def week_of(d: date | None = None) -> date:
    d = d or datetime.now(timezone.utc).date()
    return d - timedelta(days=d.weekday())


def parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def claim(
    row: dict[str, Any], channels: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Try to attach an unmatched row to a channel. Returns (channel, method).

    Same order as the receiver: the alias it was sent to is definitive, the sender domain
    is the fallback. The fallback exists because VIDA mails from uacompanies.com.
    """
    to_addr = (row.get("to_address") or "").lower()
    from_dom = (row.get("from_domain") or "").lower()

    for ch in channels:
        handle = (ch.get("handle") or "").lower()
        if handle and handle == to_addr:
            return ch, "to_address"

    for ch in channels:
        for d in ch.get("sender_domains") or []:
            d = (d or "").lower()
            if d and (from_dom == d or from_dom.endswith("." + d)):
                return ch, "sender_domain"

    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--dry-run", action="store_true", help="classify and print, write nothing")
    ap.add_argument(
        "--reclassify",
        action="store_true",
        help="re-run over already-classified messages (after a vocabulary change)",
    )
    args = ap.parse_args()

    supa_url, supa_key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY")
    if not supa_url or not supa_key:
        print("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
        return 2
    sb = Supa(supa_url, supa_key)

    clients = sb.get("public", "clients", {"slug": f"eq.{args.client}", "select": "id,name"})
    if not clients:
        print(f"no client with slug {args.client}", file=sys.stderr)
        return 2
    client_id, client_name = clients[0]["id"], clients[0]["name"]
    print(f"client: {client_name}")

    competitors = sb.get(
        "portal",
        "competitors",
        {
            "client_id": f"eq.{client_id}",
            "select": "id,name,domain,single_market,prices_by_location",
        },
    )
    comp_by_id = {c["id"]: c for c in competitors}

    channels = sb.get(
        "portal",
        "channels",
        {"purpose": "eq.email", "select": "id,competitor_id,handle,scope,sender_domains"},
    )
    channels = [c for c in channels if c["competitor_id"] in comp_by_id]
    ch_by_id = {c["id"]: c for c in channels}
    print(f"  {len(channels)} email channels across {len(comp_by_id)} competitors")

    params: dict[str, str] = {
        "select": "id,message_id,provider_id,dedupe_key,received_at,sent_at,to_address,"
        "from_address,from_domain,subject,text_body,html_body,competitor_id,channel_id,"
        "match_status,classified_at",
        "order": "received_at.asc",
        "limit": str(PAGE),
    }
    if not args.reclassify:
        params["classified_at"] = "is.null"

    rows = sb.get("portal", "inbound_emails", params)
    print(f"  {len(rows)} message(s) to look at")

    signals: list[dict[str, Any]] = []
    claims: list[tuple[str, dict[str, Any]]] = []
    done_ids: list[str] = []
    still_unmatched: list[dict[str, Any]] = []
    surfaced = 0
    skipped_other_client = 0

    for row in rows:
        comp_id = row.get("competitor_id")
        ch_id = row.get("channel_id")
        status = row.get("match_status")

        # Claim, or re-claim, anything without a competitor. The channel config may have
        # gained a sender domain since this message arrived.
        if status == "unmatched" or not comp_id:
            ch, method = claim(row, channels)
            if ch:
                comp_id, ch_id = ch["competitor_id"], ch["id"]
                claims.append(
                    (
                        row["id"],
                        {
                            "competitor_id": comp_id,
                            "channel_id": ch_id,
                            "client_id": client_id,
                            "match_status": "matched",
                            "match_method": method,
                        },
                    )
                )
            else:
                still_unmatched.append(row)
                # Deliberately NOT marked classified. It stays in the queue so that
                # adding one sender domain later picks it up, and stays in the summary
                # so an unknown sender is a visible fact rather than a silent drop.
                continue

        if comp_id not in comp_by_id:
            # Another client's mail sharing the same domain. Not ours to classify.
            skipped_other_client += 1
            continue

        if status == "ignored":
            # Our own confirmations and bounces. Marked done, no signal.
            done_ids.append(row["id"])
            continue

        comp = comp_by_id[comp_id]
        ch = ch_by_id.get(ch_id) or {}

        v = classify_email(
            subject=row.get("subject"),
            text_body=row.get("text_body"),
            html_body=row.get("html_body"),
            competitor_domain=comp.get("domain"),
            channel_scope=ch.get("scope") or "national",
            single_market=bool(comp.get("single_market")),
            prices_by_home_gym=bool(comp.get("prices_by_location")),
        )

        sent = parse_ts(row.get("sent_at")) or parse_ts(row.get("received_at"))
        observed = sent or datetime.now(timezone.utc)
        # The week the email was SENT, not the week the run happened. A message received
        # on a Saturday belongs to that week's briefing, not next Monday's.
        wk = week_of(observed.date())

        data = as_signal_data(v, subject=row.get("subject"), from_address=row.get("from_address"))
        data["to_address"] = row.get("to_address")
        data["surfaced"] = should_surface(v)
        data["rank"] = list(surface_rank(v))
        if v.headline_type in (CONFIRMATION, TRANSACTIONAL):
            data["surfaced"] = False

        if data["surfaced"]:
            surfaced += 1

        signals.append(
            {
                "client_id": client_id,
                "competitor_id": comp_id,
                "channel_id": ch_id,
                "source_scope": ch.get("scope") or "national",
                "geo_relevance": v.geo_relevance,
                "geo_evidence": v.geo_evidence,
                "signal_type": "email",
                "data": data,
                "observed_at": observed.isoformat(),
                "external_ref": row["id"],
                "week_of": wk.isoformat(),
            }
        )
        done_ids.append(row["id"])

        flag = "*" if data["surfaced"] else " "
        print(
            f"  {flag} {comp['name']:<10} {v.headline_type:<13} {v.geo_relevance:<12} "
            f"{(row.get('subject') or '')[:58]}"
        )

    print()
    print(f"  classified {len(signals)}, of which {surfaced} surface")
    if claims:
        print(f"  claimed {len(claims)} previously unmatched message(s)")
    if skipped_other_client:
        print(f"  skipped {skipped_other_client} belonging to another client")
    if still_unmatched:
        print(f"  {len(still_unmatched)} still unmatched — left in the queue:")
        seen: set[str] = set()
        for r in still_unmatched:
            dom = r.get("from_domain") or "(no sender)"
            if dom in seen:
                continue
            seen.add(dom)
            print(f"      {dom:<32} to {r.get('to_address')}  e.g. {(r.get('subject') or '')[:40]}")
        print("      add the domain to the competitor's channels.sender_domains to claim these")

    if args.dry_run:
        print("\n  dry run, nothing written")
        return 0

    for row_id, patch in claims:
        sb.patch("portal", "inbound_emails", {"id": f"eq.{row_id}"}, patch)

    if signals:
        written = sb.upsert(
            "portal",
            "signals",
            signals,
            on_conflict="competitor_id,signal_type,external_ref,week_of",
            returning="representation",
        )
        # Link each inbound row to the signal it produced, so a finding in the briefing
        # can be traced back to the actual email it came from.
        by_ref = {s.get("external_ref"): s.get("id") for s in (written or [])}
        for row_id in done_ids:
            sb.patch(
                "portal",
                "inbound_emails",
                {"id": f"eq.{row_id}"},
                {
                    "classified_at": datetime.now(timezone.utc).isoformat(),
                    "signal_id": by_ref.get(row_id),
                },
            )
        print(f"  wrote {len(signals)} signal(s)")
    else:
        for row_id in done_ids:
            sb.patch(
                "portal",
                "inbound_emails",
                {"id": f"eq.{row_id}"},
                {"classified_at": datetime.now(timezone.utc).isoformat()},
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
