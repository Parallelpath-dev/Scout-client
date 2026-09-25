-- 016: the client as a benchmark, collected like a competitor, never scored as one.
--
-- is_client marks the row that is the client itself. Collectors pick it up like any
-- other brand. Everything competitive leaves it out: the pressure score, the market
-- score, the set comparisons, the Analyst's digest and every feed. It is scored on
-- its own, against its own history and against the competitive set, and shows only
-- where the page labels it "you" (the channel radar and the amenity map).
--
-- Its own posts can name its other DC location, which is kept out of writing, so no
-- client-row text reaches the model or the screen.

alter table portal.competitors
  add column if not exists is_client boolean not null default false;

create unique index if not exists portal_competitors_one_client_row
  on portal.competitors (client_id) where is_client;

comment on column portal.competitors.is_client is
  'The client itself, collected as a benchmark. Excluded from pressure, the market score, set comparisons, the digest and feeds.';
