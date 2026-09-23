-- ═══════════════════════════════════════════════════════════════════════════
-- portal schema, migration 006 — this pipeline owns its own collection config
--
-- REVIEW ONLY. Nothing here has been applied.
--
-- WHY THIS COLUMN IS HERE AND NOT IN public.clients.config
-- --------------------------------------------------------
-- The obvious place to put a per-competitor ad cap is the client's `config`
-- blob in `public.clients`, next to the competitor list, which is where the
-- internal pipeline reads its config from.
--
-- That is exactly the wrong place. `weekly_scout.yml` loops over EVERY row in
-- public.clients and collects for whatever competitors it finds in that blob.
-- Putting Bouldering Project's competitors there would make the internal
-- pipeline start scraping them, and then the same five pages would be pulled
-- twice a week by two different codebases.
--
-- Keeping this config in `portal` is what keeps the two pipelines independent.
-- The internal tool cannot see it, will never act on it, and this repo can
-- change a cap, add a competitor or retire a channel without a pull request
-- against the internal tool.
-- ═══════════════════════════════════════════════════════════════════════════

-- How many ads to pull for this channel per run.
--
-- NULL means "no cap, take the whole page", which is the right default for a
-- client whose briefing reports a geographic SHARE rather than a raw count.
-- The Ad Library sorts by impressions, so any cap yields the top N by spend
-- rather than a sample of the page, and a rate computed from that describes
-- the biggest campaigns rather than the market.
--
-- Set an integer only where a page is large enough that a full pull is not
-- worth what it costs, and expect `sample_method` to record
-- 'top_by_impressions' for that competitor from then on.
alter table portal.channels
  add column if not exists max_ads integer
    check (max_ads is null or max_ads > 0);

comment on column portal.channels.max_ads is
  'Per-run ad cap for this channel. NULL = census, take everything. Any integer '
  'yields the top N by impressions, never a random sample, so a rate computed '
  'from a capped pull must not be extrapolated to the page total.';

-- Onelife ran 367 active ads on 11 Sep, more than Equinox. Everything else in
-- the set was double digits. Leaving all five uncapped is roughly 550 ads a
-- week, and the geographic mix of the whole page is the entire point for this
-- client, so a census is what we want.
update portal.channels
   set max_ads = null
 where platform = 'facebook' and purpose = 'paid_ads';


-- ─────────────────────────────────────────────────────────────────────────
-- Verify
-- ─────────────────────────────────────────────────────────────────────────
select k.name, c.external_id, c.max_ads
from portal.channels c
join portal.competitors k on k.id = c.competitor_id
where c.platform = 'facebook' and c.purpose = 'paid_ads'
order by k.name;
-- Expect five rows, each with a classic page id and max_ads NULL.
-- A NULL external_id here means that competitor's ads will never be collected.
