-- ═══════════════════════════════════════════════════════════════════════════
-- portal schema, migration 008 — make the dedupe index usable by PostgREST
--
-- APPLIED 23 Sep 2026.
--
-- Migration 004 made the dedupe index PARTIAL:
--     ... where external_ref is not null and week_of is not null
--
-- PostgREST cannot use a partial index for ON CONFLICT. Its upsert emits a
-- bare column list, and Postgres will only match that against an index with no
-- predicate, so the upsert fails with 42P10 regardless of how correct the
-- columns are. The first real collection run got all the way to the write and
-- died there.
--
-- The predicate was defensive and unnecessary. Unique indexes treat NULLs as
-- distinct by default, so rows with a null external_ref never collide with
-- each other, which is exactly what the predicate was trying to buy.
-- ═══════════════════════════════════════════════════════════════════════════

drop index if exists portal.portal_signals_dedupe_idx;

create unique index portal_signals_dedupe_idx
  on portal.signals (competitor_id, signal_type, external_ref, week_of);

comment on index portal.portal_signals_dedupe_idx is
  'Deliberately NOT partial. PostgREST ON CONFLICT cannot match a predicated '
  'index and fails with 42P10. NULLs are distinct by default, so signal types '
  'with no external_ref never collide.';
