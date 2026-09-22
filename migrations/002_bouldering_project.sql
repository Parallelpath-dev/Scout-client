-- ═══════════════════════════════════════════════════════════════════════════
-- Scout Client Portal — Bouldering Project tenant
-- Migration 002. Run after 001.
--
-- Everything client-specific lives here as data. No code in the portal knows
-- the words "Bouldering Project" — which is the test for whether client #2
-- is a row or a rewrite.
--
-- Competitors are deliberately absent. They arrive from the client (or from
-- us, if they'd rather we propose the DC set). Until a competitor row exists,
-- the portal shows "Waiting on you" against step 03 and collection stays idle.
-- ═══════════════════════════════════════════════════════════════════════════

insert into public.clients (name, slug, config, theme, brain,
                            output_profile, call_sweep_enabled, portal_enabled)
values (
  'Bouldering Project',
  'bouldering-project',
  jsonb_build_object(
    'domain',            'boulderingproject.com',
    'vertical',          'fitness',
    'markets',           jsonb_build_array('DC'),
    'primary_market',    'DC',
    -- Null on purpose. The portal reads "Collection starts once you send the
    -- competitor list" until a date exists, so there is no wrong date on
    -- screen. Set it the day competitors land:
    --   update public.clients
    --      set config = jsonb_set(config,'{first_briefing_at}','"2026-09-07"')
    --    where slug = 'bouldering-project';
    'first_briefing_at', null
  ),
  -- Measured off boulderingproject.com. See bouldering-project-theme.json
  -- for the provenance of every value, including the four that are derived
  -- and the three substitutions (typeface, casing, logo hosting).
  jsonb_build_object(
    'ground',      '#3D3935',
    'surface',     '#4A4540',
    'sunk',        '#332F2C',
    'ink',         '#FFFFFF',
    'muted',       '#97A2BA',
    'line',        'rgba(255,255,255,0.13)',
    'birch',       '#97A2BA',
    'accent',      '#D65F52',
    'accentSoft',  'rgba(214,95,82,0.14)',
    'ok',          '#97A2BA',
    'wait',        '#D65F52',
    'fontDisplay', '''Archivo'', ''Good Pro'', system-ui, sans-serif',
    'fontBody',    '''Archivo'', ''Good Pro'', system-ui, sans-serif',
    'fontMono',    '''Archivo'', system-ui, sans-serif',
    'logoUrl',     'assets/bp-logo.jpg'
  ),
  -- Placeholder. Replace with their actual goals and obstacles before the
  -- first synthesis run: the Strategist reads this field directly, and a
  -- placeholder here produces generic recommendations.
  'AWAITING CLIENT CONTEXT. Do not run synthesis until this is replaced.',
  'executive',   -- word-capped output for a CEO and a head of revenue
  false,         -- no Fathom call sweep: they are a prospect, not a client
  true           -- portal access on
)
on conflict (slug) do update set
  config             = excluded.config,
  theme              = excluded.theme,
  output_profile     = excluded.output_profile,
  call_sweep_enabled = excluded.call_sweep_enabled,
  portal_enabled     = excluded.portal_enabled;


-- ─────────────────────────────────────────────────────────────────────────
-- Authorized users
-- ─────────────────────────────────────────────────────────────────────────
-- Adding a row here does NOT create an account or send anything. It authorizes
-- an address. The account gets created the first time that person requests a
-- sign-in link, and the trigger from migration 001 binds the two together.
--
-- Anyone not listed here can request a link, receive it, sign in, and see
-- nothing at all. That's the intended behavior for a stranger.

-- ⚠ REAL ADDRESSES DO NOT GO IN THIS FILE.
-- This repo is public (GitHub Pages requires it), so anything committed here
-- is served on the open internet. Client contact details are personal data.
-- Run the real insert in the Supabase SQL editor instead — see
-- authorize-users.sql, which is delivered outside the repo and never committed.

insert into public.client_users (client_id, email, full_name, role)
select c.id, u.email, u.full_name, 'client'
from public.clients c
cross join (values
  ('first.last@example.com',  'First Last'),
  ('other.person@example.com','Other Person')
) as u(email, full_name)
where c.slug = 'bouldering-project'
  and false   -- placeholder rows never insert; remove this line only if you
              -- are running a copy of this file OUTSIDE the repo
on conflict do nothing;


-- ─────────────────────────────────────────────────────────────────────────
-- Verify
-- ─────────────────────────────────────────────────────────────────────────
-- Expect: one client, two users, zero competitors.
select c.name, c.slug, c.output_profile, c.call_sweep_enabled, c.portal_enabled,
       c.config->>'first_briefing_at' as first_briefing,
       (select count(*) from public.client_users cu where cu.client_id = c.id) as users,
       (select count(*) from public.competitors k where k.client_id = c.id)   as competitors
from public.clients c
where c.slug = 'bouldering-project';
