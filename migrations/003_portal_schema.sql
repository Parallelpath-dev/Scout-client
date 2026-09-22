-- ═══════════════════════════════════════════════════════════════════════════
-- Scout Client Portal — `portal` schema, migration 001
--
-- REVIEW ONLY. Nothing here has been applied.
--
-- Creates a separate schema for the client-facing build. `public` is untouched:
-- no new columns, no altered constraints, no dropped anything. The internal
-- tool keeps working exactly as it does today.
--
-- Two ideas carry the whole design:
--
--   SOURCE SCOPE  — where a signal came from. Movement's national ad page.
--   GEO RELEVANCE — who the content is about. An ad naming Crystal City.
--
-- They are independent. A national source publishes local content all the time,
-- and that case is the interesting one. One field cannot represent both, which
-- is why there are two.
-- ═══════════════════════════════════════════════════════════════════════════

create schema if not exists portal;


-- ─────────────────────────────────────────────────────────────────────────
-- 1. Enumerated vocabularies
-- ─────────────────────────────────────────────────────────────────────────
-- Text plus a check constraint rather than Postgres enums: adding a value to
-- an enum in a migration is a lock; adding one to a check is a one-line alter.

create domain portal.scope as text
  check (value in ('local','regional','national'));

create domain portal.geo_relevance as text
  check (value in (
    'dc_landing',    -- links to a DC location page. Strongest available signal.
    'dc_explicit',   -- names DC, Columbia Heights, Tenleytown, U Street, etc.
    'regional',      -- names DMV, Northern Virginia, Maryland
    'other_market',  -- names a market that is NOT DC. Informative in its own right.
    'none'           -- no geographic reference. NOT evidence of non-DC delivery.
  ));


-- ─────────────────────────────────────────────────────────────────────────
-- 2. Competitors (portal's own, decoupled from public.competitors)
-- ─────────────────────────────────────────────────────────────────────────
create table portal.competitors (
  id              uuid primary key default gen_random_uuid(),
  client_id       uuid not null,              -- references public.clients(id) logically
  name            text not null,              -- brand name: "Movement"
  domain          text,
  market          text,                       -- 'DC'
  monitored_site  text,                       -- "Crystal City"
  monitored_addr  text,
  display_local   text,                       -- "Movement Crystal City"
  display_national text,                      -- "Movement (national brand)"
  active          boolean not null default true,
  notes           text,
  created_at      timestamptz default now()
);

create unique index portal_competitors_client_name_idx
  on portal.competitors (client_id, lower(name));

-- display_local and display_national are not decoration. The labelling rule is
-- that a brand name never appears bare next to a paid figure, and storing both
-- strings means the reporting layer cannot forget.


-- ─────────────────────────────────────────────────────────────────────────
-- 3. Channels — one row per competitor × platform × purpose
-- ─────────────────────────────────────────────────────────────────────────
-- Replaces "one social handle set per competitor". Scope varies by platform AND
-- by brand: Movement's Instagram is regional DMV, its Facebook organic is local
-- Crystal City, its ads are national. Two Facebook columns would fix Facebook
-- and leave the identical defect on Instagram.

create table portal.channels (
  id             uuid primary key default gen_random_uuid(),
  competitor_id  uuid not null references portal.competitors(id) on delete cascade,

  platform       text not null
                 check (platform in ('facebook','instagram','tiktok','youtube',
                                     'x','web','email','news')),
  purpose        text not null
                 check (purpose in ('organic_social','paid_ads','web_change',
                                    'email','news')),
  scope          portal.scope not null,

  handle         text,          -- vanity/slug/username
  external_id    text,          -- CLASSIC Facebook page id, YouTube channel id
  url            text,
  location_label text,          -- 'Crystal City', 'Anthony Bowen'

  -- Email sender matching. VIDA's campaigns send from a UA Companies domain,
  -- not vidafitness.com. Holding that as data rather than a hardcoded exception
  -- is the difference between a config line and a silent six-week failure.
  sender_domains text[],

  active         boolean not null default true,
  notes          text,
  created_at     timestamptz default now()
);

create unique index portal_channels_unique_idx
  on portal.channels (competitor_id, platform, purpose);
create index portal_channels_competitor_idx on portal.channels (competitor_id);

comment on column portal.channels.external_id is
  'Facebook: the CLASSIC page id. Profile-style ids beginning 1000... fail '
  'SILENTLY in the Ad Library. Re-derive by opening the Ad Library, searching '
  'the page, and reading view_all_page_id from the resulting URL.';


-- ─────────────────────────────────────────────────────────────────────────
-- 4. Signals
-- ─────────────────────────────────────────────────────────────────────────
-- scope is denormalised off the channel on purpose. The reporting layer must
-- never need a join to know how to label a number.

create table portal.signals (
  id             uuid primary key default gen_random_uuid(),
  client_id      uuid not null,
  competitor_id  uuid not null references portal.competitors(id) on delete cascade,
  channel_id     uuid references portal.channels(id) on delete set null,

  source_scope   portal.scope not null,
  geo_relevance  portal.geo_relevance not null default 'none',
  geo_evidence   text,          -- the phrase or URL that triggered the classification

  signal_type    text not null, -- ad_active, post, web_change, email_campaign, news_item
  data           jsonb not null default '{}',
  source_url     text,
  observed_at    timestamptz default now(),
  collected_at   timestamptz default now()
);

create index portal_signals_client_idx    on portal.signals (client_id, collected_at desc);
create index portal_signals_competitor_idx on portal.signals (competitor_id, collected_at desc);
create index portal_signals_geo_idx        on portal.signals (client_id, geo_relevance, collected_at desc);

comment on column portal.signals.geo_relevance is
  'What the CONTENT references, not where it was delivered. The Meta Ad Library '
  'exposes no targeting, audience location, DMA or delivery region for commercial '
  'ads anywhere. A dc_explicit count is a FLOOR on local activity, never a count '
  'of ads targeted at DC, and "none" is not evidence an ad is not running here.';


-- ─────────────────────────────────────────────────────────────────────────
-- 5. Weekly DC-referencing ad rollup
-- ─────────────────────────────────────────────────────────────────────────
-- The number Kyle actually reads, and the trend underneath it. Stored rather
-- than computed at read time so the week-over-week series survives ads rotating
-- out of the library.

create table portal.ad_geo_weekly (
  id              uuid primary key default gen_random_uuid(),
  client_id       uuid not null,
  competitor_id   uuid not null references portal.competitors(id) on delete cascade,
  week_of         date not null,

  total_active    integer not null,   -- every active ad on the national page
  dc_landing      integer not null default 0,
  dc_explicit     integer not null default 0,
  regional        integer not null default 0,
  other_market    integer not null default 0,
  no_geo          integer not null default 0,

  -- dc_landing + dc_explicit. The reported figure, always as a floor.
  dc_referencing  integer generated always as (dc_landing + dc_explicit) stored,

  collected_at    timestamptz default now()
);

create unique index portal_ad_geo_weekly_idx
  on portal.ad_geo_weekly (competitor_id, week_of);

-- Deliberately absent: any per-market average. Dividing 367 national ads by 7
-- locations produces a number with no basis in anything observable. There is no
-- column for it so nobody can populate one.


-- ─────────────────────────────────────────────────────────────────────────
-- 6. Briefings
-- ─────────────────────────────────────────────────────────────────────────
create table portal.briefings (
  id             uuid primary key default gen_random_uuid(),
  client_id      uuid not null,
  week_of        date not null,
  pressure_score integer check (pressure_score between 0 and 100),
  summary        text,
  developments   jsonb default '[]',
  full_report    jsonb default '{}',
  published_at   timestamptz default now(),   -- NULL = held, invisible to client
  created_at     timestamptz default now()
);

create unique index portal_briefings_client_week_idx
  on portal.briefings (client_id, week_of);
create index portal_briefings_published_idx
  on portal.briefings (client_id, published_at, week_of desc);


-- ─────────────────────────────────────────────────────────────────────────
-- 7. Row level security
-- ─────────────────────────────────────────────────────────────────────────
-- Membership still comes from public.client_users, which already works and is
-- already bound to auth.users by the existing trigger. No reason to duplicate it.

alter table portal.competitors   enable row level security;
alter table portal.channels      enable row level security;
alter table portal.signals       enable row level security;
alter table portal.ad_geo_weekly enable row level security;
alter table portal.briefings     enable row level security;

grant usage on schema portal to authenticated;
grant select on all tables in schema portal to authenticated;
-- anon gets nothing. Not a policy that could be mis-scoped — no grant at all.

create policy competitors_own_read on portal.competitors
  for select to authenticated using (
    exists (select 1 from public.client_users cu
             where cu.auth_user_id = auth.uid()
               and cu.client_id = portal.competitors.client_id
               and cu.active = true));

create policy channels_own_read on portal.channels
  for select to authenticated using (
    exists (select 1 from portal.competitors k
             join public.client_users cu on cu.client_id = k.client_id
            where k.id = portal.channels.competitor_id
              and cu.auth_user_id = auth.uid()
              and cu.active = true));

create policy signals_own_read on portal.signals
  for select to authenticated using (
    exists (select 1 from public.client_users cu
             where cu.auth_user_id = auth.uid()
               and cu.client_id = portal.signals.client_id
               and cu.active = true));

create policy ad_geo_own_read on portal.ad_geo_weekly
  for select to authenticated using (
    exists (select 1 from public.client_users cu
             where cu.auth_user_id = auth.uid()
               and cu.client_id = portal.ad_geo_weekly.client_id
               and cu.active = true));

create policy briefings_own_read on portal.briefings
  for select to authenticated using (
    published_at is not null
    and exists (select 1 from public.client_users cu
                 where cu.auth_user_id = auth.uid()
                   and cu.client_id = portal.briefings.client_id
                   and cu.active = true));


-- ─────────────────────────────────────────────────────────────────────────
-- 8. Verify
-- ─────────────────────────────────────────────────────────────────────────
select table_name,
       (select count(*) from pg_policies p
         where p.schemaname = 'portal' and p.tablename = t.table_name) as policies
from information_schema.tables t
where table_schema = 'portal'
order by table_name;
-- Expect 5 tables, 1 policy each. Any table showing 0 is readable by nobody
-- (RLS on, no policy = deny all) — safe, but broken. Any table missing from
-- the list did not create.
