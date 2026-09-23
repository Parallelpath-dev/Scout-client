-- portal schema, migration 012 — which brands price by location
-- APPLIED 23 Sep 2026.
--
-- Movement sets membership price per home gym, so a change on their national
-- memberships page may not be true in DC. The web change collector needs to know that
-- to attach the caveat, and the alternative was matching a phrase inside a free-text
-- notes column, which works until somebody rewords the note.
--
-- Reporting a national price as a local one is the specific failure this guards
-- against: a price the client acts on that is not real in their market is worse than
-- no price at all.

alter table portal.competitors
  add column if not exists prices_by_location boolean not null default false;

comment on column portal.competitors.prices_by_location is
  'True when membership price varies by site, so a change on a national pricing page '
  'cannot be assumed to apply in the monitored market. Forces the web change collector '
  'to emit applies_locally=unknown with a caveat.';

update portal.competitors set prices_by_location = true  where lower(name) = 'movement';
update portal.competitors set prices_by_location = false where lower(name) <> 'movement';
