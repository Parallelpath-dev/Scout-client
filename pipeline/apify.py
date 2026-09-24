"""
Scout — the Apify runner the collectors share.

Start an actor, wait for it, page through its dataset. Used by the ad collector and the
organic social collector, so the next Apify quirk gets fixed once (the same reason
supa.py exists).

Two things the internal tool's runner did that this one deliberately does not:

  * It fetched `limit: 50` items and stopped. A census needs every item, so this pages
    until a short page.
  * It returned [] on a failed or timed-out run. An empty list from a failure is
    indistinguishable from a competitor that posted nothing, and "posted nothing" is a
    finding. This raises, and the caller decides what a failure means.
"""

from __future__ import annotations

import time
from typing import Any

import requests

APIFY_BASE = "https://api.apify.com/v2"
PAGE = 1000


class ApifyRunFailed(RuntimeError):
    pass


def run_actor(
    token: str,
    actor: str,
    payload: dict[str, Any],
    *,
    timeout_s: int = 1800,
    poll_s: int = 10,
    memory_mb: int | None = None,
) -> list[dict[str, Any]]:
    """Run `actor` ("owner/name" or "owner~name") with `payload`; return every item."""
    params: dict[str, Any] = {"token": token}
    if memory_mb:
        params["memory"] = memory_mb
    r = requests.post(
        f"{APIFY_BASE}/acts/{actor.replace('/', '~')}/runs",
        params=params, json=payload, timeout=60,
    )
    r.raise_for_status()
    run = r.json()["data"]
    run_id, dataset_id = run["id"], run["defaultDatasetId"]
    print(f"  apify {actor} run {run_id}")

    deadline = time.time() + timeout_s
    status = run["status"]
    while status in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(poll_s)
        s = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}", params={"token": token},
                         timeout=60)
        s.raise_for_status()
        status = s.json()["data"]["status"]

    if status != "SUCCEEDED":
        raise ApifyRunFailed(f"{actor} run {run_id} ended {status}")

    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        d = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": token, "offset": offset, "limit": PAGE, "clean": "true"},
            timeout=120,
        )
        d.raise_for_status()
        batch = d.json()
        if not batch:
            return items
        items.extend(batch)
        offset += len(batch)
