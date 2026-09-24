"""
Scout — which signals belong to a briefing week.

One definition, imported by the synthesizer and by the validation gate. If they ever
disagreed, the gate would check a briefing against a different set of signal ids from
the ones it was written from, and either pass a fabrication or hold a clean week.

THE RULE
--------
A briefing for week W (a Monday) reads:

  * every signal filed under week_of = W. The ad, web and search collectors all run on
    Monday and stamp that Monday. Web changes are "since the last snapshot", so the
    Monday row already describes the week just gone.

  * email filed under week_of = W - 7 as well. Email is filed under the week it was
    SENT (classify_emails.py reads the message's own headers), so last Tuesday's promo
    carries last Monday's week_of. Without this line the briefing would never see the
    week's email at all.

  * any row with no week_of, collected inside [W, W+7). A fallback for a writer that
    does not stamp week_of. It should match nothing; if it matches something, the
    writer needs fixing, not this function.

page_snapshot rows are excluded. They are the web collector's memory, not evidence.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from supa import Supa

PAGE = 1000

SELECT = (
    "id,competitor_id,channel_id,source_scope,geo_relevance,geo_evidence,"
    "signal_type,data,source_url,observed_at,collected_at,week_of"
)

# Types whose week_of is the week they happened rather than the week they were collected.
FILED_BY_EVENT_DATE = {"email"}


def week_of(d: date | None = None) -> date:
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def parse_week(s: str | None) -> date:
    if not s:
        return week_of()
    return week_of(datetime.strptime(s[:10], "%Y-%m-%d").date())


def in_window(signal: dict[str, Any], wk: date) -> bool:
    """Pure form of the rule above, so it can be tested without a database."""
    if signal.get("signal_type") == "page_snapshot":
        return False
    w = signal.get("week_of")
    if w:
        w = str(w)[:10]
        if w == wk.isoformat():
            return True
        prev = (wk - timedelta(days=7)).isoformat()
        return w == prev and signal.get("signal_type") in FILED_BY_EVENT_DATE
    c = str(signal.get("collected_at") or "")[:10]
    return bool(c) and wk.isoformat() <= c < (wk + timedelta(days=7)).isoformat()


def _get_all(sb: Supa, schema: str, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    """PostgREST caps a response at the project's max rows. Page until a short page."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        p = dict(params, limit=str(PAGE), offset=str(offset))
        batch = sb.get(schema, table, p)
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        offset += PAGE


def fetch_week_signals(
    sb: Supa, client_id: str, wk: date, select: str = SELECT
) -> list[dict[str, Any]]:
    prev = wk - timedelta(days=7)
    stamped = _get_all(sb, "portal", "signals", {
        "client_id": f"eq.{client_id}",
        "week_of": f"in.({wk.isoformat()},{prev.isoformat()})",
        "signal_type": "neq.page_snapshot",
        "select": select,
        "order": "id",
    })
    unstamped = _get_all(sb, "portal", "signals", {
        "client_id": f"eq.{client_id}",
        "week_of": "is.null",
        "signal_type": "neq.page_snapshot",
        "and": f"(collected_at.gte.{wk.isoformat()},"
               f"collected_at.lt.{(wk + timedelta(days=7)).isoformat()})",
        "select": select,
        "order": "id",
    })
    return [s for s in stamped + unstamped if in_window(s, wk)]
