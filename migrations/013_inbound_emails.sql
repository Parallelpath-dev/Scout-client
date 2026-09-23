-- 013 — portal.inbound_emails
--
-- A raw, append-only store for every message that arrives at the scout aliases.
--
-- WHY THIS IS A SEPARATE TABLE AND NOT JUST A SIGNAL
-- --------------------------------------------------
-- The internal tool's equivalent (public.competitor_emails) has competitor_name NOT NULL.
-- That single word is why it holds 335 messages for ten internal aliases and zero for
-- Bouldering Project's four: an email from a sender nobody had mapped yet failed its
-- insert, and the message was gone. Resend had delivered it. Nothing kept it.
--
-- So the receiving end here does exactly one thing — write the message down — and every
-- column that could fail to resolve is nullable. Competitor unknown, client unknown,
-- sender domain never seen before: the row is still written, with match_status
-- 'unmatched', and can be claimed later by adding one handle or one sender domain.
-- Classification happens afterwards, in Python, where the geo vocabulary already lives
-- and is already tested. Receiving must never be able to lose a message.
--
-- MATCHING
-- --------
-- to_address is the strong key. We created one alias per competitor, so a message sent
-- to steven.richard@scout.parallelpath.com is Sportrock's, whatever it claims to be
-- from. Sender domain is the fallback, and it exists because VIDA mails from
-- uacompanies.com, not vidafitness.com — a from-domain-only matcher drops those.

create table if not exists portal.inbound_emails (
  id             uuid primary key default gen_random_uuid(),

  -- dedupe. Resend can redeliver a webhook; the same message must not become two rows.
  message_id     text,                    -- RFC 5322 Message-ID when present
  provider_id    text,                    -- Resend's own id, used when Message-ID is absent
  dedupe_key     text generated always as (coalesce(message_id, provider_id)) stored,

  received_at    timestamptz not null default now(),
  sent_at        timestamptz,

  to_address     text,
  from_address   text,
  from_domain    text,
  from_name      text,
  subject        text,
  text_body      text,
  html_body      text,
  headers        jsonb not null default '{}'::jsonb,

  -- All four nullable on purpose. See the note above.
  client_id      uuid,
  competitor_id  uuid references portal.competitors(id) on delete set null,
  channel_id     uuid references portal.channels(id) on delete set null,
  match_status   text not null default 'unmatched'
                 check (match_status in ('matched', 'unmatched', 'ignored')),
  match_method   text check (match_method in ('to_address', 'sender_domain', 'manual')),

  -- set by the weekly Python pass, not by the receiver
  classified_at  timestamptz,
  signal_id      uuid references portal.signals(id) on delete set null,

  raw            jsonb not null default '{}'::jsonb
);

-- Non-partial so PostgREST can use it as an ON CONFLICT target. A partial index fails
-- with 42P10 there, which cost us a run on the ads collector already.
-- NULLS NOT DISTINCT would collapse every message that has neither id into one row, so
-- the default (nulls distinct) is deliberate: an id-less message is always inserted.
create unique index if not exists inbound_emails_dedupe_idx
  on portal.inbound_emails (dedupe_key);

create index if not exists inbound_emails_unclassified_idx
  on portal.inbound_emails (received_at)
  where classified_at is null;

create index if not exists inbound_emails_competitor_idx
  on portal.inbound_emails (competitor_id, received_at desc);

create index if not exists inbound_emails_from_domain_idx
  on portal.inbound_emails (from_domain);

alter table portal.inbound_emails enable row level security;

-- anon gets nothing. The portal reads briefings, never raw mail.
grant usage on schema portal to service_role;
grant select, insert, update, delete on portal.inbound_emails to service_role;

comment on table portal.inbound_emails is
  'Raw inbound competitor mail. Every resolvable column is nullable so that an '
  'unrecognised sender is stored rather than dropped. Classified later by '
  'pipeline/classify_emails.py.';

comment on column portal.inbound_emails.competitor_id is
  'Nullable on purpose. The internal tool made this NOT NULL and silently lost every '
  'message from an unmapped sender.';
