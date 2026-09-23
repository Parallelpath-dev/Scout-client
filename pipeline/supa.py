"""
Scout — the PostgREST client the collectors share.

This used to be pasted into every collector. Three copies of the same thirty lines is
three places to fix the next PostgREST quirk, and this codebase has already hit two of
them the hard way:

  * A non-default schema needs a profile header, and it differs by verb — Accept-Profile
    on reads, Content-Profile on writes. Miss it and you get PGRST106, which reads like
    a permissions problem and is not.

  * ON CONFLICT cannot use a PARTIAL unique index. PostgREST fails it with 42P10, after
    the run has already spent its Apify credits.

One copy. When the next quirk turns up it gets fixed once.
"""

from __future__ import annotations

import json
from typing import Any

import requests

CHUNK = 250
READ_TIMEOUT_S = 60
WRITE_TIMEOUT_S = 120


class Supa:
    """Thin PostgREST client. Service role, so RLS does not apply to anything here.

    Note that service_role bypasses RLS but NOT grants: a table in a non-public schema
    still needs `grant usage on schema` and `grant ... on table` before any of this
    works. That one cost us a 403 that looked exactly like a bad key.
    """

    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.h = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _headers(self, schema: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = dict(self.h)
        h["Accept-Profile"] = schema
        h["Content-Profile"] = schema
        if extra:
            h.update(extra)
        return h

    def get(self, schema: str, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        r = requests.get(
            f"{self.url}/rest/v1/{table}",
            headers=self._headers(schema),
            params=params,
            timeout=READ_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()

    def insert(
        self, schema: str, table: str, rows: list[dict[str, Any]], *, returning: str = "minimal"
    ) -> list[dict[str, Any]]:
        """Plain insert. `returning='representation'` gives the written rows back, which
        is how a signal's generated id is obtained for linking."""
        if not rows:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i : i + CHUNK]
            r = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers=self._headers(schema, {"Prefer": f"return={returning}"}),
                data=json.dumps(chunk),
                timeout=WRITE_TIMEOUT_S,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"{table} insert failed {r.status_code}: {r.text[:500]}")
            if returning == "representation" and r.text:
                out.extend(r.json())
        return out

    def upsert(
        self,
        schema: str,
        table: str,
        rows: list[dict[str, Any]],
        on_conflict: str | None = None,
        *,
        returning: str = "minimal",
    ) -> list[dict[str, Any]] | int:
        if not rows:
            return [] if returning == "representation" else 0
        params = {"on_conflict": on_conflict} if on_conflict else {}
        prefer = f"resolution=merge-duplicates,return={returning}"
        out: list[dict[str, Any]] = []
        written = 0
        for i in range(0, len(rows), CHUNK):
            chunk = rows[i : i + CHUNK]
            r = requests.post(
                f"{self.url}/rest/v1/{table}",
                headers=self._headers(schema, {"Prefer": prefer}),
                params=params,
                data=json.dumps(chunk),
                timeout=WRITE_TIMEOUT_S,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"{table} upsert failed {r.status_code}: {r.text[:500]}")
            if returning == "representation" and r.text:
                out.extend(r.json())
            written += len(chunk)
        return out if returning == "representation" else written

    def patch(
        self, schema: str, table: str, params: dict[str, str], patch: dict[str, Any]
    ) -> int:
        """Update the rows matching `params`.

        `params` is a PostgREST filter and it is NOT optional: an empty filter updates
        every row in the table and PostgREST will happily do it.
        """
        if not params:
            raise ValueError("refusing to PATCH without a filter")
        r = requests.patch(
            f"{self.url}/rest/v1/{table}",
            headers=self._headers(schema, {"Prefer": "return=minimal"}),
            params=params,
            data=json.dumps(patch),
            timeout=WRITE_TIMEOUT_S,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{table} patch failed {r.status_code}: {r.text[:500]}")
        return 1
