# Scout — Client Portal

One app, every client. Which client you see is decided by your login, not by
the URL. There is no client switcher and no client parameter anywhere in this
codebase, so there is no code path that reaches another tenant's data.

Separate from the internal Scout dashboard on purpose.

```
.nojekyll                   stops GitHub from preprocessing the files
index.html                  the whole app, one file
assets/bp-logo.jpg          client logo, hosted by us (never hotlinked)
migrations/001_*.sql        auth, isolation policies, market + publish columns
migrations/002_*.sql        Bouldering Project tenant (contact details scrubbed)
scripts/test_isolation.py   proves a client login can't reach another client
scripts/extract-brand.js    console script that pulls a client's palette
bouldering-project-theme.json   the theme, with provenance for every value
```

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
