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
  // its 404 is noise. Basemap tiles are ignored too, from section 10 onwards:
  // they are third-party WMS requests, and a gate that fails when someone else's
  // server is slow or this machine is offline is a gate that gets ignored. What
  // section 10 asserts instead is that the *request* is built correctly, which
  // is the part this repo can actually be wrong about.
  const basemapHosts = ((JSON.parse(
    fs.readFileSync(path.join(ROOT, "data", "manifest.json"), "utf8")).map || {}
  ).basemaps || []).map((b) => new URL(b.url).host);
  const IGNORE = new RegExp(
    ["favicon\\.ico", ...basemapHosts.map((h) => h.replace(/\./g, "\\."))].join("|"));
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
  const requests = [];
  page.on("request", (r) => requests.push(r.url()));
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
    const im = document.querySelector(".fieldcopy-primary");
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
  // Row 0 of the canvas must correspond to lat_start: the tropics have to end
  // up in the middle of the image and the poles at its edges.
  //
  // The proxy for "warm" is red-minus-blue, not luminance. It used to be
  // luminance, which worked only because viridis happens to be monotonic in
  // lightness (dark violet -> bright yellow). The t2m ramp is now the
  // conventional meteorological one, whose lightness *peaks* in the pale band
  // near freezing and falls off toward both the violet and the red end — so a
  // correctly drawn field fails a brightness test. R-B rises monotonically
  // across both ramps, so it tests the orientation rather than the palette.
  if (state.variable === "t2m" && state.kind === "forecast") {
    const px = await page.evaluate(async () => {
      const img = document.querySelector(".fieldcopy-primary");
      if (!img) return null;
      const c = document.createElement("canvas");
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      c.getContext("2d").drawImage(img, 0, 0);
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      const rowWarmth = (r) => {
        let s = 0, n = 0;
        for (let x = 0; x < c.width; x++) {
          const p = (r * c.width + x) * 4;
          if (d[p + 3] === 0) continue;
          s += d[p] - d[p + 2]; n++;          // red minus blue
        }
        return n ? s / n : NaN;
      };
      return { top: rowWarmth(0), mid: rowWarmth(Math.floor(c.height / 2)),
               bot: rowWarmth(c.height - 1), w: c.width, h: c.height };
    });
    if (!px) fail("could not read the overlay image");
    else {
      console.log(`     overlay ${px.w}x${px.h}  warmth(R-B) top ${px.top.toFixed(1)} ` +
                  `mid ${px.mid.toFixed(1)} bot ${px.bot.toFixed(1)}`);
      if (!(px.mid > px.top && px.mid > px.bot))
        fail(`rasterised rows: middle (${px.mid.toFixed(1)}) is not warmer than both ` +
             `edges (${px.top.toFixed(1)}/${px.bot.toFixed(1)}) — the drawn field is not warm-in-the-middle`);
      else ok("rasterised pixels: tropics warmer than both poles");
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
    const im = document.querySelector(".fieldcopy-primary");
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
  // Checked at four widths, not one. The bottom bar is a wrapping flex row, so
  // its height is a function of viewport width — it takes three rows below
  // ~520px and reaches 130px, which used to swallow a pane label pinned at a
  // fixed 92px. Testing only at 1280px is exactly why that shipped.
  const CHROME = { nav: ".topbar", panel: ".ctlpanel", bottom: ".bottombar" };
  for (const w of [1280, 800, 480, 380]) {
    await page.setViewport({ width: w, height: 1000 });
    await new Promise((r) => setTimeout(r, 600));
    // Below the breakpoint the control panel is a full-width overlay the reader
    // opens over the map, so it is not something a label can be "clear of".
    const against = Object.entries(CHROME).filter(([n]) => w > 820 || n !== "panel");
    const overlaps = await page.evaluate((sels) => {
      const r = (sel) => {
        const e = document.querySelector(sel);
        if (!e || e.hidden || getComputedStyle(e).display === "none") return null;
        const b = e.getBoundingClientRect();
        return b.width && b.height ? b : null;
      };
      const hit = (a, b) => a && b && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
      const out = [];
      for (const tag of ["#t1", "#t2"]) {
        const t = r(tag);
        if (!t) { if (tag === "#t1") out.push(`${tag} is not visible at all`); continue; }
        if (t.top < 0 || t.bottom > innerHeight) out.push(`${tag} is off screen`);
        for (const [name, sel] of sels) if (hit(t, r(sel))) out.push(`${tag} overlaps .${name}`);
      }
      return out;
    }, against);
    if (overlaps.length) overlaps.forEach((o) => fail(`layout at ${w}px: ${o}`));
    else ok(`${w}px: pane labels clear of the nav, panel and bottom bar`);
  }
  await page.setViewport({ width: 1280, height: 1000 });
  await new Promise((r) => setTimeout(r, 600));

  // --- 8. stretch-to-view scaling ------------------------------------------
  // Two things can go wrong here and neither is visible in a screenshot: the
  // stretched scale can be computed from the wrong cells (the grid runs 0..360
  // and the bounds run -180..180, so an unfolded longitude samples the
  // antipodes and still returns plausible numbers), and the two panes can end
  // up on different scales, which makes a side-by-side comparison of two
  // near-identical fields look like a large disagreement.
  const setView = (lat, lon, z) =>
    page.evaluate((a, o, zz) => window.__S.maps.map1.setView([a, o], zz, { animate: false }),
                  lat, lon, z);

  // Set up explicitly rather than inheriting: earlier sections leave the page
  // on the error view, whose scale is symmetrised about zero and so cannot show
  // a warm/cold difference between two views at all.
  await page.evaluate(() => {
    document.querySelector('#viewtabs button[data-kind=forecast]').click();
    document.querySelector('#paneltabs button[data-panes="1"]').click();
  });
  await new Promise((r) => setTimeout(r, 700));
  await setView(50, 10, 5);
  await new Promise((r) => setTimeout(r, 500));
  const globalScale = await page.evaluate(() => window.__S.shownScale);

  await page.evaluate(() => document.querySelector('#scaletabs button[data-scale=stretch]').click());
  await new Promise((r) => setTimeout(r, 800));
  const europe = await page.evaluate(() => window.__S.shownScale);
  if (!(europe[0] > globalScale[0] && europe[1] < globalScale[1]))
    fail(`stretch over Europe (${europe}) did not narrow the global scale (${globalScale})`);
  else ok(`stretch narrows the scale to the view (${europe[0].toFixed(1)}..${europe[1].toFixed(1)} vs global ${globalScale[0].toFixed(1)}..${globalScale[1].toFixed(1)})`);

  // Panning must move the scale. If the longitude fold were wrong this would
  // still change, so the real check is that the tropics come out *warmer*.
  await setView(0, 20, 5);
  await new Promise((r) => setTimeout(r, 900));
  const tropics = await page.evaluate(() => window.__S.shownScale);
  if (!(tropics[0] > europe[0]))
    fail(`stretch over the tropics (${tropics}) is not warmer than over Europe (${europe}) — check the 0..360 longitude fold in extentInBounds`);
  else ok(`stretch follows the view and samples the right cells (tropics ${tropics[0].toFixed(1)}.. vs Europe ${europe[0].toFixed(1)}..)`);

  await page.evaluate(() => document.querySelector('#paneltabs button[data-panes="2"]').click());
  await new Promise((r) => setTimeout(r, 900));
  const shared = await page.evaluate(() => {
    const c = window.__S.colour;
    // One colour function is handed to both panes, so identity is the check.
    return { scale: window.__S.shownScale, same: window.__S.layers.map1 && window.__S.layers.map2 ? true : false, c: !!c };
  });
  if (!shared.same) fail("side-by-side: one of the two panes has no field layer");
  else ok(`side-by-side panes share one stretched scale (${shared.scale[0].toFixed(1)}..${shared.scale[1].toFixed(1)})`);

  // Error views must stay symmetric about zero even when stretched, or the
  // colourbar's neutral grey stops meaning "no error".
  await page.evaluate(() => document.querySelector('#viewtabs button[data-kind=error]').click());
  await new Promise((r) => setTimeout(r, 900));
  const err = await page.evaluate(() => window.__S.shownScale);
  if (Math.abs(err[0] + err[1]) > 1e-6)
    fail(`stretched error scale is not symmetric about zero: ${err[0]} .. ${err[1]}`);
  else ok(`stretched error scale stays symmetric about zero (±${err[1].toFixed(2)})`);

  await page.evaluate(() => {
    document.querySelector('#viewtabs button[data-kind=forecast]').click();
    document.querySelector('#scaletabs button[data-scale=global]').click();
    document.querySelector('#paneltabs button[data-panes="1"]').click();
  });
  await new Promise((r) => setTimeout(r, 700));

  // --- 9. longitude wrapping ------------------------------------------------
  // Panning past the antimeridian must stay continuous. The failure this
  // catches is not an exception — it is blank page appearing at the edge of the
  // outermost world copy, which looks like the map simply ending. Assert that
  // every column of the viewport sits under some copy of the field, at several
  // points along a long westward pan.
  const dragWest = async () => {
    await page.mouse.move(640, 500);
    await page.mouse.down();
    for (let i = 1; i <= 10; i++) await page.mouse.move(640 + 60 * i, 500);
    await page.mouse.up();
    await new Promise((r) => setTimeout(r, 350));
  };
  const coverage = () => page.evaluate(() => {
    const m = window.__S.maps.map1;
    const rects = [...document.querySelectorAll(".fieldcopy")].map((e) => e.getBoundingClientRect());
    const view = m.getContainer().getBoundingClientRect();
    let covered = 0, total = 0;
    for (let x = view.left + 2; x < view.right - 2; x += 20) {
      total++;
      if (rects.some((r) => x >= r.left && x <= r.right)) covered++;
    }
    return { covered, total, lng: m.getCenter().lng, copies: rects.length };
  });

  let wrapOK = true, travelled = 0;
  const start = (await coverage()).lng;
  for (let i = 0; i < 6; i++) {
    await dragWest();
    const c = await coverage();
    travelled = Math.abs(c.lng - start);
    if (c.covered < c.total) {
      wrapOK = false;
      fail(`panning west left ${c.total - c.covered}/${c.total} viewport columns off the ` +
           `field at centre lon ${c.lng.toFixed(1)} — the world copies ran out`);
      break;
    }
  }
  if (wrapOK) ok("panning west stays on the field across the antimeridian (6 drags, no gaps)");

  // A centre set beyond +-180 must FOLD to the equivalent longitude, not be
  // clamped at the antimeridian. The distinction is the whole fix: clamping is
  // the old wall, and leaving it unfolded is how a view ends up past the
  // outermost world copy, staring at blank page.
  //
  // Every one of these is the same place on Earth. If any renders differently
  // the copies have run out, which is what a reader who keeps dragging one way
  // eventually does.
  for (const lon of [300, -100 + 360, -100 + 720, -100 - 720]) {
    const got = await page.evaluate((L) => {
      window.__S.maps.map1.setView([38, L], 3, { animate: false });
      return window.__S.maps.map1.getCenter().lng;
    }, lon);
    const wrapped = ((lon + 180) % 360 + 360) % 360 - 180;
    if (Math.abs(got - 180) < 1e-6 || Math.abs(got - (-180)) < 1e-6)
      fail(`centre set to ${lon} was clamped to ${got} — longitude is walled again`);
    else if (Math.abs(got - wrapped) > 1e-3)
      fail(`centre set to ${lon} became ${got.toFixed(1)}, expected the equivalent ${wrapped.toFixed(1)}`);
    else {
      const c = await coverage();
      if (c.covered < c.total)
        fail(`at lon ${lon} (folded to ${got.toFixed(1)}) ${c.total - c.covered}/${c.total} ` +
             `viewport columns are off the field — the world copies ran out`);
    }
  }
  ok("centres beyond +-180 fold to the equivalent longitude and stay on the field");

  // Sampling has to be wrap-invariant, or the readout and the stretched scale
  // report the antipodes once the user crosses the dateline.
  const wrapSample = await page.evaluate(() => ({
    a: window.__sampleAt(20, 190), b: window.__sampleAt(20, -170),
    c: window.__sampleAt(20, 550),
  }));
  if (!(wrapSample.a === wrapSample.b && wrapSample.a === wrapSample.c))
    fail(`sampling is not wrap-invariant: lon 190 -> ${wrapSample.a}, ` +
         `-170 -> ${wrapSample.b}, 550 -> ${wrapSample.c}`);
  else ok("sampling is wrap-invariant across world copies");

  await page.evaluate(() => window.__S.maps.map1.setView([0, 0], 3, { animate: false }));
  await new Promise((r) => setTimeout(r, 400));

  // --- 10. city popups ------------------------------------------------------
  // The point-store popup is the one thing on this page whose numbers do not
  // come from the raster, so nothing else in this file would notice if it broke.
  // Three things have gone wrong here already and each is asserted: reading the
  // popup's open state off L.Map (which has no isPopupOpen and throws), leaving
  // a stale popup pinned while the lead moves under it, and applying the
  // absolute unit conversion to the error column.
  // The click is a real mouse click at the marker's screen position, not a
  // dispatched MouseEvent. Leaflet's _findEventTargets drops a click outright
  // while map.dragging.moved() is still latched — and section 9's drags leave it
  // latched, since only a fresh mousedown clears it. A synthetic event skips the
  // mousedown and is silently discarded; a real click is also what a reader does.
  const dot = await page.evaluate(async () => {
    const S = window.__S;
    S.maps.map1.setView([51.5, -0.13], 5, { animate: false });
    await new Promise((r) => setTimeout(r, 400));
    // Nearest marker to the view centre — London, since it is a sampled city.
    const c = S.maps.map1.latLngToContainerPoint(S.maps.map1.getCenter());
    let best = null, bd = 1e9;
    for (const d of S.maps.map1.getContainer().querySelectorAll(".citylabel")) {
      const r = d.getBoundingClientRect();
      const dist = Math.hypot(r.left - c.x, r.top - c.y);
      if (dist < bd) { bd = dist; best = d; }
    }
    if (!best) return null;
    const r = best.querySelector(".dot").getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width };
  });
  if (!dot) fail("no city markers on the map");
  else if (dot.w < 6) fail(`city dots are ${dot.w}px across — too small to be a click target`);
  if (dot) await page.mouse.click(dot.x, dot.y);
  await new Promise((r) => setTimeout(r, 900));

  const pop = await page.evaluate(async () => {
    const box = document.querySelector(".leaflet-popup-content");
    if (!box) return { err: "clicking a city marker opened no popup" };
    const head = box.querySelector(".pophead b").textContent;
    const rows = [...box.querySelectorAll(".poptbl tr")].map((tr) => ({
      name: tr.querySelector("td.name").textContent,
      val: tr.querySelector("td.val").textContent,
      err: (tr.querySelector("td.err") || {}).textContent || "",
      truth: tr.classList.contains("truth"),
    }));
    const link = box.querySelector(".popfoot a");
    return { head, rows, href: link ? link.getAttribute("href") : null,
             sub: box.querySelector(".pophead span").textContent };
  });
  if (pop.err) fail(pop.err);
  else {
    const city = (await page.evaluate(() => window.__S.manifest.cities))
      .find((c) => c.name === pop.head);
    if (!city) fail(`popup title "${pop.head}" is not one of the sampled cities`);
    else ok(`city marker opens a popup for ${pop.head} (${pop.rows.length} row(s))`);
    const models = pop.rows.filter((r) => !r.truth);
    if (models.length < 2)
      fail(`popup lists ${models.length} model(s) — the point store has every model at this init, ` +
           "so this is reading the raster instead of points/<init>/<city>.json");
    else ok(`popup lists ${models.length} models from the point store, not one sampled pixel`);

    const truth = pop.rows.find((r) => r.truth);
    if (truth) {
      // The error column is a *difference*, so it takes units.js's delta
      // conversion. Applying the absolute one would make a 2 K miss read as
      // -271 °C — a number that is wrong by 273 and looks like weather.
      const bad = models.filter((r) => {
        const e = Number(r.err.replace("−", "-"));
        return !Number.isFinite(e) || Math.abs(e) > 60;
      });
      if (bad.length)
        fail(`popup error column is not a delta conversion: ${bad.map((b) => `${b.name} ${b.err}`).join(", ")}`);
      else ok(`popup error column is a signed delta against ${truth.name.trim()}`);
      // Ranked by |error| is the whole reason to show every model at once.
      const errs = models.map((r) => Math.abs(Number(r.err.replace("−", "-"))));
      if (errs.some((e, i) => i && e < errs[i - 1] - 1e-9))
        fail(`popup models are not ranked by |error|: ${errs.join(", ")}`);
      else ok("popup ranks models by absolute error, best first");
    }
    if (!pop.href || !/^compare\.html\?city=/.test(pop.href))
      fail(`popup link is ${pop.href}, not a compare.html?city= deep link`);
    else ok(`popup links through to ${pop.href}`);
  }

  // The popup has to follow the lead slider. A stale popup is worse than a
  // closed one: nothing about the wrong number looks wrong.
  const moved = await page.evaluate(async () => {
    const before = document.querySelector(".pophead span").textContent;
    const s = document.getElementById("leadslider");
    s.value = String(Math.min(3, Number(s.max)));
    s.dispatchEvent(new Event("input"));
    await new Promise((r) => setTimeout(r, 1200));
    const box = document.querySelector(".leaflet-popup-content");
    return { before, after: box ? box.querySelector(".pophead span").textContent : null,
             lead: window.__S.f.leads[window.__S.leadIdx] };
  });
  if (!moved.after) fail("the popup vanished when the lead changed");
  else if (moved.after === moved.before)
    fail(`popup still reads "${moved.before}" at +${moved.lead} h — it is not tracking the lead slider`);
  else ok(`popup follows the lead slider (${moved.before.split("·")[1].trim()} -> ${moved.after.split("·")[1].trim()})`);

  // Clicking open water gets the raster instead, and must say so rather than
  // silently showing nothing.
  const water = await page.evaluate(async () => {
    window.__S.maps.map1.fire("click", { latlng: L.latLng(-30, -20) });
    await new Promise((r) => setTimeout(r, 400));
    const box = document.querySelector(".leaflet-popup-content");
    return box ? box.innerText : null;
  });
  if (!water || !/°/.test(water)) fail(`clicking open map gave no value: ${JSON.stringify(water)}`);
  else ok("clicking anywhere reads the field at that point");
  await page.evaluate(() => window.__S.maps.map1.closePopup());

  // --- 11. basemaps ---------------------------------------------------------
  // The whole point of the WMS detour is EPSG:4326. A Web Mercator tile source
  // would look almost right at low zoom and drift further from the field the
  // further north you go, which is the kind of wrong that ships.
  const cfg = await page.evaluate(() => window.__S.mapcfg);
  const maxZoom = await page.evaluate(() => window.__S.maps.map1.getMaxZoom());
  if (maxZoom !== cfg.max_zoom) fail(`map maxZoom is ${maxZoom}, manifest says ${cfg.max_zoom}`);
  else ok(`map honours the configured max zoom (${maxZoom})`);

  // Nothing may be fetched off-origin until a basemap is switched on. Sections
  // 1-10 have already driven the whole page, so anything third-party would have
  // shown up by now.
  const offOrigin = requests.filter(
    (u) => !u.startsWith(`http://localhost:${PORT}`) && !u.startsWith("data:"));
  if (offOrigin.length)
    fail(`the page made ${offOrigin.length} off-origin request(s) with the basemap off: ${offOrigin[0]}`);
  else ok("no third-party request until a basemap is asked for");

  if (!cfg.basemaps.length) ok("no basemaps configured — skipping the rest of section 11");
  else {
    for (const b of cfg.basemaps) {
      const st = await page.evaluate(async (id) => {
        document.querySelector(`#basetabs button[data-base="${id}"]`).click();
        window.__S.maps.map1.setView([51.5, -0.13], 8, { animate: false });
        await new Promise((r) => setTimeout(r, 400));
        const S = window.__S, m = S.maps.map1, layer = S.baseLayers.map1;
        // getTileUrl needs a real L.Point (it calls scaleBy on it); a plain
        // {x,y,z} throws, and the throw would be swallowed into a URL that
        // parses to nothing rather than a failure that names itself.
        let url = null;
        try {
          const tc = L.point(64, 42);
          tc.z = 8;
          url = layer.getTileUrl(tc);
        } catch (e) { url = "ERR " + e.message; }
        return {
          added: !!layer,
          url,
          pane: (layer && layer.options.pane) || "tilePane",
          opacity: S.fieldOpacity,
          geoOn: (S.geo.map1 || []).filter((g) => m.hasLayer(g)).length,
          attrib: (document.getElementById("attrib") || {}).textContent || "",
          labelsShown: getComputedStyle(document.querySelector(".citylabel span")).display,
          dotsShown: getComputedStyle(document.querySelector(".citylabel .dot")).display,
        };
      }, b.id);

      if (!st.added) { fail(`basemap ${b.id}: no layer was added`); continue; }
      // Leaflet uppercases WMS parameter names (getParamString(..., true)), and
      // WMS itself is case-insensitive about them, so read them case-blind
      // rather than pinning the assertion to Leaflet's current choice.
      const q = new Map([...new URLSearchParams((st.url || "").split("?")[1] || "")]
        .map(([k, v]) => [k.toLowerCase(), v]));
      if (q.get("srs") !== "EPSG:4326")
        fail(`basemap ${b.id} requests srs=${q.get("srs")}, not EPSG:4326 — a Web Mercator ` +
             "source cannot line up with an equirectangular field");
      else if (q.get("layers") !== b.layers)
        fail(`basemap ${b.id} requests layers=${q.get("layers")}, config says ${b.layers}`);
      else if ((q.get("bbox") || "").split(",").length !== 4)
        fail(`basemap ${b.id} has no 4-part bbox: ${q.get("bbox")}`);
      else ok(`basemap ${b.id}: WMS request is EPSG:4326 with layers=${b.layers}`);

      // Transparent layers go above the field at full strength; opaque ones go
      // below it and dim it. Getting this backwards hides one or the other.
      const wantPane = b.over ? "basetop" : "tilePane";
      const wantOpacity = b.over ? 1 : cfg.field_opacity;
      if (st.pane !== wantPane) fail(`basemap ${b.id} is in pane ${st.pane}, expected ${wantPane}`);
      else if (Math.abs(st.opacity - wantOpacity) > 1e-9)
        fail(`basemap ${b.id}: field opacity is ${st.opacity}, expected ${wantOpacity}`);
      else ok(`basemap ${b.id}: drawn ${b.over ? "over" : "under"} the field, field at ${st.opacity}`);

      // Using someone else's tiles without crediting them is a licence breach
      // that renders perfectly, so it has to be asserted rather than noticed.
      // Compared as rendered text: the config string is HTML, so it carries
      // both markup and entities and cannot be matched literally.
      const wantCredit = await page.evaluate((html) => {
        const d = document.createElement("div");
        d.innerHTML = html;
        return d.textContent.trim();
      }, b.attribution);
      if (!st.attrib.trim()) fail(`basemap ${b.id} renders with no attribution`);
      else if (st.attrib.trim() !== wantCredit)
        fail(`basemap ${b.id} credits "${st.attrib.trim()}", config says "${wantCredit}"`);
      else ok(`basemap ${b.id}: credited ("${st.attrib.trim().slice(0, 40)}...")`);

      // The vendored coastline is simplified to 0.02 deg, which is fifteen
      // pixels of stair-stepping at zoom 10 and sits on top of OSM's own.
      if (st.geoOn) fail(`basemap ${b.id}: ${st.geoOn} vendored geography layer(s) still drawn over it`);
      else ok(`basemap ${b.id}: vendored coastline and borders stood down`);
      if (st.labelsShown !== "none")
        fail(`basemap ${b.id}: our city names are still drawn beside the basemap's own`);
      else if (st.dotsShown === "none")
        fail(`basemap ${b.id}: the city dots vanished — they are the only way to reach a popup`);
      else ok(`basemap ${b.id}: names deferred to the basemap, sampling dots kept`);
    }

    // A basemap that fails must say so. Otherwise it is indistinguishable from a
    // button that did nothing: the tiles are third-party, requested lazily, and
    // a failed one leaves blank space rather than an error. Forced here by
    // pointing the layer at a host that cannot resolve, so the check does not
    // depend on the real service misbehaving.
    const failNote = await page.evaluate(async () => {
      const S = window.__S;
      S.basemapId = S.basemaps[0].id;
      const original = S.basemaps[0].url;
      S.basemaps[0].url = "https://tiles.nonexistent.invalid/wms";
      S.applyBasemap();
      S.maps.map1.setView([48.86, 2.35], 8, { animate: false });
      await new Promise((r) => setTimeout(r, 2500));
      const n = document.getElementById("basenote");
      const shown = n ? n.innerText : null;
      // Put it back and confirm the warning retracts once tiles load again.
      S.basemaps[0].url = original;
      S.applyBasemap();
      await new Promise((r) => setTimeout(r, 2500));
      return { shown, cleared: !document.getElementById("basenote"),
               stillHasField: !!(S.layers.map1 || []).length };
    });
    if (!failNote.shown)
      fail("a basemap whose tiles all fail shows no notice — indistinguishable from a dead button");
    else if (!/nonexistent\.invalid/.test(failNote.shown))
      fail(`the failure notice does not name the service that failed: ${failNote.shown}`);
    else if (!failNote.stillHasField)
      fail("a basemap failure took the forecast field down with it");
    else ok(`a failing basemap says so ("${failNote.shown.split(".")[0]}.")`);
    if (!failNote.cleared) fail("the failure notice did not retract once tiles loaded again");
    else ok("the failure notice retracts when the service recovers");

    // The credit is only credit if it is visible. It is absolutely positioned on
    // a page where everything else is too, so a collision hides it silently —
    // which is how the first attempt (a Leaflet control on map1) ended up under
    // pane 1's label in side-by-side. Checked in both layouts and at both
    // breakpoints, since the bottom bar and the panel move between them.
    for (const [w, h] of [[1280, 1000], [800, 900]]) {
      await page.setViewport({ width: w, height: h });
      for (const panes of ["1", "2"]) {
        await page.evaluate((p) => {
          document.querySelector(`#paneltabs button[data-panes="${p}"]`).click();
        }, panes);
        await new Promise((r) => setTimeout(r, 700));
        // Below the 820px breakpoint the control panel is a full-width overlay
        // that covers the upper map by design — notes and pane labels included —
        // so it is not something the credit can be "clear of" there. Everything
        // that stays put at both widths is still checked.
        const against = [".topbar", ".bottombar", "#t1", "#t2", ".leaflet-control-zoom"]
          .concat(w > 820 ? [".ctlpanel"] : []);
        const hits = await page.evaluate((sels) => {
          const r = (sel) => {
            const e = document.querySelector(sel);
            if (!e || e.hidden || getComputedStyle(e).display === "none") return null;
            const b = e.getBoundingClientRect();
            return b.width && b.height ? b : null;
          };
          const a = r("#attrib");
          if (!a) return ["the credit is not visible at all"];
          const hit = (x, y) => x && y && x.left < y.right && y.left < x.right &&
                                x.top < y.bottom && y.top < x.bottom;
          const out = [];
          if (a.right > innerWidth || a.bottom > innerHeight || a.left < 0)
            out.push("the credit is off screen");
          for (const sel of sels) if (hit(a, r(sel))) out.push(`the credit overlaps ${sel}`);
          return out;
        }, against);
        if (hits.length) hits.forEach((x) => fail(`${w}px, ${panes}-pane: ${x}`));
        else ok(`${w}px, ${panes}-pane: basemap credit visible and clear of every panel`);
      }
    }
    await page.setViewport({ width: 1280, height: 1000 });
    await page.evaluate(() => document.querySelector('#paneltabs button[data-panes="1"]').click());
    await new Promise((r) => setTimeout(r, 500));

    // Auto mode: follows the zoom, both ways, until the reader states a
    // preference. The clicks above were preferences, so this needs a fresh page.
    await page.evaluate(() => localStorage.removeItem("scoreboard.basemap"));
    await page.goto(`http://localhost:${PORT}/map.html`, { waitUntil: "networkidle0" });
    await page.evaluate(() => window.__mapReady);
    await new Promise((r) => setTimeout(r, 400));
    const auto = await page.evaluate(async (z) => {
      const S = window.__S, out = { boot: S.basemapId };
      S.maps.map1.setView([48.86, 2.35], z + 1, { animate: false });
      await new Promise((r) => setTimeout(r, 500));
      out.zoomedIn = S.basemapId;
      S.maps.map1.setZoom(Math.max(S.maps.map1.getMinZoom(), z - 3));
      await new Promise((r) => setTimeout(r, 500));
      out.zoomedOut = S.basemapId;
      // An explicit choice has to survive zooming back out.
      document.querySelector('#basetabs button[data-base="off"]').click();
      S.maps.map1.setView([48.86, 2.35], z + 1, { animate: false });
      await new Promise((r) => setTimeout(r, 500));
      out.afterExplicitOff = S.basemapId;
      return out;
    }, cfg.basemap_zoom);
    const first = cfg.basemaps[0].id;
    if (auto.boot !== "off") fail(`basemap is ${auto.boot} at the opening world view, not off`);
    else if (auto.zoomedIn !== first) fail(`zooming past ${cfg.basemap_zoom} left the basemap ${auto.zoomedIn}`);
    else if (auto.zoomedOut !== "off") fail(`zooming back out left the basemap ${auto.zoomedOut} under a whole-world view`);
    else ok(`basemap follows the zoom until asked otherwise (off -> ${first} -> off across z${cfg.basemap_zoom})`);
    if (auto.afterExplicitOff !== "off")
      fail(`an explicit "off" was overridden by auto mode on the next zoom-in (${auto.afterExplicitOff})`);
    else ok("an explicit choice ends auto mode and sticks");
    await page.evaluate(() => window.__S.maps.map1.setView([0, 0], 3, { animate: false }));
    await new Promise((r) => setTimeout(r, 500));
  }

  // --- 12. the control panel ------------------------------------------------
  // Ten equally-weighted controls made this 725px tall — 81% of a 1440x900
  // window and already scrolling on a 1280x800 laptop. The two-tier split is
  // only worth anything if it stays split, so the height is asserted rather
  // than admired.
  await page.setViewport({ width: 1280, height: 800 });
  await page.goto(`http://localhost:${PORT}/map.html`, { waitUntil: "networkidle0" });
  await page.evaluate(() => window.__mapReady);
  await new Promise((r) => setTimeout(r, 500));

  const panel = await page.evaluate(() => {
    const p = document.getElementById("ctlpanel");
    const r = p.getBoundingClientRect();
    return { h: Math.round(r.height), pct: Math.round((r.height / innerHeight) * 100),
             scrolls: p.scrollHeight > p.clientHeight + 1,
             folds: [...document.querySelectorAll(".fold")].map((f) => ({ id: f.id, open: f.open })),
             summary: (document.getElementById("dispsum") || {}).textContent,
             varShown: !document.getElementById("varwrap").hidden,
             varCount: Object.keys(window.__S.f.variables).length };
  });
  if (panel.pct > 55) fail(`the control panel is ${panel.h}px, ${panel.pct}% of a 1280x800 viewport`);
  else if (panel.scrolls) fail(`the control panel scrolls at 1280x800 (${panel.h}px)`);
  else ok(`control panel is ${panel.h}px, ${panel.pct}% of a laptop viewport, no scroll`);
  if (panel.folds.some((f) => f.open))
    fail(`second-tier sections start expanded: ${panel.folds.filter((f) => f.open).map((f) => f.id)}`);
  else ok(`both second-tier sections start collapsed (${panel.folds.map((f) => f.id).join(", ")})`);
  // A disclosure that hides live state has to report it, or the reader cannot
  // tell °C from °F without opening it.
  if (!panel.summary || !panel.summary.trim())
    fail("the collapsed Display section reports none of the state it is hiding");
  else ok(`collapsed Display reports its state ("${panel.summary}")`);
  if (panel.varCount < 2 && panel.varShown)
    fail("the Variable group is shown with a single variable exported — a tab group offering no choice");
  else ok(`Variable group shown only when there is a choice (${panel.varCount} exported)`);

  // Every control must still be reachable, collapsed or not — querySelector
  // finds them inside a closed <details>, but only if they still exist.
  const missing = await page.evaluate(() => ["modelsel", "initsel", "vartabs", "viewtabs",
    "basetabs", "scaletabs", "unittabs", "paneltabs", "opac", "prov"]
    .filter((id) => !document.getElementById(id)));
  if (missing.length) fail(`controls lost in the panel restructure: ${missing.join(", ")}`);
  else ok("every control survived the restructure");

  // The reported bug: narrowing an already-open window left a full-width slab
  // over the map whose dismiss button had been pushed off the edge of the nav.
  for (const w of [820, 600, 380]) {
    await page.setViewport({ width: w, height: 820 });
    await new Promise((r) => setTimeout(r, 500));
    const narrow = await page.evaluate(() => {
      const p = document.getElementById("ctlpanel");
      const nav = document.querySelector(".topbar").getBoundingClientRect();
      const btn = document.getElementById("panelbtn").getBoundingClientRect();
      return { hidden: p.hidden, navFits: nav.right <= innerWidth,
               btnReachable: btn.width > 0 && btn.right <= innerWidth };
    });
    if (!narrow.hidden) fail(`${w}px: narrowing the window left the panel open over the map`);
    else if (!narrow.navFits) fail(`${w}px: the nav runs off the right edge of the viewport`);
    else if (!narrow.btnReachable) fail(`${w}px: the Layers button is off screen — the panel cannot be dismissed`);
    else ok(`${w}px: panel steps aside, nav and its toggle stay on screen`);
  }

  // ...and opening it there gives a drawer bounded by the chrome around it,
  // not a slab clipped at 46% of the viewport through the middle of a control.
  const drawer = await page.evaluate(async () => {
    document.getElementById("panelbtn").click();
    await new Promise((r) => setTimeout(r, 400));
    const p = document.getElementById("ctlpanel"), r = p.getBoundingClientRect();
    const nav = document.querySelector(".topbar").getBoundingClientRect();
    const bar = document.querySelector(".bottombar").getBoundingClientRect();
    return { shown: !p.hidden, belowNav: r.top >= nav.bottom - 1,
             aboveBar: r.bottom <= bar.top + 1, onScreen: r.bottom <= innerHeight,
             clipped: p.scrollHeight > p.clientHeight + 1, h: Math.round(r.height) };
  });
  if (!drawer.shown) fail("380px: the Layers button did not open the panel");
  else if (!drawer.belowNav) fail("380px: the open drawer overlaps the nav");
  else if (!drawer.aboveBar || !drawer.onScreen) fail("380px: the open drawer runs under the bottom bar");
  else if (drawer.clipped) fail(`380px: the drawer is clipped (${drawer.h}px shown, content is taller)`);
  else ok(`380px: drawer opens clear of the nav and bottom bar, unclipped (${drawer.h}px)`);
  await page.setViewport({ width: 1280, height: 1000 });
  await new Promise((r) => setTimeout(r, 400));

  // --- 13. glass legibility -------------------------------------------------
  // The floating chrome is translucent, so its effective background is not a
  // colour anyone chose — it is whatever the field happens to be behind it, and
  // the field is a temperature ramp that spans violet to dark red. Contrast has
  // to be measured on the *composited* result, which means rendering the page
  // and reading the pixels back; no amount of reasoning about the tokens
  // predicts it. Both themes, over the hot end of the ramp where it is worst.
  const relLum = ([r, g, b]) => {
    const f = (v) => (v /= 255) <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const contrast = (a, b) => {
    const [hi, lo] = [relLum(a), relLum(b)].sort((x, y) => y - x);
    return (hi + 0.05) / (lo + 0.05);
  };
  const CHROME_BOXES = [["panel", "#ctlpanel"], ["bottom bar", ".bottombar"],
                        ["nav", ".topbar"], ["pane label", "#t1"]];
  await page.setViewport({ width: 1440, height: 900 });

  let glassFails = 0, worstSeen = { ratio: Infinity };
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    for (const view of [[22, 20], [5, -25], [28, 55]]) {
      await page.evaluate((v) => window.__S.maps.map1.setView(v, 4, { animate: false }), view);
      await new Promise((r) => setTimeout(r, 700));
      const shot = await page.screenshot({ encoding: "base64" });
      const res = await page.evaluate(async (url, boxes) => {
        const img = new Image();
        img.src = "data:image/png;base64," + url;
        await img.decode();
        const c = document.createElement("canvas");
        c.width = img.width; c.height = img.height;
        const ctx = c.getContext("2d");
        ctx.drawImage(img, 0, 0);
        // Resolve the text tokens as they apply *inside* the glass — map.html
        // scopes a darker --muted there, and reading it off :root would test a
        // colour the panel never uses.
        const glass = document.getElementById("ctlpanel");
        const val = (el, n) => {
          const probe = document.createElement("div");
          probe.style.color = getComputedStyle(el).getPropertyValue(n).trim();
          document.body.append(probe);
          const rgb = getComputedStyle(probe).color.match(/[\d.]+/g).slice(0, 3).map(Number);
          probe.remove();
          return rgb;
        };
        const inks = { ink: val(glass, "--ink"), "ink-2": val(glass, "--ink-2"),
                       muted: val(glass, "--muted") };
        const out = [];
        for (const [name, sel] of boxes) {
          const e = document.querySelector(sel);
          if (!e || e.hidden) continue;
          const r = e.getBoundingClientRect();
          if (r.width < 8 || r.height < 8) continue;
          // The modal colour inside the box is its effective background: the
          // background dominates a panel by area, glyphs are a minority.
          const d = ctx.getImageData(r.left + 2, r.top + 2, r.width - 4, r.height - 4).data;
          const hist = new Map();
          for (let i = 0; i < d.length; i += 4) {
            const k = ((d[i] >> 2) << 12) | ((d[i + 1] >> 2) << 6) | (d[i + 2] >> 2);
            hist.set(k, (hist.get(k) || 0) + 1);
          }
          let best = 0, key = 0;
          for (const [k, n] of hist) if (n > best) { best = n; key = k; }
          out.push({ name, bg: [((key >> 12) & 63) << 2, ((key >> 6) & 63) << 2, (key & 63) << 2] });
        }
        return { inks, out };
      }, shot, CHROME_BOXES);

      for (const { name, bg } of res.out) {
        for (const [token, rgb] of Object.entries(res.inks)) {
          const r = contrast(bg, rgb);
          if (r < worstSeen.ratio) worstSeen = { ratio: r, name, token, theme, bg };
          if (r < 4.5) {
            fail(`glass: ${theme} ${name}, ${token} text is ${r.toFixed(1)}:1 over the composited ` +
                 `background rgb(${bg.join(",")}) — below the 4.5:1 floor for small text`);
            glassFails++;
          }
        }
      }
    }
  }
  await page.evaluate(() => document.documentElement.removeAttribute("data-theme"));
  if (!glassFails)
    ok(`glass keeps every text token above 4.5:1 over the ramp (worst ` +
       `${worstSeen.ratio.toFixed(1)}:1 — ${worstSeen.theme} ${worstSeen.name}, ${worstSeen.token})`);

  // The effect has to actually be an effect, and it has to yield when the
  // reader has asked for less transparency.
  const glass = await page.evaluate(() => {
    const s = getComputedStyle(document.getElementById("ctlpanel"));
    return { filter: s.backdropFilter || s.webkitBackdropFilter, bg: s.backgroundColor };
  });
  if (!/blur/.test(glass.filter || "")) fail(`the panel has no backdrop blur (${glass.filter})`);
  else ok(`glass is a real backdrop filter (${glass.filter})`);

  // Not every Chrome build can emulate this one; an unsupported media feature
  // is a gap in the harness, not a defect in the page, so it is reported and
  // skipped rather than crashing the run.
  let canEmulate = true;
  try {
    await page.emulateMediaFeatures([{ name: "prefers-reduced-transparency", value: "reduce" }]);
  } catch {
    canEmulate = false;
    console.log("     (this Chrome cannot emulate prefers-reduced-transparency — skipped)");
  }
  await new Promise((r) => setTimeout(r, 300));
  if (canEmulate) {
  const reduced = await page.evaluate(() => {
    const s = getComputedStyle(document.getElementById("ctlpanel"));
    const m = s.backgroundColor.match(/[\d.]+/g);
    return { filter: s.backdropFilter || s.webkitBackdropFilter,
             alpha: m && m.length > 3 ? Number(m[3]) : 1 };
  });
  if (/blur/.test(reduced.filter || "") || reduced.alpha < 0.99)
    fail(`prefers-reduced-transparency still gets glass (filter ${reduced.filter}, alpha ${reduced.alpha})`);
  else ok("prefers-reduced-transparency falls back to an opaque panel");
  await page.emulateMediaFeatures([]);
  }
  await page.setViewport({ width: 1280, height: 1000 });
  await new Promise((r) => setTimeout(r, 400));

  await page.screenshot({ path: SHOT, fullPage: false });
  console.log(`     screenshot -> ${SHOT}`);
  if (warnings.length) console.log(`     (${warnings.length} console warning(s))`);

  await browser.close();
  server.close();
  console.log(failures ? `\n${failures} FAILURE(S)` : "\nall render checks passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error("gate crashed:", e); process.exit(2); });
