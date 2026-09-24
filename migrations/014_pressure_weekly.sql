-- portal schema, migration 014 — the pressure history calibration reads
--
-- The pressure score is calibrated against each competitor's own last 12 weeks
-- (pipeline/calibrate.py, pipeline/momentum.py). That needs the counted metrics for
-- every past week, per competitor and for the market, kept somewhere that does not
-- depend on a briefing having been published. A held week still counts as history.
--
-- competitor_id NULL is the market row. NULLS NOT DISTINCT so it dedupes like the
-- competitor rows and a re-run of the same week updates in place (same pattern as 010).
-- Not partial: PostgREST ON CONFLICT cannot match a predicated index (42P10).

create table if not exists portal.pressure_weekly (
  id             uuid primary key default gen_random_uuid(),
  client_id      uuid not null,
  competitor_id  uuid references portal.competitors(id) on delete cascade,
  week_of        date not null,
  metrics        jsonb not null default '{}',   -- counted, geo-weighted, pre-calibration
  metric_z       jsonb not null default '{}',
  events         jsonb not null default '[]',
  event_points   integer not null default 0,
  score          integer check (score between 0 and 100),   -- NULL while calibrating
  status         text not null check (status in ('calibrating', 'scored')),
  trend          text check (trend in ('hotter', 'cooler', 'steady')),
  method         text not null default 'momentum-v1',
  created_at     timestamptz default now()
);

create unique index if not exists portal_pressure_weekly_idx
  on portal.pressure_weekly (client_id, competitor_id, week_of) nulls not distinct;

alter table portal.pressure_weekly enable row level security;

-- Same read rule as briefings: a signed-in user sees their own client's rows. The
-- page does not read this table yet; the policy exists so it never has to be added
-- in a hurry. anon gets nothing, as everywhere in this schema.
drop policy if exists pressure_weekly_own_read on portal.pressure_weekly;
create policy pressure_weekly_own_read on portal.pressure_weekly
  for select to authenticated
  using (exists (select 1 from public.client_users cu
                  where cu.auth_user_id = auth.uid()
                    and cu.client_id = portal.pressure_weekly.client_id
                    and cu.active = true));

grant all privileges on portal.pressure_weekly to service_role;
grant select on portal.pressure_weekly to authenticated;
revoke all on portal.pressure_weekly from anon;

-- Watch terms: names whose appearance in any competitor signal is a step-change event
-- worth fixed points (momentum.EVENT_POINTS["watch_term"]). Config, not code.
--
-- never_in_writing: terms that hold a briefing if they appear in anything client-
-- readable (validate_briefing.py). The client brain says the Columbia Heights /
-- Eckington overlap is raised on a call and never in writing. A pattern for
-- "cannibalise" misses "draw members away from Eckington", so the name itself is
-- the rule: a competitive briefing has no need to mention the client's own gym.
update public.clients
   set config = coalesce(config, '{}'::jsonb)
              || jsonb_build_object(
                   'watch_terms',
                   jsonb_build_array('columbia heights', 'dc usa', '14th st nw', '3100 14th'),
                   'never_in_writing',
                   jsonb_build_array('eckington'))
 where slug = 'bouldering-project';

select slug, config->'watch_terms', config->'never_in_writing'
  from public.clients where slug = 'bouldering-project';
