-- portal schema, migration 010 — signals that belong to the client, not a competitor
-- APPLIED to the live database before this file existed. Committed 24 Sep 2026 so a
-- rebuild from migrations/ reproduces the live schema. Idempotent: safe to re-run.
--
-- Search position rows describe Bouldering Project's own rankings. They have no
-- competitor, so portal.signals.competitor_id has to accept NULL.
--
-- That breaks the dedupe index from migration 008 in a quiet way: NULLs are distinct by
-- default, so two client-level rows with the same (signal_type, external_ref, week_of)
-- never collide and a re-run inserts a duplicate instead of updating. NULLS NOT DISTINCT
-- (Postgres 15+) makes NULL competitor_id rows collide like any other.
--
-- Still deliberately NOT partial: PostgREST cannot ON CONFLICT a predicated index (42P10).

alter table portal.signals
  alter column competitor_id drop not null;

drop index if exists portal.portal_signals_dedupe_idx;

create unique index portal_signals_dedupe_idx
  on portal.signals (competitor_id, signal_type, external_ref, week_of)
  nulls not distinct;

comment on index portal.portal_signals_dedupe_idx is
  'NULLS NOT DISTINCT so client-level rows (competitor_id NULL) dedupe like competitor '
  'rows. Not partial: PostgREST ON CONFLICT cannot match a predicated index (42P10).';

-- Verify: expect is_nullable = YES and the index definition to end NULLS NOT DISTINCT.
select column_name, is_nullable
  from information_schema.columns
 where table_schema = 'portal' and table_name = 'signals' and column_name = 'competitor_id';
select indexdef from pg_indexes
 where schemaname = 'portal' and indexname = 'portal_signals_dedupe_idx';
