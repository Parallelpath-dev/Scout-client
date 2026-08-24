/* ═══════════════════════════════════════════════════════════════════════
   Scout — brand extractor
   Paste into the DevTools console on the client's site (home page is best,
   but run it on an interior page too and compare — nav-heavy home pages
   can skew the palette).
   Output lands on your clipboard as JSON shaped exactly like the `theme`
   column the portal reads, so it can be pasted straight into the client row.
   ═══════════════════════════════════════════════════════════════════════ */
(() => {
  const hex = v => {
    if (!v) return null;
    v = v.trim();
    if (v.startsWith('#')) return v.toLowerCase();
    const m = v.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[,\s/]+/).filter(Boolean).map(Number);
    if (p.length < 3 || p.some(isNaN)) return null;
    if (p.length > 3 && p[3] === 0) return null;            // fully transparent
    return '#' + p.slice(0, 3).map(n =>
      Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0')).join('');
  };

  const isNeutral = h => {
    if (!h) return true;
    const r = parseInt(h.slice(1, 3), 16), g = parseInt(h.slice(3, 5), 16), b = parseInt(h.slice(5, 7), 16);
    return (Math.max(r, g, b) - Math.min(r, g, b)) < 18;     // grey-ish
  };

  /* ── 1. CSS custom properties (the real design tokens, when they exist) ── */
  const vars = {};
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; }      // cross-origin
    if (!rules) continue;
    for (const rule of rules) {
      if (!rule.style || !rule.selectorText) continue;
      if (!/^(:root|html|body)\b/.test(rule.selectorText)) continue;
      for (const prop of rule.style) {
        if (prop.startsWith('--')) vars[prop] = rule.style.getPropertyValue(prop).trim();
      }
    }
  }

  /* ── 2. Colors, weighted by how much of the page they actually cover ── */
  const bg = new Map(), fg = new Map(), br = new Map();
  const bump = (map, key, weight) => { if (key) map.set(key, (map.get(key) || 0) + weight); };

  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const area = Math.min(r.width * r.height, 900000);
    bump(bg, hex(cs.backgroundColor), area);
    bump(fg, hex(cs.color), (el.textContent || '').trim().length || 1);
    if (parseFloat(cs.borderTopWidth) > 0) bump(br, hex(cs.borderTopColor), area / 900);
  }

  const rank = (map, n = 8) => [...map.entries()]
    .sort((a, b) => b[1] - a[1]).slice(0, n)
    .map(([c, w]) => ({ color: c, weight: Math.round(w) }));

  /* ── 3. Accent: the most-used non-neutral color across any role ── */
  const accents = new Map();
  for (const [c, w] of [...bg, ...fg, ...br]) if (!isNeutral(c)) accents.set(c, (accents.get(c) || 0) + w);
  const accent = [...accents.entries()].sort((a, b) => b[1] - a[1])[0];

  /* ── 4. Type, by role ── */
  const face = sel => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const cs = getComputedStyle(el);
    return { family: cs.fontFamily, weight: cs.fontWeight, size: cs.fontSize,
             spacing: cs.letterSpacing, transform: cs.textTransform };
  };
  const fonts = {
    h1: face('h1'), h2: face('h2'),
    body: face('p') || face('body'),
    button: face('button') || face('a.button') || face('[class*=btn]'),
    nav: face('nav a') || face('header a'),
  };

  const families = new Set();
  for (const el of document.querySelectorAll('h1,h2,h3,h4,p,a,button,li,span')) {
    const f = getComputedStyle(el).fontFamily;
    if (f) families.add(f.split(',')[0].replace(/["']/g, '').trim());
  }

  const webfonts = [...document.querySelectorAll('link[rel=stylesheet],link[rel=preload]')]
    .map(l => l.href).filter(h => /font|typekit|typography|fonts\./i.test(h));

  /* ── 5. Logo candidates ── */
  const logos = [...document.querySelectorAll('header img, [class*=logo] img, img[class*=logo], img[alt*=logo i], header svg, [class*=logo] svg')]
    .slice(0, 6).map(n => n.tagName === 'IMG'
      ? { type: 'img', src: n.currentSrc || n.src, alt: n.alt }
      : { type: 'svg', markup: n.outerHTML.slice(0, 400) });

  /* ── 6. Assemble ── */
  const out = {
    _source: location.href,
    _capturedAt: new Date().toISOString(),
    cssVariables: Object.keys(vars).length ? vars : '(none exposed — theme is hardcoded or cross-origin)',
    backgrounds: rank(bg),
    textColors: rank(fg),
    borderColors: rank(br, 5),
    likelyAccent: accent ? accent[0] : null,
    fontsByRole: fonts,
    allFamilies: [...families],
    webfontStylesheets: webfonts,
    logos,
    /* Paste-ready starting point — verify against the ranked lists above
       before trusting it. Frequency is a proxy for brand, not brand itself. */
    themeDraft: {
      ground:  (rank(bg)[0] || {}).color || null,
      surface: (rank(bg)[1] || {}).color || null,
      ink:     (rank(fg)[0] || {}).color || null,
      muted:   (rank(fg)[1] || {}).color || null,
      line:    (rank(br, 5)[0] || {}).color || null,
      accent:  accent ? accent[0] : null,
      fontDisplay: fonts.h1 ? fonts.h1.family : null,
      fontBody:    fonts.body ? fonts.body.family : null,
      fontImport:  webfonts[0] || null,
      logoUrl:     (logos.find(l => l.type === 'img') || {}).src || null,
    },
  };

  const json = JSON.stringify(out, null, 2);
  console.log(out);
  try { copy(json); console.log('%c✓ Copied to clipboard — paste it back to Claude.',
    'color:#2E7D5B;font-weight:600'); }
  catch { console.log('%cSelect the JSON below and copy it manually:',
    'color:#B07A2A;font-weight:600'); console.log(json); }
  return out;
})();
