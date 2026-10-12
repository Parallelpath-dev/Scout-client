-- Monday runs are triggered from Supabase, not from GitHub's cron.
--
-- GitHub's `schedule:` trigger is best effort, and for this repo it stopped firing:
-- Portal weekly did not start on its own on 28 Sep, 5 Oct or 12 Oct, while every
-- manual run worked. A manual run and an API dispatch are the same event
-- (workflow_dispatch), so Supabase now sends that event on the clock, and checks
-- two hours later that the work actually landed.
--
-- The GitHub token lives in Vault as 'github_actions_token' (a fine-grained token,
-- this repo only, Actions: read and write). It is never in the repo and never in a
-- table a client role can read.

create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function portal.dispatch_workflow(workflow text)
returns bigint
language plpgsql
security definer
set search_path = portal, public
as $$
declare
  tok text;
  req bigint;
begin
  select decrypted_secret into tok from vault.decrypted_secrets where name = 'github_actions_token';
  if tok is null then
    raise exception 'github_actions_token is not in Vault, so % was not dispatched', workflow;
  end if;
  select net.http_post(
    url := 'https://api.github.com/repos/Parallelpath-dev/Scout-client/actions/workflows/' || workflow || '/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || tok,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'scout-portal-scheduler',
      'Content-Type', 'application/json'),
    body := jsonb_build_object('ref', 'main'))
  into req;
  insert into portal.dispatch_log (workflow, request_id) values (workflow, req);
  return req;
end;
$$;

-- What was sent, and what GitHub answered (204 = accepted). net._http_response keeps
-- answers for a few hours only, so the watchdog copies the status here.
create table if not exists portal.dispatch_log (
  id bigserial primary key,
  workflow text not null,
  request_id bigint,
  sent_at timestamptz not null default now(),
  status_code int,
  reason text
);
alter table portal.dispatch_log enable row level security;
revoke all on portal.dispatch_log from anon, authenticated;

-- The Monday the current briefing week starts (UTC).
create or replace function portal.this_monday() returns date
language sql stable as $$ select (date_trunc('week', now() at time zone 'utc'))::date $$;

-- Synthesis only starts once the collectors have written the week. Without this, a
-- Monday when Portal weekly failed would produce a briefing about an empty week.
create or replace function portal.dispatch_synthesize_if_ready()
returns text
language plpgsql
security definer
set search_path = portal, public
as $$
declare
  wk date := portal.this_monday();
  bp uuid := (select id from public.clients where slug = 'bouldering-project');
  have int;
begin
  select count(*) into have from portal.signals
    where client_id = bp and week_of = wk and signal_type in ('ad_active', 'page_snapshot');
  if have = 0 then
    insert into portal.dispatch_log (workflow, reason) values ('portal_synthesize.yml', 'skipped: no collector data for ' || wk);
    return 'skipped, weekly data missing';
  end if;
  perform portal.dispatch_workflow('portal_synthesize.yml');
  return 'dispatched';
end;
$$;

-- Two hours after each dispatch: record GitHub's answer, and if the week's data is
-- still missing, dispatch once more. One retry only, so a real failure stays visible
-- as a red run rather than looping.
create or replace function portal.monday_watchdog(stage text)
returns text
language plpgsql
security definer
set search_path = portal, public
as $$
declare
  wk date := portal.this_monday();
  have int;
  bp uuid := (select id from public.clients where slug = 'bouldering-project');
begin
  update portal.dispatch_log d set status_code = r.status_code, reason = left(coalesce(r.error_msg, r.content::text), 300)
    from net._http_response r where r.id = d.request_id and d.status_code is null;

  if stage = 'weekly' then
    select count(*) into have from portal.signals
      where client_id = bp and week_of = wk and signal_type in ('ad_active', 'page_snapshot');
    if have = 0 then
      perform portal.dispatch_workflow('portal_weekly.yml');
      return 'weekly missing, dispatched again';
    end if;
  elsif stage = 'synthesize' then
    select count(*) into have from portal.briefings where client_id = bp and week_of = wk;
    if have = 0 then
      return portal.dispatch_synthesize_if_ready();
    end if;
  end if;
  return stage || ' present';
end;
$$;

revoke all on function portal.dispatch_workflow(text) from public, anon, authenticated;
revoke all on function portal.monday_watchdog(text) from public, anon, authenticated;
revoke all on function portal.dispatch_synthesize_if_ready() from public, anon, authenticated;

-- Same clock as before: collectors 08:00, Semrush task 14:00, synthesis 15:00 (UTC).
select cron.schedule('portal-weekly',            '0 8 * * 1',  $$select portal.dispatch_workflow('portal_weekly.yml')$$);
select cron.schedule('portal-weekly-check',      '0 10 * * 1', $$select portal.monday_watchdog('weekly')$$);
select cron.schedule('portal-synthesize',        '0 15 * * 1', $$select portal.dispatch_synthesize_if_ready()$$);
select cron.schedule('portal-synthesize-check',  '0 17 * * 1', $$select portal.monday_watchdog('synthesize')$$);
