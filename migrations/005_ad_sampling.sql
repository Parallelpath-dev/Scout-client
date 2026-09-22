-- ═══════════════════════════════════════════════════════════════════════════
-- portal schema, migration 003 — separate the library total from the sample
--
-- REVIEW ONLY. Nothing here has been applied. portal.ad_geo_weekly is empty.
--
-- WHY
-- ---
-- The internal Meta collector caps every page at 35 ads. Verified 22 Sep 2026
-- against the last run: `total_active_ads` reads 35 for all seven competitors
-- while `total_available_ads` reads 131, 464, 1029, 96, 318, 422 and 72. The
-- field named "total active" is the SAMPLE SIZE, not the total.
--
-- For Bouldering Project that gap is the whole story. Onelife runs 367 ads. A
-- 35-ad sample is 9.5% of them, and because the pull is sorted by impressions
-- it is the top 9.5% by spend rather than a random draw.
--
-- Migration 001 gave ad_geo_weekly a single `total_active` column, which quietly
-- assumed the tier counts sum to it. Under a cap they do not, and a dashboard
-- reading 8 dc_referencing out of 367 would be wrong by a factor of ten while
-- looking entirely reasonable.
--
-- Three columns make the denominator explicit and impossible to confuse.
-- ═══════════════════════════════════════════════════════════════════════════

-- The library's own reported total for the page. The honest headline number.
alter table portal.ad_geo_weekly
  add column if not exists total_available integer;

-- How many ads were actually pulled and classified. The tier counts sum to THIS.
alter table portal.ad_geo_weekly
  add column if not exists ads_sampled integer;

-- How the sample was drawn, so nobody has to guess later whether it was random.
alter table portal.ad_geo_weekly
  add column if not exists sample_method text
    check (sample_method in ('census', 'top_by_impressions', 'unknown'));

comment on column portal.ad_geo_weekly.total_available is
  'The Ad Library''s own reported total active ads for the page. The headline number.';

comment on column portal.ad_geo_weekly.ads_sampled is
  'How many ads were pulled and classified. dc_landing + dc_explicit + regional + '
  'other_market + no_geo sums to THIS, not to total_available.';

comment on column portal.ad_geo_weekly.sample_method is
  'census = every active ad was pulled, so ads_sampled = total_available and the '
  'tier counts are complete. top_by_impressions = a capped pull sorted by '
  'impressions, which is biased toward the highest-spend creative and is NOT a '
  'random sample. A rate computed from a top_by_impressions sample must never be '
  'extrapolated to total_available.';

comment on column portal.ad_geo_weekly.total_active is
  'DEPRECATED, kept only so nothing referencing it breaks. Use total_available for '
  'the page total and ads_sampled for the classified denominator. The name is '
  'ambiguous in exactly the way that produced this migration.';

comment on column portal.ad_geo_weekly.dc_referencing is
  'dc_landing + dc_explicit, out of ads_sampled. A FLOOR on local activity and never '
  'a count of ads targeted at DC: the Meta Ad Library exposes no targeting, audience '
  'location, DMA or delivery region for commercial ads anywhere.';


-- ─────────────────────────────────────────────────────────────────────────
-- Verify
-- ─────────────────────────────────────────────────────────────────────────
select column_name, data_type
from information_schema.columns
where table_schema = 'portal' and table_name = 'ad_geo_weekly'
  and column_name in ('total_available', 'ads_sampled', 'sample_method')
order by column_name;
-- Expect 3 rows.
