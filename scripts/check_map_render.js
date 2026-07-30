/* Render gate for map.html (EXPLORER_STEPS.md E5).
 *
 * Everything upstream of drawing is already covered by check_fields.py. What no
 * Python gate can see is whether the page draws the field the right way up: a
 * north-up and a south-up render are both entirely plausible pictures, and a
 * half-turn in longitude looks like weather. This loads the real page in
 * Chromium and asserts orientation from three independent directions —
 * the decoded array, the overlay bounds Leaflet was given, and the pixels
 * actually rasterised into the overlay.
 *
 *   node scripts/check_map_render.js [--keep-shot]
 */

const http = require("http");
const fs = require("fs");
const path = require("path");
const puppeteer = require("puppeteer");

const ROOT = path.join(__dirname, "..", "docs");
const PORT = 8749;
const SHOT = "/tmp/map-render.png";

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
               ".json": "application/json", ".png": "image/png" };

let failures = 0;
const fail = (m) => { console.error("FAIL " + m); failures++; };
const ok = (m) => console.log("ok   " + m);

function serve() {
  return http.createServer((req, res) => {
    const p = path.join(ROOT, decodeURIComponent(req.url.split("?")[0]));
    if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) {
      res.writeHead(404); return res.end("nope");
    }
    res.writeHead(200, { "Content-Type": MIME[path.extname(p)] || "application/octet-stream" });
    fs.createReadStream(p).pipe(res);
  }).listen(PORT);
}

(async () => {
  const server = serve();
  const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 1000 });

  // Chromium asks for /favicon.ico on its own; the page never references it, so
  // its 404 is noise. Everything else counts.
  const IGNORE = /favicon\.ico/;
  const errors = [], warnings = [];
  let pendingConsole = 0;
  page.on("console", (m) => {
    if (m.type() === "warning") warnings.push(m.text());
    if (m.type() !== "error") return;
    // "Failed to load resource" console lines carry no URL; the response
    // listener below is what decides whether they were real.
    if (/Failed to load resource/.test(m.text())) { pendingConsole++; return; }
    errors.push(m.text());
  });
  page.on("response", (r) => {
    if (r.status() >= 400 && !IGNORE.test(r.url())) errors.push(`HTTP ${r.status()} ${r.url()}`);
  });
  page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
  page.on("requestfailed", (r) => {
    if (!IGNORE.test(r.url())) errors.push(`request failed: ${r.url()}`);
  });

  await page.goto(`http://localhost:${PORT}/map.html`, { waitUntil: "networkidle0" });
  await page.evaluate(() => window.__mapReady);
  await new Promise((r) => setTimeout(r, 600));

  // --- 1. the page loaded at all --------------------------------------------
  const okFlag = await page.evaluate(() => window.__mapOK === true);
  if (!okFlag) fail("map did not finish booting: " + await page.evaluate(() => window.__mapError || "?"));
  else ok("page booted");

  if (errors.length) errors.forEach((e) => fail("console: " + e));
  else ok("no console errors or failed requests");

  // Every page is full of em-dashes and middots. GitHub Pages happens to send
  // charset=utf-8, which hid the absence of a <meta charset> until a local
  // server rendered "Â·" and "â€"" — so assert the tag rather than the host.
  for (const name of ["index.html", "compare.html", "map.html"]) {
    const src = fs.readFileSync(path.join(ROOT, name), "utf8");
    if (!/<meta\s+charset=/i.test(src)) fail(`${name}: no <meta charset> — non-ASCII will mojibake off GitHub Pages`);
  }
  ok("all pages declare a charset");

  const state = await page.evaluate(() => ({
    grid: window.__S.grid, variable: window.__S.variable, kind: window.__S.kind,
    lead: window.__S.f.leads[window.__S.leadIdx], models: window.__S.f.models,
  }));
  console.log(`     showing ${state.variable} ${state.kind} +${state.lead}h, ` +
              `grid ${state.grid.width}x${state.grid.height} @ ${state.grid.resolution_deg}deg`);

  // --- 2. orientation from the decoded array --------------------------------
  // t2m: the pole must be colder than the equator. If the field were flipped or
  // rolled, this inverts or collapses.
  if (state.variable === "t2m" && state.kind === "forecast") {
    const s = await page.evaluate(() => ({
      npole: window.__sampleAt(89, 0), spole: window.__sampleAt(-89, 0),
      eq: window.__sampleAt(0, 0), eq2: window.__sampleAt(0, 180),
    }));
    console.log(`     sampled  N-pole ${s.npole.toFixed(1)}  equator ${s.eq.toFixed(1)}  ` +
                `S-pole ${s.spole.toFixed(1)}  equator@180 ${s.eq2.toFixed(1)} K`);
    if (!(s.eq > s.npole + 5)) fail(`equator (${s.eq.toFixed(1)}) not warmer than N pole (${s.npole.toFixed(1)}) — field may be flipped`);
    else ok("decoded array: equator warmer than the pole");
    if (!(s.eq > s.spole + 5)) fail(`equator not warmer than S pole — field may be flipped`);
    else ok("decoded array: equator warmer than the south pole too");
  }

  // --- 3. the bounds Leaflet was actually given -----------------------------
  const b = await page.evaluate(() => window.__overlayBounds());
  if (!b) fail("no image overlay on the map");
  else {
    const g = state.grid;
    const wantN = g.lat_start;
    const wantS = g.lat_start + g.lat_step * (g.height - 1);
    if (Math.abs(b.north - wantN) > 1e-6) fail(`overlay north ${b.north} != grid lat_start ${wantN}`);
    else if (Math.abs(b.south - wantS) > 1e-6) fail(`overlay south ${b.south} != ${wantS}`);
    else ok(`overlay bounds match the manifest grid (N ${b.north}, S ${b.south})`);
    if (Math.abs((b.east - b.west) - 360) > 1e-6) fail(`overlay spans ${(b.east - b.west).toFixed(3)}deg, not 360`);
    else ok("overlay spans a full 360deg of longitude");
    // The grid is numbered 0..360 east of Greenwich but Leaflet, the coastlines
    // and maxBounds are all -180..180. An overlay left at 0..360 leaves the
    // western hemisphere with no field beneath it.
    if (Math.abs(b.west + 180) > 1e-6) fail(`overlay west is ${b.west}, not -180 — the western hemisphere would be blank`);
    else ok("overlay is anchored at -180, aligned with the coastlines");
  }

  // Opacity across longitude: every column of the rasterised overlay must carry
  // data. A rolled-vs-unrolled mistake shows up as a fully transparent half.
  const cover = await page.evaluate(() => {
    const im = document.querySelector(".leaflet-image-layer");
    if (!im) return null;
    const c = document.createElement("canvas");
    c.width = im.naturalWidth; c.height = im.naturalHeight;
    c.getContext("2d").drawImage(im, 0, 0);
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    let empty = 0;
    for (let x = 0; x < c.width; x++) {
      let any = false;
      for (let y = 0; y < c.height; y++) if (d[((y * c.width + x) * 4) + 3] !== 0) { any = true; break; }
      if (!any) empty++;
    }
    return { empty, width: c.width };
  });
  if (!cover) fail("no overlay to measure coverage on");
  else if (cover.empty) fail(`${cover.empty}/${cover.width} longitude columns are fully transparent`);
  else ok(`all ${cover.width} longitude columns carry field data`);

  // --- 4. the pixels that were rasterised -----------------------------------
  // Row 0 of the canvas must correspond to lat_start. Compare the mean
  // luminance of the top row against the middle row: for a sequential ramp on
  // t2m, cold poles are dark and the warm tropics bright.
  if (state.variable === "t2m" && state.kind === "forecast") {
    const px = await page.evaluate(async () => {
      const img = document.querySelector(".leaflet-image-layer");
      if (!img) return null;
      const c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0);
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      const rowLum = (r) => {
        let s = 0, n = 0;
        for (let x = 0; x < c.width; x++) {
          const p = (r * c.width + x) * 4;
          if (d[p + 3] === 0) continue;
          s += 0.2126 * d[p] + 0.7152 * d[p + 1] + 0.0722 * d[p + 2]; n++;
        }
        return n ? s / n : NaN;
      };
      return { top: rowLum(0), mid: rowLum(Math.floor(c.height / 2)),
               bot: rowLum(c.height - 1), w: c.width, h: c.height };
    });
    if (!px) fail("could not read the overlay image");
    else {
      console.log(`     overlay ${px.w}x${px.h}  luminance top ${px.top.toFixed(1)} ` +
                  `mid ${px.mid.toFixed(1)} bot ${px.bot.toFixed(1)}`);
      if (!(px.mid > px.top && px.mid > px.bot))
        fail(`rasterised rows: middle (${px.mid.toFixed(1)}) is not brighter than both ` +
             `edges (${px.top.toFixed(1)}/${px.bot.toFixed(1)}) — the drawn field is not warm-in-the-middle`);
      else ok("rasterised pixels: tropics brighter than both poles");
    }
  }

  // --- 4b. the drawn pixel at a place matches the value at that place -------
  // Everything above still passes if the longitude roll goes the wrong way and
  // Asia's field is painted over the Americas: the bounds are right, every
  // column has data, and the poles are still cold. This is the assertion that
  // ties geography to the pixel — for each point, the colour actually drawn is
  // compared against the colormap applied to the value sampled from the
  // undrawn array (whose lon arithmetic was verified against the zarr).
  const align = await page.evaluate(() => {
    const im = document.querySelector(".leaflet-image-layer");
    if (!im) return null;
    const c = document.createElement("canvas");
    c.width = im.naturalWidth; c.height = im.naturalHeight;
    c.getContext("2d").drawImage(im, 0, 0);
    const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
    const pts = [[20, 10], [20, -160], [0, 100], [0, -60], [-30, 140], [50, -100], [60, 30]];
    return pts.map(([lat, lon]) => {
      const x = Math.round(((lon + 180) / 360) * c.width) % c.width;
      const y = Math.round(((90 - lat) / 180) * (c.height - 1));
      const p = (y * c.width + x) * 4;
      const got = [d[p], d[p + 1], d[p + 2]];
      const want = window.__S.colour(window.__sampleAt(lat, lon));
      return { lat, lon, got, want,
               dist: Math.max(...got.map((g, i) => Math.abs(g - want[i]))) };
    });
  });
  if (!align) fail("no overlay to check alignment on");
  else {
    const bad = align.filter((a) => a.dist > 12);
    align.forEach((a) => console.log(
      `     ${String(a.lat).padStart(3)},${String(a.lon).padStart(5)}  drawn ${a.got.join(",")}  ` +
      `expected ${a.want.join(",")}  d=${a.dist}`));
    if (bad.length) bad.forEach((a) => fail(
      `field is drawn at the wrong longitude: at ${a.lat},${a.lon} the pixel is ` +
      `[${a.got}] but the value there implies [${a.want}]`));
    else ok(`drawn pixels match the sampled values at all ${align.length} probe points`);
  }

  // --- 5. error view renders and is symmetric -------------------------------
  await page.evaluate(() => {
    document.querySelector('#viewtabs button[data-kind="error"]').click();
  });
  await new Promise((r) => setTimeout(r, 900));
  const errState = await page.evaluate(() => ({
    kind: window.__S.kind,
    ticks: [...document.querySelectorAll("#cbarticks span")].map((s) => Number(s.textContent)),
  }));
  if (errState.kind !== "error") fail("error view did not activate");
  else {
    const t = errState.ticks;
    if (t.length < 3) fail("colorbar has no ticks in error view");
    else if (Math.abs(t[Math.floor(t.length / 2)]) > 1e-9)
      fail(`error colorbar midpoint is ${t[Math.floor(t.length / 2)]}, not 0 — the neutral colour would misstate the sign of a bias`);
    else if (Math.abs(Math.abs(t[0]) - Math.abs(t[t.length - 1])) > 1e-6)
      fail(`error colorbar is asymmetric: ${t[0]} .. ${t[t.length - 1]}`);
    else ok(`error colorbar symmetric about zero (${t[0]} .. ${t[t.length - 1]})`);
  }

  // --- 6. side-by-side keeps the panes locked together ----------------------
  await page.evaluate(() => document.querySelector('#paneltabs button[data-panes="2"]').click());
  await new Promise((r) => setTimeout(r, 900));
  const sync = await page.evaluate(async () => {
    window.__S.maps.map1.setView([40, 60], 3, { animate: false });
    await new Promise((r) => setTimeout(r, 350));
    const a = window.__S.maps.map1.getCenter(), b = window.__S.maps.map2.getCenter();
    return { a: [a.lat, a.lng], b: [b.lat, b.lng],
             za: window.__S.maps.map1.getZoom(), zb: window.__S.maps.map2.getZoom() };
  });
  if (Math.abs(sync.a[0] - sync.b[0]) > 0.5 || Math.abs(sync.a[1] - sync.b[1]) > 0.5 || sync.za !== sync.zb)
    fail(`panes not linked: ${JSON.stringify(sync)}`);
  else ok(`side-by-side panes stay locked (both at ${sync.a[0].toFixed(1)}, ${sync.a[1].toFixed(1)} z${sync.za})`);

  // --- 6b. the field must fill the viewport, in every layout ---------------
  // A "fit" zoom letterboxes a 2:1 world into a non-2:1 window, and panning
  // past +-180 leaves coastlines drawn over blank page — which reads as a
  // monochrome repeat of the map. Assert the view never extends beyond the
  // world, at minimum zoom, in both layouts.
  for (const panes of ["1", "2"]) {
    await page.evaluate((p) => document.querySelector(`#paneltabs button[data-panes="${p}"]`).click(), panes);
    await new Promise((r) => setTimeout(r, 700));
    const v = await page.evaluate(() => {
      const m = window.__S.maps.map1;
      m.setZoom(m.getMinZoom());
      const b = m.getBounds();
      return { n: b.getNorth(), s: b.getSouth(), w: b.getWest(), e: b.getEast(),
               z: m.getZoom(), min: m.getMinZoom() };
    });
    const eps = 1e-6;
    const out = [];
    if (v.n > 90 + eps) out.push(`north ${v.n.toFixed(2)} > 90`);
    if (v.s < -90 - eps) out.push(`south ${v.s.toFixed(2)} < -90`);
    if (v.w < -180 - eps) out.push(`west ${v.w.toFixed(2)} < -180`);
    if (v.e > 180 + eps) out.push(`east ${v.e.toFixed(2)} > 180`);
    if (out.length) fail(`${panes}-pane at min zoom shows blank beyond the world: ${out.join(", ")}`);
    else ok(`${panes}-pane: field covers the viewport at min zoom (z${v.min})`);
  }
  await page.evaluate(() => document.querySelector('#paneltabs button[data-panes="2"]').click());
  await new Promise((r) => setTimeout(r, 700));

  // --- 7. floating chrome must not cover the pane labels -------------------
  // On a full-bleed map everything is absolutely positioned, so nothing throws
  // when two panels land on top of each other — the label simply vanishes.
  // Screenshots caught this once; assert it instead of relying on noticing.
  const overlaps = await page.evaluate(() => {
    const r = (sel) => { const e = document.querySelector(sel); if (!e || e.hidden) return null;
      const b = e.getBoundingClientRect(); return b.width && b.height ? b : null; };
    const hit = (a, b) => a && b && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const chrome = { nav: r(".topbar"), panel: r(".ctlpanel"), bottom: r(".bottombar") };
    const out = [];
    for (const tag of ["#t1", "#t2"]) {
      const t = r(tag);
      if (!t) { if (tag === "#t1") out.push(`${tag} is not visible at all`); continue; }
      for (const [name, c] of Object.entries(chrome))
        if (hit(t, c)) out.push(`${tag} overlaps .${name}`);
    }
    return out;
  });
  if (overlaps.length) overlaps.forEach((o) => fail("layout: " + o));
  else ok("pane labels clear of the nav, control panel and bottom bar");

  await page.screenshot({ path: SHOT, fullPage: false });
  console.log(`     screenshot -> ${SHOT}`);
  if (warnings.length) console.log(`     (${warnings.length} console warning(s))`);

  await browser.close();
  server.close();
  console.log(failures ? `\n${failures} FAILURE(S)` : "\nall render checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("gate crashed:", e); process.exit(2); });
