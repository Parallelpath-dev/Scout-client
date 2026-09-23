-- ═══════════════════════════════════════════════════════════════════════════
-- portal schema, migration 007 — grants for the service role
--
-- APPLIED 23 Sep 2026.
--
-- Migration 003 granted `portal` to `authenticated` and nothing else, on the
-- assumption that the service role reaches everything. It does not.
--
-- The service role BYPASSES ROW LEVEL SECURITY. That is a different thing from
-- having privileges. It still needs USAGE on the schema and grants on the
-- tables like any other role, and it only has those on `public` because
-- Supabase sets them up there by default. A brand new schema starts with
-- nothing, so the collector got a flat 403 on its first real run.
-- ═══════════════════════════════════════════════════════════════════════════

grant usage on schema portal to service_role;
grant all privileges on all tables in schema portal to service_role;
grant all privileges on all sequences in schema portal to service_role;

-- So migration 009 does not rediscover this.
alter default privileges in schema portal
  grant all privileges on tables to service_role;
alter default privileges in schema portal
  grant all privileges on sequences to service_role;

-- `authenticated` keeps read-only, which is what a signed-in client should
-- have. The portal never writes.
alter default privileges in schema portal
  grant select on tables to authenticated;

-- anon still gets nothing, deliberately. Not a policy that could be
-- mis-scoped, no grant in the first place.
revoke all on all tables in schema portal from anon;
revoke usage on schema portal from anon;

-- Verify
select grantee, string_agg(distinct privilege_type, ',' order by privilege_type) as privs
from information_schema.role_table_grants
where table_schema='portal' and grantee in ('anon','authenticated','service_role')
group by grantee order by grantee;
-- Expect: authenticated SELECT, service_role everything, anon absent entirely.
