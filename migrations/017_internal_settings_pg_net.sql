-- 017: server-only settings, and pg_net so SQL can call our own edge functions.
--
-- internal_settings holds values only the service role reads (RLS on, no policies).
-- First use: the token that authorises portal-inbound-email's backfill route. Its value
-- is set by hand in the database and never committed.

create table if not exists portal.internal_settings (
  key text primary key,
  value text not null,
  updated_at timestamptz not null default now()
);
alter table portal.internal_settings enable row level security;
revoke all on portal.internal_settings from anon, authenticated;

create extension if not exists pg_net;
