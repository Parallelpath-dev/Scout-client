# Bouldering Project — Scout formatting reference

Everything visual for the Bouldering Project client portal. Paste-ready.
Last updated 10 Sep 2026.

Live at `parallelpath-dev.github.io/Scout-client/`.

---

## Ground rule

**No BP-specific value belongs in a file.** Colors, fonts, logo, and the section list all live on
the client's row in Supabase (`clients.theme` and `clients.config`) and get applied to `:root` at
runtime. The values below are documentation of what's in that record — not something to hardcode.

If you need to change a color, update the database. If you need a *new* kind of token, add one
line to the `MAP` in `index.html` and one `var()` reference.

---

## Palette

Measured off `boulderingproject.com`, not chosen.

```css
:root{
  --ground:      #3D3935;                    /* page background — their warm near-brown */
  --surface:     #4A4540;                    /* cards, sidebar */
  --sunk:        #332F2C;                    /* recessed panels */
  --ink:         #FFFFFF;                    /* primary text */
  --muted:       #97A2BA;                    /* secondary text — their cool blue-grey */
  --line:        rgba(255,255,255,0.13);     /* hairlines */
  --birch:       #97A2BA;                    /* neutral marker dots */
  --accent:      #D65F52;                    /* coral — the ONE emphasis color */
  --accent-soft: rgba(214,95,82,0.14);       /* active nav wash */
  --ok:          #97A2BA;                    /* settled state */
  --wait:        #D65F52;                    /* action-required state */
  --radius:      10px;
}
```

**Provenance, so nobody second-guesses it later:**

| Value | Where it came from |
|---|---|
| `#3D3935` | Dominant page background **and** dominant text color on their site — 2,644,331px² of coverage |
| `#97A2BA` | Secondary surface, 142,000px². Cool blue-grey |
| `#D65F52` | The real accent. Only 11,737px², but present across background, text, **and** border — that low-area/multi-role pattern is what an accent looks like |
| `#4E5569` | Measured but **deliberately unused** — a third cool grey that muddies the palette against the warm ground |

**Derived, not measured** — their site has no dashboard surfaces to sample:

- `--surface` is a ~7% lift off `--ground`
- `--sunk` is a recess below it
- `--line` is white at 13%. Their site's hairlines are solid white, which is too loud at dashboard density

**Two things to preserve:**

1. **Only one thing on a screen is coral.** On the setup card, the single coral element is
   "Approval pending" next to the competitor list — the one item we need from them. Everything else
   stays quiet. If two things shout, neither does.
2. **`--ok` is their blue-grey, not a green.** Their brand owns no green, so settled states use
   the color they do own. Don't introduce one.

---

## Type

Their site runs **FF Good Pro** across every role — self-hosted and licensed. We substitute
**Archivo**, the closest free grotesque, and leave Good Pro first in the stack so a licensed kit
would render with no code change.

```html
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700;800&display=swap" rel="stylesheet">
```

```css
--font-display: Archivo, 'Good Pro', system-ui, sans-serif;
--font-body:    Archivo, 'Good Pro', system-ui, sans-serif;
--font-mono:    Archivo, system-ui, sans-serif;   /* single-family brand — no mono face */
```

Don't hotlink Good Pro off their server. It breaches the license and fails referrer checks anyway.

### Scale

| Element | Size | Weight | Case |
|---|---|---|---|
| Wordmark | 23px rail / 52px login | 800 | As-is, `-.03em` tracking |
| Card heading | 24px | 700 | Sentence case |
| Section header | 13px | 700 | UPPERCASE, `.03em` |
| Row title | 15px | 600 | Sentence case |
| Body | 15px | 400 | Sentence case |
| Row description | 14px | 400 | Sentence case |
| Eyebrow / chip / marker | 10–11px | 500–700 | UPPERCASE, `.09–.14em` |

Body text caps at **56–62ch**.

### The uppercase decision

Their site sets `text-transform: uppercase` on `h1`, `h2`, **and** body. We take it for labels,
navigation, eyebrows, and state chips — that's their signature. We **drop it for body copy**.
Uppercase running text at dashboard density is hard to read, and Kyle and Trever read this weekly.
Their brand rule loses to legibility in the one place the two conflict.

---

## Logo

File: `bouldering_project_logo.jpg` at the **repo root** — hosted by us, never hotlinked from
their WordPress install.

```css
--logo-width:  58px;
--logo-height: 58px;
--logo-radius: 50%;                      /* circular badge — clipping drops the JPEG's white corners */
--logo-ring:   rgba(255,255,255,0.13);   /* keeps a near-black badge off a near-matching ground */
--logo-pad:    0;
```

Their `logo--black.svg` is a black wordmark on transparency and **disappears** against `#3D3935`.
If you ever need the wordmark instead of the badge, use their reversed footer version
(`logo-footer.svg`) rather than CSS-inverting theirs — inverting is a brand modification we don't
have permission to make. Last resort only: `logoBackdrop: "#FFFFFF"` with `logoPad: "8px"`.

The image also has an `onerror` handler that clears the container, so a missing logo looks like
no logo rather than a broken page.

---

## Components

### Eyebrow

```css
.eyebrow{
  font-family:var(--font-mono);font-size:11px;font-weight:500;
  letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
}
```

### Card

```css
.card{background:var(--surface);border:1px solid var(--line);
  border-radius:var(--radius);padding:22px 24px}
```

Emphasis card adds `border-left:3px solid var(--accent)`. One per screen, maximum.

### Section header

```css
.sec-h{display:flex;align-items:baseline;gap:12px;margin-bottom:13px}
.sec-h h3{font-family:var(--font-display);font-size:13px;font-weight:700;
  letter-spacing:.03em;text-transform:uppercase}
.sec-h span{font-size:13px;color:var(--muted)}   /* qualifier sits inline, not below */
```

### Checklist row

Three columns — mono marker, content, state chip. Reflows to two rows under 640px, because a
three-column grid squeezes sentences into a sliver on a phone.

```css
.step{display:grid;grid-template-columns:30px 1fr auto;gap:16px;align-items:start;
  padding:16px 0;border-bottom:1px solid var(--line)}
.step:last-child{border-bottom:none;padding-bottom:2px}
.step .n{font-family:var(--font-mono);font-size:11px;color:var(--muted);padding-top:2px}
.step .t{font-family:var(--font-display);font-weight:600;font-size:15px}
.step .d{color:var(--muted);font-size:14px;margin-top:2px;max-width:56ch}

@media(max-width:640px){
  .step{display:flex;flex-wrap:wrap;align-items:center;gap:9px 10px}
  .step .n{order:1;padding-top:0}
  .step .state{order:2;margin-left:auto}
  .step > div{order:3;flex:1 1 100%}
  .step .d{max-width:none}
}
```

### State chip — three states, semantics matter

```css
.state{font-family:var(--font-mono);font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;padding:5px 11px;border-radius:99px;
  white-space:nowrap;border:1px solid var(--line)}

.state.done{color:var(--ok);
  border-color:color-mix(in srgb,currentColor 38%,transparent);
  background:color-mix(in srgb,currentColor 9%,transparent)}
.state.wait{color:var(--wait);
  border-color:color-mix(in srgb,currentColor 42%,transparent);
  background:color-mix(in srgb,currentColor 11%,transparent)}
.state.sched{color:var(--muted);
  border-color:color-mix(in srgb,currentColor 30%,transparent);
  background:transparent}
```

`color-mix` against `currentColor` means each chip re-tints itself from the theme token. Never
hardcode a chip background.

- **`done`** — settled. Blue-grey.
- **`wait`** — the client owes us something. Coral. The only accent-colored element on the screen.
- **`sched`** — on the calendar, nobody blocked. Muted. *Scheduled is not blocked* — using `wait`
  for a future date makes a date look like a problem.

### Navigation

```css
.nav-i{display:flex;justify-content:space-between;align-items:center;gap:8px;
  padding:9px 12px;border-radius:8px;font-size:14px;color:var(--muted);
  cursor:pointer;border:none;background:none;width:100%;
  font-family:var(--font-body);text-align:left}
.nav-i:hover{background:var(--sunk);color:var(--ink)}
.nav-i[aria-current=true]{background:var(--accent-soft);color:var(--accent);font-weight:600}
```

Real `<button>` elements with `aria-current`, not styled divs. Keyboard access comes free.

### Top bar

```css
.bar{display:flex;justify-content:space-between;align-items:center;gap:16px;
  padding:19px 34px;border-bottom:1px solid var(--line);
  background:var(--ground);position:sticky;top:0;z-index:10}
.bar h1{font-family:var(--font-display);font-size:15px;font-weight:600}
.chip{font-family:var(--font-mono);font-size:10px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted);border:1px solid var(--line);
  background:var(--surface);padding:5px 11px;border-radius:99px;white-space:nowrap}
```

### Embedded analysis panel

For the Network Signal Monitor tab, and anything like it:

```css
.panel.embed{padding:20px 24px;max-width:none}
.panel.embed iframe{width:100%;height:calc(100vh - 120px);border:none;
  display:block;background:transparent}
```

```js
p.innerHTML = `<iframe src="${esc(s.src)}" title="${esc(s.title)}"
  sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
  loading="lazy"></iframe>`;
```

Same-origin file in the repo, still sandboxed. On the embedded page: set `body` background to
`transparent` so it doesn't paint over the portal, and strip its own masthead — the portal supplies
the title bar.

**Any one-time analysis carries a dated banner at the top.** Every other tab updates weekly; one
stale number makes the live tabs look unreliable too. State the collection date, the window, and
the sample size. The monitor's banner is the pattern to copy.

### Section list

The six tabs come from `clients.config.sections`, not from code:

```json
[{"id":"overview","label":"Overview","title":"Overview"},
 {"id":"search","label":"Search Intelligence","title":"Search Intelligence"},
 {"id":"paid","label":"Paid Monitoring","title":"Paid Monitoring"},
 {"id":"social","label":"Social Presence","title":"Social Presence"},
 {"id":"owned","label":"Owned Channels","title":"Owned Channels"},
 {"id":"netmon","label":"Network Signal Monitor",
  "title":"Network Social Signal Monitor · May 20 – Aug 20, 2026",
  "type":"embed","src":"network-monitor.html"}]
```

Data Health is deliberately absent. Collector status is our plumbing, and a failed scrape is our
problem to fix, not something to show a client.

---

## Copy rules

- **No em dashes.** Commas or full stops.
- **Active voice, human subject.** "We're setting up your Scout," not "your Scout is being set up."
- **No false agency.** "What you'll see here," not "what lands here." Things don't act; people do.
- **No throat-clearing** — cut "here's what," "the reality is," "it's worth noting."
- **No adverbs.** "The DC operators you compete with," not "the DC operators you *actually* compete with."
- **Sentence case** everywhere except eyebrows, chips, and nav labels.
- **Say what they get, not what we run.** "Which terms your competitors gained or lost," not
  "Semrush organic keyword delta."
- **Empty states give direction, not mood.** Name what's missing and who owns it.
- **The login screen never confirms who has an account.** The success message reads identically
  whether or not the address is authorized.

---

## Quality floor

- Responsive to 390px. Test it.
- `:focus-visible{outline:2px solid var(--accent);outline-offset:3px}`
- `@media (prefers-reduced-motion:reduce)` kills animations and transitions.
- Escape anything rendered from data — there's an `esc()` helper in `index.html`.
- Wide content scrolls inside its own container. The page body never scrolls horizontally.

---

## Don't

- Hardcode a color, name, logo path, or section list in a file.
- Use more than one accent color per screen.
- Give `--ok` a green. Their palette has no green.
- Hotlink their logo or webfont off their servers.
- CSS-invert their logo.
- Put client contact details in the repo. It's public, and Pages serves every path in it.
- Uppercase body copy, even though their site does.
