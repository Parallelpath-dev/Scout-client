-- Monday runs follow Chicago time, daylight saving included.
--
-- pg_cron only knows UTC, and 08:45 in Chicago is 13:45 UTC in summer and 14:45 UTC
-- in winter. Each job is scheduled at both UTC hours and does nothing unless it is the
-- right minute in Chicago, so exactly one of the two fires all year.
--
--   08:45  Portal weekly (collectors)
--   10:00  Portal synthesize, only if the collectors wrote the week
--   10:45  check: no ads or page checks yet -> start Portal weekly once more
--   12:00  check: no briefing yet -> start Portal synthesize once more (if data exists)

create or replace function portal.central_time_is(hhmm text) returns boolean
language sql stable as $$ select to_char(now() at time zone 'America/Chicago', 'HH24:MI') = hhmm $$;

select cron.unschedule('portal-weekly');
select cron.unschedule('portal-weekly-check');
select cron.unschedule('portal-synthesize');
select cron.unschedule('portal-synthesize-check');

select cron.schedule('portal-weekly', '45 13,14 * * 1',
  $$select portal.dispatch_workflow('portal_weekly.yml') where portal.central_time_is('08:45')$$);
select cron.schedule('portal-synthesize', '0 15,16 * * 1',
  $$select portal.dispatch_synthesize_if_ready() where portal.central_time_is('10:00')$$);
select cron.schedule('portal-weekly-check', '45 15,16 * * 1',
  $$select portal.monday_watchdog('weekly') where portal.central_time_is('10:45')$$);
select cron.schedule('portal-synthesize-check', '0 17,18 * * 1',
  $$select portal.monday_watchdog('synthesize') where portal.central_time_is('12:00')$$);
