# Scout — Client Portal

One app, every client. Which client you see is decided by your login, not by
the URL. There is no client switcher and no client parameter anywhere in this
codebase, so there is no code path that reaches another tenant's data.

Separate from the internal Scout dashboard on purpose.

```
.nojekyll                       stops GitHub from preprocessing the files
index.html                      the portal, one file
network-monitor.html            one-time network analysis, embedded as a tab
bouldering_project_logo.jpg     client logo, hosted by us (never hotlinked)
bouldering-project-theme.json   the theme, with provenance for every value
BP-SCOUT-FORMATTING.md          the design reference for this client

pipeline/geo.py                 geo classifier for competitor ad creative
pipeline/geo_terms.json         its vocabulary. The part that churns.
pipeline/test_geo.py            fixtures pinning the decisions that are easy to get wrong
pipeline/classify_meta_ads.py   reads the internal pull, classifies, writes portal.*
pipeline/executive_profile.py   prompt constraints for the client-facing output profile
pipeline/validate_briefing.py   the gate. Holds a briefing rather than publishing a bad one.

migrations/001_*.sql            auth, isolation policies, market + publish columns
migrations/002_*.sql            Bouldering Project tenant (contact details scrubbed)
migrations/003_portal_schema.sql  the `portal` schema: five tables, RLS, anon granted nothing
migrations/004_signal_dedupe.sql  external_ref + week_of, so a re-run is idempotent
migrations/005_ad_sampling.sql    splits the library total from the classified sample
migrations/006_channel_max_ads.sql per-channel ad cap. NULL = census, which is the default.

scripts/test_isolation.py       proves a client login can't reach another client
scripts/extract-brand.js        console script that pulls a client's palette
.github/workflows/portal_weekly.yml  Mondays 09:00 UTC, after the internal pipeline
```

## How this fits with the internal tool

Three moving parts, and the seam between them is the database, not shared code.

| Where | What it does |
|---|---|
| this repo | Collects this client's competitor ads, classifies them for geographic relevance, writes `portal.*`, and serves the portal. |
| `Parallelpath-dev/scout` | The internal tool. Different clients, different output profile, no shared code. |
| Supabase | Holds both schemas. `public` is the internal working set, `portal` is what a client can see and what this pipeline owns. |

**This pipeline collects its own ads.** It does not read from the internal tool and the
internal tool does not know this client's competitors exist. Their config lives in
`portal.competitors` and `portal.channels`, never in `public.clients.config`, which is
what keeps `weekly_scout.yml` from picking up the same pages. A cap, a new competitor or
a retired channel is a change in this repo alone.

That independence costs nothing in Apify spend, because these five pages would otherwise
be scraped by the internal tool instead, not in addition. It buys two things the internal
collector cannot give us: full ad copy, where it truncates to 500 characters and would
drop geography mentioned late in a long body, and a census rather than the top 35 ads by
impressions.

`--source internal` remains available for the day this client is also an internal client,
where a second scrape would genuinely be paying twice.

**What the ad numbers mean.** The Meta Ad Library exposes no targeting, audience
location, DMA or delivery region for commercial ads anywhere, and US commercial ads are
not in the API at all. So `dc_referencing` counts ads whose creative REFERENCES this
market. It is a floor on local activity, never a count of ads targeted at DC, and an ad
classified `none` may be a competitor's heaviest local spend. `portal.ad_geo_weekly`
has no per-market average column and never should: dividing a national ad count by a
location count produces a number with no basis in anything observable.

**This repo is public.** GitHub Pages requires it, and Pages serves every file
at its path — `/migrations/001_tenant_isolation.sql` is fetchable by anyone.
Nothing here is secret: the publishable key grants nothing without row-level
security, and the schema is not a credential. But **no client contact details,
API keys, or service keys belong in this repo, ever.** Authorizing a person's
email is done in the Supabase SQL editor, never in a committed file.

## Deploy

1. New **public** repo: `Parallelpath-dev/scout-client`. Public is required —
   GitHub only serves Pages from private repos on Enterprise plans. Push these
   files to `main`.
2. Settings → Pages → deploy from `main`, root. Lands at
   `https://parallelpath-dev.github.io/scout-client/`.
3. Supabase → Authentication → URL Configuration → add that URL to
   **Redirect URLs**. Skip this and sign-in links fail silently in a way that
   looks like the email never sent.
4. Apply `migrations/001` then `migrations/002`.
5. Run `authorize-users.sql` in the Supabase SQL editor (delivered separately,
   never committed) to authorize the client's addresses.
6. Run `python3 scripts/test_isolation.py`, then again with `--token` from a
   real signed-in session. Both parts clean before anyone gets a link.
7. Point Supabase Auth at Resend for SMTP, or the sign-in emails come from a
   generic Supabase address, land in spam, and rate-limit after a handful.

## Adding a client

No code changes. Ever. If you find yourself editing `index.html` to onboard
someone, the config is missing a field — add the field.

1. Run `scripts/extract-brand.js` in the console on their site. Do the home page and one
   interior page; home pages are nav-heavy and skew the palette.
2. Sanity-check the result. The extractor ranks colors by area, so it reliably
   mislabels a large surface as the accent. The real accent is usually a small
   color that turns up across background, text, *and* border.
3. Copy `migrations/002` as a template. New slug, new theme, their market,
   `output_profile` (`executive` for a CEO, `operator` for an in-house team),
   and `call_sweep_enabled = false` for any prospect.
4. Add their people to `client_users` **from the Supabase SQL editor**, not from
   a file in this repo. That authorizes an address; it creates nothing and sends
   nothing. The account appears when they first request a link.
5. Run the isolation tests again.

## Things that will bite you

- **The publishable key is in the source and that's fine.** Row-level security
  is what protects the data. If you ever disable RLS on a table, that key
  becomes a real problem the same afternoon.
- **`brain` drives recommendation quality.** A placeholder there produces
  generic advice. Migration 002 seeds a placeholder that starts with `AWAITING`,
  and the portal's setup card reads that prefix and keeps showing "Waiting on
  you" until it's replaced.
- **`call_sweep_enabled = false` matters for prospects.** The Fathom sweep pulls
  call content into `brain`. A prospect's strategic context should never be
  assembled from recordings of your sales calls.
- **Unpublishing is `published_at = null`.** The row survives, the client stops
  seeing it, effective immediately. That's the kill switch for a bad week.
