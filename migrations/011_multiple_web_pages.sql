-- portal schema, migration 011 — more than one watched page per competitor
-- APPLIED 23 Sep 2026.
--
-- Migration 003 made portal.channels unique on (competitor_id, platform, purpose).
-- That is right for social, where a brand has one Instagram account, and wrong for
-- web: every competitor needs at least two pages watched, their DC location page and
-- their membership/pricing page, and those answer different questions.
--
-- The pricing pages are the higher-value target and none were seeded. A competitor
-- changing price during Bouldering Project's pre-opening window is the single most
-- actionable thing this collector can catch, and until now there was nowhere to put
-- the URL.

drop index if exists portal.portal_channels_unique_idx;

create unique index portal_channels_unique_idx
  on portal.channels (competitor_id, platform, purpose, coalesce(url, ''))
  nulls not distinct;

comment on index portal.portal_channels_unique_idx is
  'URL is part of the key so a competitor can have several watched pages for one '
  'purpose. Social channels leave url null and still collapse to one row per '
  'platform+purpose, which is what migration 003 intended.';

insert into portal.channels (competitor_id, platform, purpose, scope, url, location_label, notes)
select k.id, 'web', 'web_change', v.scope, v.url, v.label, v.notes
from portal.competitors k
join (values
  ('Movement',  'national', 'https://movementgyms.com/memberships-passes/',   'Pricing (national)',
   'Movement prices by home gym. A change here MAY NOT APPLY in DC and must be reported with that caveat, or Kyle acts on a price that is not real in his market.'),
  ('Sportrock', 'regional', 'https://sportrock.com/membership',                'Pricing (all sites)',
   'Flat $90/month across all four DMV locations, so a change here does apply locally.'),
  ('VIDA',      'regional', 'https://vidafitness.com/membership/options/',     'Pricing (all clubs)',
   'Six DMV clubs, all corporate-run. Single-market brand, so this page is local by definition.'),
  ('Onelife',   'regional', 'https://onelifefitness.com/gyms-washington-dc',   'DC gyms index',
   'Lists their DC-area clubs. A new location appearing here is a material event.'),
  ('YMCA',      'regional', 'https://www.ymcadc.org/membership/membership-at-the-ymca/', 'Membership',
   'Single-market brand. This is the page their paid search sends traffic to.')
) as v(name, scope, url, label, notes) on v.name = k.name
where k.active
on conflict do nothing;
