/**
 * portal-inbound-email — the Bouldering Project portal's inbound mail receiver.
 *
 * WHY THIS EXISTS SEPARATELY FROM `inbound-email`
 * -----------------------------------------------
 * Resend has no per-address inbound routing. One MX record covers the whole of
 * scout.parallelpath.com, and every message to every alias fires the same
 * `email.received` event. Which alias it was addressed to is a field in the payload,
 * not a routing decision.
 *
 * So the split happens at the webhook subscription: Resend fans `email.received` out to
 * every subscribed endpoint. `inbound-email` keeps receiving all fourteen aliases and
 * ignores the four it does not know. This function receives all fourteen and ignores the
 * ten it does not know. Neither is modified to accommodate the other, and neither can
 * break the other by being redeployed.
 *
 * WHAT THIS FUNCTION DOES, AND WHAT IT REFUSES TO DO
 * ---------------------------------------------------
 * It writes the message down. That is all.
 *
 * It does not classify, does not score geography, does not decide whether something is
 * a promotion. Those judgments live in pipeline/, in Python, next to the vocabulary they
 * share with the ad and web-change classifiers, under test. Duplicating them in
 * TypeScript would mean two copies of the same regex drifting apart in silence.
 *
 * More importantly: a receiver that can make a judgment is a receiver that can fail on
 * one. `public.competitor_emails.competitor_name` is NOT NULL, and that is the entire
 * reason the internal tool holds 335 messages for its own aliases and none for these
 * four — an unmapped sender failed its insert and the message was gone. Resend had
 * already delivered it. There was no second chance.
 *
 * Here, an unrecognised sender is a stored row with match_status 'unmatched'. Adding one
 * handle or one sender domain later claims every message already sitting in the table.
 *
 * DEPLOY
 *   verify_jwt: false — a webhook cannot present a JWT. Authenticity comes from the
 *   Svix signature instead (see verifySignature).
 *
 * ENV
 *   SUPABASE_URL                  (provided by the platform)
 *   SUPABASE_SERVICE_ROLE_KEY     (provided by the platform)
 *   PORTAL_INBOUND_SIGNING_SECRET Resend's webhook signing secret, `whsec_...`.
 *                                 If unset the function still runs but logs loudly on
 *                                 every request, because an unauthenticated endpoint
 *                                 that writes to the database is not something to
 *                                 discover by accident six months from now.
 */

import { createClient } from "jsr:@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const SIGNING_SECRET = Deno.env.get("PORTAL_INBOUND_SIGNING_SECRET") ?? "";
// Resend's email.received webhook carries metadata only: no body, no headers. The body
// is fetched from the Received Emails API by the event's email_id. Without this key every
// message is stored as a subject line and classified blind.
const RESEND_API_KEY = Deno.env.get("RESEND_API_KEY") ?? "";

// Mail from our own domain is our own doing — bounce notices, the signup confirmations
// we generated ourselves, anything looping. Stored, never counted.
const OWN_DOMAIN = "parallelpath.com";

const db = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { persistSession: false },
  db: { schema: "portal" },
});

// ── Svix signature ──────────────────────────────────────────────────────────
// Resend signs webhooks with Svix's scheme: HMAC-SHA256 over `${id}.${timestamp}.${body}`
// keyed by the base64 secret after the `whsec_` prefix. The signature header can carry
// several space-separated `v1,<sig>` values during a secret rotation, so any match counts.

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function verifySignature(req: Request, body: string): Promise<boolean> {
  if (!SIGNING_SECRET) {
    console.warn(
      "PORTAL_INBOUND_SIGNING_SECRET is not set — accepting unverified webhook. " +
        "Set it in the function's secrets.",
    );
    return true;
  }

  const id = req.headers.get("svix-id") ?? req.headers.get("webhook-id");
  const ts = req.headers.get("svix-timestamp") ?? req.headers.get("webhook-timestamp");
  const sigHeader = req.headers.get("svix-signature") ?? req.headers.get("webhook-signature");
  if (!id || !ts || !sigHeader) return false;

  // Reject anything older than five minutes. A replayed webhook cannot do much harm
  // here — the dedupe index would collapse it — but an endpoint that accepts arbitrarily
  // old signed payloads is a habit worth not forming.
  const age = Math.abs(Date.now() / 1000 - Number(ts));
  if (!Number.isFinite(age) || age > 300) return false;

  const secretB64 = SIGNING_SECRET.startsWith("whsec_")
    ? SIGNING_SECRET.slice(6)
    : SIGNING_SECRET;
  const keyBytes = Uint8Array.from(atob(secretB64), (c) => c.charCodeAt(0));

  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`${id}.${ts}.${body}`),
  );
  const expected = btoa(String.fromCharCode(...new Uint8Array(mac)));

  return sigHeader
    .split(" ")
    .map((p) => p.split(",", 2)[1] ?? "")
    .some((s) => timingSafeEqual(s, expected));
}

// ── payload shredding ───────────────────────────────────────────────────────
// Written defensively on purpose. The cost of a shape we did not expect should be a
// null column, never a dropped message.

function addressOf(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const m = v.match(/<([^>]+)>/);
  const addr = (m ? m[1] : v).trim().toLowerCase();
  return addr.includes("@") ? addr : null;
}

function displayNameOf(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const m = v.match(/^\s*"?([^"<]*?)"?\s*</);
  const name = m?.[1]?.trim();
  return name ? name : null;
}

function firstAddress(v: unknown): string | null {
  if (Array.isArray(v)) {
    for (const item of v) {
      const a = addressOf(item);
      if (a) return a;
    }
    return null;
  }
  return addressOf(v);
}

/** Headers arrive as [{name, value}] or as a plain object. Normalise to an object. */
function normaliseHeaders(v: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (Array.isArray(v)) {
    for (const h of v) {
      const name = (h as Record<string, unknown>)?.name;
      const value = (h as Record<string, unknown>)?.value;
      if (typeof name === "string") out[name.toLowerCase()] = String(value ?? "");
    }
  } else if (v && typeof v === "object") {
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
      out[k.toLowerCase()] = String(val ?? "");
    }
  }
  return out;
}

function toISO(v: unknown): string | null {
  if (typeof v !== "string" && typeof v !== "number") return null;
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

// ── body fetch ──────────────────────────────────────────────────────────────
// GET /emails/receiving/{id}. Best effort: a failure leaves the body null and the message
// stored, and the backfill route below can fetch it later.

type Body = { text: string | null; html: string | null; headers: Record<string, string> };

async function fetchBody(emailId: string): Promise<Body | null> {
  if (!RESEND_API_KEY) {
    console.warn("RESEND_API_KEY is not set, so email bodies cannot be fetched.");
    return null;
  }
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 10_000);
  try {
    const r = await fetch(`https://api.resend.com/emails/receiving/${encodeURIComponent(emailId)}`, {
      headers: { Authorization: `Bearer ${RESEND_API_KEY}` },
      signal: ctl.signal,
    });
    if (!r.ok) {
      console.error(`body fetch ${emailId} failed: ${r.status} ${(await r.text()).slice(0, 200)}`);
      return null;
    }
    const j = await r.json() as Record<string, unknown>;
    return {
      text: typeof j.text === "string" ? j.text : null,
      html: typeof j.html === "string" ? j.html : null,
      headers: normaliseHeaders(j.headers),
    };
  } catch (e) {
    console.error(`body fetch ${emailId} errored:`, e);
    return null;
  } finally {
    clearTimeout(t);
  }
}

// ── backfill ────────────────────────────────────────────────────────────────
// POST {"type":"portal.backfill","token":"..."} fills in bodies for stored rows that have
// none, and clears classified_at so the next classify run reads the real text. The token
// is compared with portal.internal_settings('backfill_token'); the route takes no ids, so
// the most it can ever do is re-fetch our own messages.

async function backfill(token: unknown): Promise<Response> {
  const { data: cfg } = await db.from("internal_settings").select("value").eq("key", "backfill_token")
    .maybeSingle();
  if (!cfg?.value || typeof token !== "string" || !timingSafeEqual(token, String(cfg.value))) {
    return new Response("forbidden", { status: 403 });
  }
  const { data: rows, error } = await db.from("inbound_emails")
    .select("id, raw, message_id, sent_at")
    .is("text_body", null).is("html_body", null)
    .order("received_at", { ascending: true }).limit(50);
  if (error) return new Response(JSON.stringify({ ok: false, error: error.message }), { status: 500 });
  const out = { filled: 0, failed: 0, skipped: 0 };
  for (const row of rows ?? []) {
    const d = ((row.raw as Record<string, unknown>)?.data ?? {}) as Record<string, unknown>;
    const id = typeof d.email_id === "string" ? d.email_id : typeof d.id === "string" ? d.id : null;
    if (!id) { out.skipped++; continue; }
    const b = await fetchBody(id);
    if (!b || (!b.text && !b.html)) { out.failed++; continue; }
    const upd: Record<string, unknown> = {
      text_body: b.text, html_body: b.html, headers: b.headers, classified_at: null,
    };
    if (!row.message_id) {
      upd.message_id = b.headers["message-id"] ?? (typeof d.message_id === "string" ? d.message_id : null);
    }
    if (b.headers["date"]) upd.sent_at = toISO(b.headers["date"]) ?? row.sent_at;
    const { error: e2 } = await db.from("inbound_emails").update(upd).eq("id", row.id);
    if (e2) { console.error("backfill update failed", row.id, e2); out.failed++; } else out.filled++;
  }
  return new Response(JSON.stringify({ ok: true, ...out }), {
    status: 200, headers: { "content-type": "application/json" },
  });
}

// ── handler ─────────────────────────────────────────────────────────────────

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  const body = await req.text();

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(body);
  } catch {
    // Malformed JSON will be malformed on every retry. 400 so Resend stops.
    return new Response("bad json", { status: 400 });
  }

  // The backfill route authenticates with its own token (checked in backfill()).
  if (payload.type !== "portal.backfill" && !(await verifySignature(req, body))) {
    console.error("signature verification failed");
    return new Response("invalid signature", { status: 401 });
  }

  if (payload.type === "portal.backfill") return await backfill(payload.token);

  const eventType = String(payload.type ?? "");
  if (eventType !== "email.received") {
    // We subscribe only to email.received, but the endpoint may be pointed at more by
    // accident. Acknowledge and do nothing rather than fail a retry loop.
    return new Response(JSON.stringify({ ok: true, skipped: eventType }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }

  const data = (payload.data ?? {}) as Record<string, unknown>;
  const emailId = typeof data.email_id === "string" ? data.email_id
    : typeof data.id === "string" ? data.id : null;
  const fetched = emailId ? await fetchBody(emailId) : null;
  const headers = { ...normaliseHeaders(data.headers), ...(fetched?.headers ?? {}) };

  const toAddress = firstAddress(data.to);
  const fromAddress = addressOf(data.from);
  const fromDomain = fromAddress?.split("@")[1] ?? null;

  const record = {
    message_id: headers["message-id"] ?? (typeof data.message_id === "string" ? data.message_id : null),
    provider_id: emailId,
    sent_at: toISO(headers["date"] ?? data.created_at ?? payload.created_at),
    to_address: toAddress,
    from_address: fromAddress,
    from_domain: fromDomain,
    from_name: displayNameOf(data.from),
    subject: typeof data.subject === "string" ? data.subject : null,
    text_body: fetched?.text ?? (typeof data.text === "string" ? data.text : null),
    html_body: fetched?.html ?? (typeof data.html === "string" ? data.html : null),
    headers,
    client_id: null as string | null,
    competitor_id: null as string | null,
    channel_id: null as string | null,
    match_status: "unmatched",
    match_method: null as string | null,
    raw: payload,
  };

  // ── match ─────────────────────────────────────────────────────────────────
  // Everything below is best-effort. A failure to match is a stored row, not a
  // dropped message, so none of it is wrapped in a way that can abort the insert.
  try {
    if (fromDomain && fromDomain.endsWith(OWN_DOMAIN)) {
      // Our own signup confirmations, bounces, loops. Kept for the audit trail,
      // excluded from anything that counts.
      record.match_status = "ignored";
    } else {
      // Flat select, no embed. client_id is derivable from competitor_id and is filled
      // in by the classifier; resolving it here would mean a join whose failure mode is
      // "every message arrives unmatched", and the receiver's job is to be boring.
      const { data: channels, error } = await db
        .from("channels")
        .select("id, competitor_id, handle, sender_domains")
        .eq("purpose", "email");

      if (error) throw error;

      type Row = {
        id: string;
        competitor_id: string;
        handle: string | null;
        sender_domains: string[] | null;
      };

      const rows = (channels ?? []) as Row[];

      // to_address first. We created one alias per competitor, so the alias a message
      // was sent to is definitive — it survives a rebrand, an ESP change, and VIDA
      // mailing from uacompanies.com instead of vidafitness.com.
      let hit = rows.find(
        (r) => r.handle && toAddress && r.handle.toLowerCase() === toAddress,
      );
      let method: string | null = hit ? "to_address" : null;

      // Sender domain as the fallback: catches a message forwarded into the alias by
      // hand, or a competitor mailing an address we did not sign up with.
      if (!hit && fromDomain) {
        hit = rows.find((r) =>
          (r.sender_domains ?? []).some(
            (d) => d && (fromDomain === d.toLowerCase() || fromDomain.endsWith("." + d.toLowerCase())),
          )
        );
        if (hit) method = "sender_domain";
      }

      if (hit) {
        record.channel_id = hit.id;
        record.competitor_id = hit.competitor_id;
        record.match_status = "matched";
        record.match_method = method;
      }
    }
  } catch (e) {
    // Matching failed — the lookup errored, the schema moved, whatever it was. The
    // message is still worth keeping, so fall through and store it unmatched.
    console.error("match failed, storing unmatched:", e);
  }

  // ── store ─────────────────────────────────────────────────────────────────
  // Upsert on the dedupe key so a Resend redelivery updates rather than duplicates.
  // A message carrying neither a Message-ID nor a provider id has a null dedupe_key,
  // which under the default nulls-distinct index is always inserted — the right
  // trade-off: a rare duplicate beats collapsing unrelated messages into one row.
  const { error } = await db
    .from("inbound_emails")
    .upsert(record, { onConflict: "dedupe_key", ignoreDuplicates: false });

  if (error) {
    // A real storage failure. 500 so Resend retries, because this is the one case where
    // giving up means losing the message.
    console.error("insert failed:", error, {
      to: toAddress,
      from: fromAddress,
      subject: record.subject,
    });
    return new Response(JSON.stringify({ ok: false, error: error.message }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  }

  console.log(
    `stored ${record.match_status}`,
    JSON.stringify({ to: toAddress, from: fromAddress, subject: record.subject }),
  );

  return new Response(
    JSON.stringify({ ok: true, match: record.match_status }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
});
