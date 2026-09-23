-- ═══════════════════════════════════════════════════════════════════════════
-- portal schema, migration 009 — mark competitors that only operate here
--
-- APPLIED 23 Sep 2026.
--
-- The first real collection run produced "VIDA: 0 of 21 ads reference DC".
-- That reads as VIDA not advertising in this market. VIDA advertises ONLY in
-- this market: six clubs, all DMV. Same for the YMCA of Metropolitan
-- Washington, and for Sportrock, whose four gyms are all inside the DMV.
--
-- For those brands the geographic ratio answers a question that does not
-- apply, and answering it anyway produces a number that is precisely
-- backwards. geo_relevance only discriminates for a brand that advertises
-- somewhere else too, which here is Movement (Baltimore, Colorado) and
-- Onelife (Georgia).
-- ═══════════════════════════════════════════════════════════════════════════

alter table portal.competitors
  add column if not exists single_market boolean not null default false;

comment on column portal.competitors.single_market is
  'True when every location this brand operates is inside the monitored market, '
  'so every ad they run is in-market by definition regardless of what the copy '
  'says. Reporting must NOT show a dc_referencing ratio for these brands: a low '
  'ratio means their creative is placeless, never that they are absent locally.';

update portal.competitors set single_market = true
 where lower(name) in ('vida', 'ymca', 'sportrock');

update portal.competitors set single_market = false
 where lower(name) in ('movement', 'onelife');

-- Verify
select name, domain, single_market from portal.competitors
order by single_market desc, name;
-- Expect Sportrock, VIDA, YMCA true; Movement, Onelife false.
