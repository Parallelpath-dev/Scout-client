-- ═══════════════════════════════════════════════════════════════════════════
-- Scout Client Portal — Tenant Isolation
-- Migration 001
--
-- REVIEW BEFORE APPLYING. This runs against production (6 live clients,
-- 3,346 signals, 93 briefings).
--
-- Safety notes:
--   * Every statement is ADDITIVE. Nothing is dropped, altered destructively,
--     or renamed.
--   * RLS is already enabled on all tables with ZERO policies, which means
--     the current effective state is deny-all for anon/authenticated. Adding
--     policies can only widen access for logged-in client users; it cannot
--     narrow anything that works today.
--   * The collectors, synthesizer, and email digest all connect with the
--     SERVICE ROLE key, which bypasses RLS entirely. This migration cannot
--     break the Monday pipeline.
--   * Section 7 (revoking anonymous execute on apply_brain_proposal) is the
--     ONLY statement that removes an existing capability. It is separated
--     deliberately — confirm nothing calls that function with the anon key
--     before running it.
-- ═══════════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────────────────
-- 1. Client configuration columns
-- ─────────────────────────────────────────────────────────────────────────
-- theme            → brand tokens (colors, fonts, logo) read by the portal.
--                    This is what makes client #2 a database row, not a fork.
-- output_profile   → 'executive' (word-capped, CEO-facing) or 'operator'
--                    (full internal detail). Read by the synthesizer prompts.
-- call_sweep_enabled → when false, the Fathom brain sweep skips this client.
--                    Bouldering Project is a prospect; call content must never
--                    flow into their strategic context.
-- portal_enabled   → whether this client has a client-facing portal at all.
--                    Existing internal-only clients stay false.

alter table public.clients
  add column if not exists theme              jsonb   default '{}'::jsonb,
  add column if not exists output_profile     text    default 'operator',
  add column if not exists call_sweep_enabled boolean default true,
  add column if not exists portal_enabled     boolean default false;

alter table public.clients
  drop constraint if exists clients_output_profile_check;
alter table public.clients
  add constraint clients_output_profile_check
  check (output_profile in ('executive', 'operator'));


-- ─────────────────────────────────────────────────────────────────────────
-- 2. Market dimension on competitors
-- ─────────────────────────────────────────────────────────────────────────
-- Bouldering Project runs 12 gyms across 9 metros. The beta covers DC only,
-- but the column goes in now because retrofitting a dimension after signals
-- have accumulated against it is the expensive version of this change.
-- NULL = national / not market-specific.

alter table public.competitors
  add column if not exists market text;

create index if not exists competitors_market_idx
  on public.competitors(client_id, market);


-- ─────────────────────────────────────────────────────────────────────────
-- 3. Publication control on briefings
-- ─────────────────────────────────────────────────────────────────────────
-- Default is now() so briefings auto-publish exactly as decided — no human
-- gate, no extra step in the Monday run. Setting published_at to NULL is the
-- kill switch: the row stays, the client stops seeing it, immediately.
-- Backfilled to created_at so all 93 existing briefings remain visible.

alter table public.briefings
  add column if not exists published_at timestamptz default now();

update public.briefings
  set published_at = created_at
  where published_at is null;


-- ─────────────────────────────────────────────────────────────────────────
-- 4. Client user mapping
-- ─────────────────────────────────────────────────────────────────────────
-- The single source of truth for "who may see which client."
-- A person with no row here can log in successfully and see nothing at all,
-- which is the correct failure mode: a stranger who signs up gets an empty
-- account, not an error that confirms the system exists.

create table if not exists public.client_users (
  id            uuid primary key default gen_random_uuid(),
  client_id     uuid not null references public.clients(id) on delete cascade,
  email         text not null,
  auth_user_id  uuid unique references auth.users(id) on delete set null,
  full_name     text,
  role          text not null default 'client',
  active        boolean not null default true,
  created_at    timestamptz default now(),
  last_seen_at  timestamptz
);

create unique index if not exists client_users_email_client_idx
  on public.client_users (lower(email), client_id);
create index if not exists client_users_auth_user_idx
  on public.client_users (auth_user_id);

alter table public.client_users enable row level security;


-- ─────────────────────────────────────────────────────────────────────────
-- 5. Bind Supabase Auth accounts to client_users on signup
-- ─────────────────────────────────────────────────────────────────────────
-- Magic-link login creates an auth.users row on first use. This trigger
-- matches that new account to a pre-authorized email and links it.
-- If the email was never authorized, nothing is linked and the account
-- remains empty — no access is granted by merely signing up.

create or replace function public.link_auth_user_to_client()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  update public.client_users
     set auth_user_id = new.id
   where lower(email) = lower(new.email)
     and auth_user_id is null
     and active = true;
  return new;
end;
$$;

revoke execute on function public.link_auth_user_to_client() from anon, authenticated;

drop trigger if exists on_auth_user_created_link_client on auth.users;
create trigger on_auth_user_created_link_client
  after insert on auth.users
  for each row execute function public.link_auth_user_to_client();


-- ─────────────────────────────────────────────────────────────────────────
-- 6. Row level security policies
-- ─────────────────────────────────────────────────────────────────────────
-- Read-only, scoped to the caller's own client, for logged-in users only.
-- No policy is granted to the `anon` role anywhere, so an unauthenticated
-- request with the public key returns zero rows from every table.
--
-- Each policy re-derives the caller's client from client_users rather than
-- trusting anything supplied by the browser. There is no code path where a
-- URL, header, or request body can influence which client_id is returned.

-- 6a. A user sees only their own membership row.
drop policy if exists client_users_self_read on public.client_users;
create policy client_users_self_read
  on public.client_users for select to authenticated
  using (auth_user_id = auth.uid());

-- 6b. A user sees only their own client record.
drop policy if exists clients_own_read on public.clients;
create policy clients_own_read
  on public.clients for select to authenticated
  using (
    portal_enabled = true
    and exists (
      select 1 from public.client_users cu
       where cu.auth_user_id = auth.uid()
         and cu.client_id    = clients.id
         and cu.active       = true
    )
  );

-- 6c. Competitors, scoped to the caller's client.
drop policy if exists competitors_own_read on public.competitors;
create policy competitors_own_read
  on public.competitors for select to authenticated
  using (
    exists (
      select 1 from public.client_users cu
       where cu.auth_user_id = auth.uid()
         and cu.client_id    = competitors.client_id
         and cu.active       = true
    )
  );

-- 6d. Signals, scoped to the caller's client.
drop policy if exists signals_own_read on public.signals;
create policy signals_own_read
  on public.signals for select to authenticated
  using (
    exists (
      select 1 from public.client_users cu
       where cu.auth_user_id = auth.uid()
         and cu.client_id    = signals.client_id
         and cu.active       = true
    )
  );

-- 6e. Briefings — scoped to the caller's client AND published.
--     An unpublished briefing is invisible to the client but still present
--     for Parallel Path via the service key.
drop policy if exists briefings_own_read on public.briefings;
create policy briefings_own_read
  on public.briefings for select to authenticated
  using (
    published_at is not null
    and exists (
      select 1 from public.client_users cu
       where cu.auth_user_id = auth.uid()
         and cu.client_id    = briefings.client_id
         and cu.active       = true
    )
  );

-- NOTE: competitor_emails, brain_history, and brain_sweep_proposals get NO
-- client-facing policy. They stay deny-all for logged-in users. Competitor
-- email captures and internal strategic-context history are not client-facing
-- material, and the safest way to keep it that way is to grant nothing.


-- ═══════════════════════════════════════════════════════════════════════════
-- 7. SEPARATE — run only after confirming nothing calls this anonymously
-- ═══════════════════════════════════════════════════════════════════════════
-- The security linter flags public.apply_brain_proposal(uuid) as executable
-- by the `anon` role over the public REST API as a SECURITY DEFINER function.
-- Anyone on the internet can call it and write to client strategic context.
--
-- Check first: does any internal tool call this with the anon (public) key
-- rather than the service key? If it's only ever called from a server-side
-- job or the Supabase dashboard, this is safe to run.
--
--   revoke execute on function public.apply_brain_proposal(uuid) from anon;
--   revoke execute on function public.apply_brain_proposal(uuid) from authenticated;
--
-- Also outstanding from the linter, not addressed here:
--   * public.significant_web_changes            — SECURITY DEFINER view
--   * public.brs_connector_benchmarks_by_industry — SECURITY DEFINER view
--   * public.brs_pulse_leads_with_scores        — SECURITY DEFINER view
--   * public.apply_brain_proposal               — mutable search_path
-- ═══════════════════════════════════════════════════════════════════════════
