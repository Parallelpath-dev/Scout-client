-- 015: per-channel pressure scores.
--
-- Every channel (paid, search, website, email, organic social) is scored from its
-- first collected week. Until a competitor has four weeks of its own history on a
-- channel, the channel is compared with the rest of the set that week; the weight
-- moves to the competitor's own normal as its history builds.
--
--   components  {"paid": {"score": 81, "basis": "set"}, "social": {...}, ...}
--               basis: own | set | blend | not collected | no comparison
--   basis       per metric: {"ads_launched": "set", "social_posts": "own", ...}
--
-- Additive and nullable-by-default: existing rows and readers are unaffected.

alter table portal.pressure_weekly
  add column if not exists components jsonb not null default '{}'::jsonb,
  add column if not exists basis      jsonb not null default '{}'::jsonb;

comment on column portal.pressure_weekly.components is
  'Per-channel 0-100 score and its basis (own | set | blend | not collected | no comparison).';
comment on column portal.pressure_weekly.basis is
  'Per metric: what the z was measured against. own = own history, set = rest of the set this week.';
