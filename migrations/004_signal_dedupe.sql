-- ═══════════════════════════════════════════════════════════════════════════
-- portal schema, migration 002 — idempotent collection
--
-- REVIEW ONLY. Nothing here has been applied.
--
-- Migration 001 gave portal.signals a uuid primary key and nothing else unique.
-- That is fine for an append-only log and wrong for a collector: re-run the Meta
-- pull twice in one week, on a retry or a manual kick, and every ad is counted
-- twice. The weekly rollup would then double and there is no way to tell from
-- the data that it happened.
--
-- Two columns and one index fix it. Both columns are useful beyond this one
-- collector, which is why they go on `signals` rather than into a Meta-specific
-- side table.
-- ═══════════════════════════════════════════════════════════════════════════

-- 1. The platform's own identifier for the thing observed.
--    Meta: adArchiveId. Instagram: the post id. Email: the message id. News: a
--    URL hash. Every collector has one; without it, "have I already recorded
--    this" is answerable only by comparing text, which is not answerable.
alter table portal.signals
  add column if not exists external_ref text;

-- 2. The collection week, set by the collector rather than generated.
--    A generated column would need date_trunc over timestamptz, which is not
--    immutable, so Postgres will not accept it. Setting it explicitly also lets
--    a backfill say which week it is filling rather than inheriting today's.
alter table portal.signals
  add column if not exists week_of date;

comment on column portal.signals.external_ref is
  'The platform''s own id for the observed item. Meta: adArchiveId. Part of the '
  'uniqueness key that makes a re-run idempotent.';

comment on column portal.signals.week_of is
  'Monday of the collection week. Set by the collector, not generated: date_trunc '
  'over timestamptz is not immutable and cannot back a generated column.';

-- 3. One row per competitor, per signal type, per platform id, per week.
--    Partial, because rows predating this migration have no external_ref and a
--    plain unique index would collapse them all into one conflict.
create unique index if not exists portal_signals_dedupe_idx
  on portal.signals (competitor_id, signal_type, external_ref, week_of)
  where external_ref is not null and week_of is not null;

create index if not exists portal_signals_week_idx
  on portal.signals (client_id, week_of desc, signal_type);


-- ─────────────────────────────────────────────────────────────────────────
-- Verify
-- ─────────────────────────────────────────────────────────────────────────
select
  (select count(*) from information_schema.columns
    where table_schema='portal' and table_name='signals'
      and column_name in ('external_ref','week_of'))            as new_columns,   -- expect 2
  (select count(*) from pg_indexes
    where schemaname='portal' and indexname='portal_signals_dedupe_idx') as dedupe_idx, -- expect 1
  (select count(*) from pg_indexes
    where schemaname='portal' and indexname='portal_signals_week_idx')   as week_idx;   -- expect 1
