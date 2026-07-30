/* map.html — global forecast and error fields.
 *
 * Leaflet with L.CRS.EPSG4326: the field is drawn to a canvas overlay and a
 * Natural Earth coastline is stroked on top. At global zooms that is the whole
 * map, and deliberately so — for a 1 deg scientific field a clean outline reads
 * better than street cartography, and the page makes no external request.
 *
 * Past `map.basemap_zoom` a WMS basemap switches itself on (see basemap()
 * below), because at that scale the coastline has stopped being enough to
 * answer "where exactly is this". It is WMS rather than {z}/{x}/{y} for a
 * reason that is easy to get wrong; config.yaml's `display.map` block has it.
 *
 * Everything about the grid and the encoding comes from manifest.json. Nothing
 * here infers geometry from array shape — that is how a north-up field gets
 * drawn south-up while every numeric check still passes.
 */

import { loadField, sampleAt, clearFieldCache, extentInBounds, upsample } from "./field.js";
import { scaleFor, colorbarTicks, rgbCss, rampFor, isAnchored, buildLUT } from "./colormap.js";
import { unitFor, loadSystem, saveSystem } from "./units.js";

const DATA_DIR = "data";
const S = { panes: 1, kind: "forecast", units: loadSystem(), stretch: false };

const $ = (id) => document.getElementById(id);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

const SHORT = { z500: "Z500", t850: "T850", t2m: "T2M", msl: "MSL",
                u10m: "U10", v10m: "V10", tp06: "Precip", tp: "Precip" };

/* ---------- boot ---------- */

async function boot() {
  const [manifest, models, coast, borders] = await Promise.all([
    fetch(`${DATA_DIR}/manifest.json`).then((r) => r.json()),
    fetch(`${DATA_DIR}/models.json`).then((r) => r.json()),
    fetch("lib/vendor/coastlines-50m.json").then((r) => r.json()),
    fetch("lib/vendor/borders-50m.json").then((r) => r.json()),
  ]);
  S.manifest = manifest; S.models = models; S.coast = coast; S.borders = borders;

  // `map` postdates the first manifests, and check_map_render.js serves whatever
  // docs/data holds — so default rather than assume. No basemaps configured is a
  // supported state, not a broken one: the page then behaves exactly as it did
  // before this block existed.
  S.mapcfg = { ...MAP_DEFAULTS, ...(manifest.map || {}) };
  S.basemaps = S.mapcfg.basemaps || [];
  S.basemapId = loadBasemap();
  // Auto mode follows the zoom until the reader states a preference; a stored
  // preference is a statement, so it also ends auto mode.
  S.basemapAuto = S.basemapId === null;
  if (S.basemapAuto) S.basemapId = "off";
  if (!S.basemaps.some((b) => b.id === S.basemapId)) S.basemapId = "off";

  const fieldInits = Object.keys(manifest.fields || {});
  if (!fieldInits.length) {
    fatal("No field data yet",
          "Run <code>python -m scoreboard.fields</code> to generate " +
          "<code>docs/data/fields/</code>.");
    return;
  }
  S.initKeys = fieldInits.sort().reverse();
  S.initKey = S.initKeys[0];
  selectInit();

  buildInitSelect();
  buildViewTabs();
  buildUnitTabs();
  buildScaleTabs();
  buildPanelTabs();
  buildBasemapTabs();
  buildOpacitySlider();
  makeMap("map1"); makeMap("map2");
  applyBasemap();

  // The map fills the viewport, so a resize changes its size rather than the
  // page's scroll height — Leaflet has to be told.
  // A resize changes what "cover" means, so the minimum zoom has to be
  // recomputed — otherwise widening the window reveals blank page beside the
  // field at a zoom that used to fill it.
  window.addEventListener("resize", () => {
    Object.values(S.maps).forEach((m) => { m.invalidateSize(); coverWorld(m, true); });
  });

  trackChromeHeights();
  initFolds();

  trackPanelBreakpoint();

  await render();
}

function selectInit() {
  S.f = S.manifest.fields[S.initKey];
  S.grid = S.f.grid;
  S.enc = S.f.encoding;
  const vars = Object.keys(S.f.variables);
  if (!S.variable || !vars.includes(S.variable)) S.variable = vars.includes("t2m") ? "t2m" : vars[0];
  if (!S.model || !S.f.models.includes(S.model)) S.model = S.f.models[0];
  if (!S.model2 || !S.f.models.includes(S.model2)) S.model2 = S.f.models[1] || S.f.models[0];
  S.leadIdx = 0;
}

const modelInfo = (id) => S.models.find((m) => m.id === id) || { id, label: id, color: "#888", css_var: "--none" };

/* Show a fatal error in the page instead of only the console.
 *
 * This used to write into `document.querySelector("main")`, which map.html does
 * not contain — its shell is div.app. So every error path threw a TypeError on
 * null and the page failed blank, which is the one situation where the message
 * mattered. */
function fatal(title, detail) {
  const box = $("fatal");
  box.innerHTML = "";
  box.append(el("h3", null, title));
  const p = el("p");
  p.innerHTML = detail;
  box.append(p);
  box.hidden = false;
  ["ctlpanel", "notes"].forEach((id) => { const n = $(id); if (n) n.hidden = true; });
  document.querySelectorAll(".bottombar, .paneltag").forEach((n) => { n.hidden = true; });
}

/* ---------- controls ---------- */

function buildInitSelect() {
  const sel = $("initsel"); sel.innerHTML = "";
  S.initKeys.forEach((k) => {
    const f = S.manifest.fields[k];
    const o = el("option", null, `${k.replace("T", " ")}Z · ${f.tier}`);
    o.value = k; sel.appendChild(o);
  });
  sel.value = S.initKey;
  sel.onchange = async () => { S.initKey = sel.value; selectInit(); clearFieldCache(); await render(); };
}

function buildModelSelects() {
  [["modelsel", "model"], ["modelsel2", "model2"]].forEach(([id, key]) => {
    const sel = $(id); sel.innerHTML = "";
    S.f.models.forEach((m) => {
      const o = el("option", null, modelInfo(m).label);
      o.value = m; sel.appendChild(o);
    });
    sel.value = S[key];
    sel.onchange = async () => { S[key] = sel.value; await render(); };
  });
}

function buildVarTabs() {
  const box = $("vartabs"); box.innerHTML = "";
  // Hidden while the export carries a single variable: a labelled tab group
  // with one button offers no choice and still costs a row.
  $("varwrap").hidden = Object.keys(S.f.variables).length < 2;
  Object.keys(S.f.variables).forEach((v) => {
    const b = el("button", S.variable === v ? "on" : null, SHORT[v] || v.toUpperCase());
    b.title = (S.manifest.variables[v] || {}).label || v;
    b.onclick = async () => { S.variable = v; await render(); };
    box.appendChild(b);
  });
}

function buildViewTabs() {
  $("viewtabs").querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      S.kind = b.dataset.kind;
      $("viewtabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      await render();
    };
  });
}

function buildScaleTabs() {
  const box = $("scaletabs");
  box.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("on", (b.dataset.scale === "stretch") === S.stretch);
    b.onclick = async () => {
      S.stretch = b.dataset.scale === "stretch";
      updateDisplaySummary();
      box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      await draw();
    };
  });
}

function buildUnitTabs() {
  const box = $("unittabs");
  box.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("on", b.dataset.units === S.units);
    b.onclick = async () => {
      S.units = b.dataset.units;
      saveSystem(S.units);
      updateDisplaySummary();
      box.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      // Only labels change — the field arrays and the colour mapping are in
      // stored units, so nothing needs refetching or recolouring.
      await draw();
    };
  });
}

/* Publish the floating bars' measured heights as `--barh` and `--navh`, for the
 * chrome that has to sit clear of them.
 *
 * Both are wrapping flex rows, so their heights are functions of viewport width
 * and of content that appears at runtime — the hover readout, the colourbar's
 * unit, the nav wrapping to two rows on a phone. The bottom bar reaches 130px
 * below ~520px, well past the fixed 92px offset the pane labels used to use, and
 * chrome that lands underneath does not error or clip: it simply vanishes.
 *
 * A ResizeObserver rather than a resize listener, because the reflows that
 * matter are not all window resizes. */
function trackChromeHeights() {
  for (const [sel, prop] of [[".bottombar", "--barh"], [".topbar", "--navh"]]) {
    const box = document.querySelector(sel);
    if (!box) continue;
    const set = () => document.documentElement.style.setProperty(
      prop, `${Math.round(box.getBoundingClientRect().height)}px`);
    set();
    if (window.ResizeObserver) new ResizeObserver(set).observe(box);
    else window.addEventListener("resize", set);   // still better than a constant
  }
}

/* Remember which second-tier sections the reader left open.
 *
 * Both default to closed: the panel's whole purpose is to stop presenting ten
 * equally-weighted controls at once, and restoring it to fully expanded on
 * every visit would undo that on the second page load. */
const FOLD_KEY = "scoreboard.panelfolds";

function initFolds() {
  let open = {};
  try { open = JSON.parse(localStorage.getItem(FOLD_KEY) || "{}"); } catch { /* ignore */ }
  const folds = [...document.querySelectorAll(".fold")];
  folds.forEach((f) => { f.open = !!open[f.id]; });
  const save = () => {
    const state = {};
    folds.forEach((f) => { state[f.id] = f.open; });
    try { localStorage.setItem(FOLD_KEY, JSON.stringify(state)); } catch { /* ignore */ }
  };
  folds.forEach((f) => f.addEventListener("toggle", save));
}

/* What the collapsed Display section is currently hiding.
 *
 * A disclosure that hides live state is worse than no disclosure: the reader
 * cannot tell whether they are looking at °C or °F without opening it. Only the
 * settings that are *not* at their default are listed, so the common case stays
 * short and anything unusual announces itself. */
function updateDisplaySummary() {
  const out = $("dispsum");
  if (!out || !S.manifest) return;
  const bits = [unitFor((S.manifest.variables[S.variable] || {}).units, S.units).label];
  if (S.stretch) bits.push("stretched");
  const base = basemapDef(S.basemapId);
  if (base) bits.push(base.label.toLowerCase());
  if (S.panes === 2) bits.push("2 panes");
  out.textContent = bits.join(" · ");
}

/* Whether the layer panel is showing.
 *
 * Three controls open and close it — the nav's Layers button, the minimise
 * button on the panel, and the pill it minimises to — and the viewport moves it
 * as well when the window crosses 820px. All four go through here, because the
 * bug this replaced was two of them disagreeing: the breakpoint used to be a
 * matchMedia *check* made once at boot, so narrowing an already-open window left
 * a full-width slab over the map with its dismiss button off the edge of the
 * nav. It is a *subscription* now, and there is exactly one place that decides.
 *
 * Minimising is remembered; the narrow-screen default is not. Collapsing the
 * panel on a desktop is a preference about how you like the map, and it should
 * survive a reload. Being narrow is a fact about the window, and re-deriving it
 * on every load is both correct and free. */
const PANEL_KEY = "scoreboard.panelmin";
const NARROW = "(max-width: 820px)";

function loadMinimised() {
  try { return localStorage.getItem(PANEL_KEY) === "1"; } catch { return false; }
}

function showPanel(show) {
  S.panelHidden = !show;
  // Only a deliberate choice on a wide screen is a preference worth storing;
  // toggling the drawer on a phone is navigation.
  if (!window.matchMedia(NARROW).matches) {
    try { localStorage.setItem(PANEL_KEY, show ? "0" : "1"); } catch { /* ignore */ }
  }
  applyPanel();
}

function applyPanel() {
  const panel = $("ctlpanel"), pill = $("panelpill"), min = $("panelmin");
  panel.hidden = S.panelHidden;
  pill.hidden = !S.panelHidden;
  pill.setAttribute("aria-expanded", String(!S.panelHidden));
  if (min) min.setAttribute("aria-expanded", String(!S.panelHidden));
  // The panes are sized by Leaflet, not by CSS reflow, so a panel that comes
  // and goes has to be announced or the map keeps its old dimensions.
  setTimeout(() => Object.values(S.maps).forEach((m) => m.invalidateSize()), 0);
}

function trackPanelBreakpoint() {
  const mq = window.matchMedia(NARROW);
  S.panelHidden = mq.matches || loadMinimised();
  mq.addEventListener("change", (e) => {
    S.panelHidden = e.matches || loadMinimised();
    applyPanel();
  });
  $("panelbtn").onclick = () => showPanel(S.panelHidden);
  $("panelmin").onclick = () => showPanel(false);
  $("panelpill").onclick = () => showPanel(true);
  applyPanel();
}

/* ---------- basemap ----------
 *
 * Defaults for a manifest written before `display.map` existed. max_zoom 6 and
 * an empty basemap list reproduce the pre-basemap page exactly.
 */
const MAP_DEFAULTS = { max_zoom: 6, basemap_zoom: 6, field_opacity: 0.72, basemaps: [] };
const BASEMAP_KEY = "scoreboard.basemap";

/* null means "never chosen" — which is what puts the page in auto mode, and is
 * why this cannot just default to "off": an explicit off has to be able to keep
 * the basemap from reappearing on the next zoom-in. */
function loadBasemap() {
  try { return localStorage.getItem(BASEMAP_KEY); } catch { return null; }
}
function saveBasemap(id) {
  try { localStorage.setItem(BASEMAP_KEY, id); } catch { /* private browsing */ }
}

const basemapDef = (id) => S.basemaps.find((b) => b.id === id) || null;

function buildBasemapTabs() {
  const wrap = $("basewrap"), box = $("basetabs");
  if (!S.basemaps.length) { wrap.hidden = true; return; }   // none configured
  box.innerHTML = "";
  const mk = (id, label, title) => {
    const b = el("button", S.basemapId === id ? "on" : null, label);
    b.dataset.base = id;
    if (title) b.title = title;
    b.onclick = () => {
      // Clicking ends auto mode even when it picks what auto had already
      // chosen: the reader has now said so, and that has to stick when they
      // zoom back out.
      S.basemapAuto = false;
      S.basemapId = id;
      saveBasemap(id);
      syncBasemapTabs();
      applyBasemap();
    };
    box.appendChild(b);
  };
  mk("off", "Off", "Coastlines and borders only");
  S.basemaps.forEach((b) => mk(b.id, b.label,
    b.over ? "Transparent — drawn over the field" : "Drawn under the field"));
}

function syncBasemapTabs() {
  const box = $("basetabs");
  if (box) box.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("on", b.dataset.base === S.basemapId));
  // The slider is meaningless without an opaque layer to reveal: fading the
  // field with nothing behind it just shows page background.
  const def = basemapDef(S.basemapId);
  $("opacwrap").hidden = !(def && !def.over);
}

function buildOpacitySlider() {
  const sl = $("opac");
  sl.value = String(S.mapcfg.field_opacity);
  sl.oninput = () => {
    S.mapcfg.field_opacity = Number(sl.value);
    $("opacval").textContent = Math.round(S.mapcfg.field_opacity * 100) + "%";
    applyFieldOpacity();
  };
  $("opacval").textContent = Math.round(S.mapcfg.field_opacity * 100) + "%";
}

/* Add or remove the WMS layer on every pane, and take everything that depends
 * on it with it: the field's opacity, the vendored geography, the city labels
 * and the attribution chip.
 *
 * The vendored coastline and borders come off whenever a basemap is on. They
 * are not merely redundant with OSM's own coastline, they are wrong at this
 * scale — vendor_geography.py simplifies to 0.02 deg, which was sub-pixel at
 * maxZoom 6 and is fifteen pixels of stair-stepping at 10. */
function applyBasemap() {
  const def = basemapDef(S.basemapId);
  S.baseFail = null;              // whatever failed, it belonged to the old layer
  for (const [id, m] of Object.entries(S.maps)) {
    const cur = S.baseLayers[id];
    if (cur) { m.removeLayer(cur); S.baseLayers[id] = null; }
    if (def) {
      const layer = L.tileLayer.wms(def.url, {
        layers: def.layers,
        format: "image/png",
        transparent: !!def.over,
        version: "1.1.1",         // 1.3.0 flips EPSG:4326 to lat,lon bbox order
        maxZoom: def.max_zoom || S.mapcfg.max_zoom,
        // Transparent layers go in a pane above the field; opaque ones stay in
        // the default tilePane, which Leaflet already stacks below the overlay
        // pane the field image lives in.
        ...(def.over ? { pane: "basetop" } : {}),
        attribution: def.attribution,
      });
      // A basemap that fails is otherwise indistinguishable from a button that
      // did nothing: the tiles are someone else's server, they are requested
      // lazily, and a failed one leaves blank space rather than an error. Say
      // so, and take it back as soon as a tile arrives — a single timed-out tile
      // during a fast pan should not leave a warning standing.
      layer.on("tileerror", () => {
        if (S.baseFail && S.baseFail.id === def.id) return;
        S.baseFail = def;
        renderBaseNote();
      });
      layer.on("tileload", () => {
        if (!S.baseFail) return;
        S.baseFail = null;
        renderBaseNote();
      });
      layer.addTo(m);
      if (!def.over) layer.bringToBack();
      S.baseLayers[id] = layer;
    }
    // OSM labels the cities itself; ours would print a second name beside its
    // own. The dots stay — they mark the 32 points that have model data, which
    // is a different fact from "there is a city here".
    m.getContainer().classList.toggle("basemap-on", !!def);
    (S.geo[id] || []).forEach((l) => {
      if (def) m.removeLayer(l); else if (!m.hasLayer(l)) l.addTo(m);
    });
  }
  syncAttribution(def);
  syncBasemapTabs();
  updateDisplaySummary();
  applyFieldOpacity();
  renderBaseNote();
}

/* The "basemap did not load" note.
 *
 * Kept out of draw()'s notes, which are rebuilt from scratch on every redraw and
 * would erase it — and re-created here rather than toggled, so it survives that
 * rebuild by being re-appended to whatever the stack currently holds. Failure is
 * about the basemap, not about the field, and the note says so: the numbers on
 * screen are unaffected and the reader should not doubt them. */
function renderBaseNote() {
  const stack = $("notes");
  if (!stack) return;
  const old = $("basenote");
  if (old) old.remove();
  if (!S.baseFail) return;
  let host = S.baseFail.url;
  try { host = new URL(S.baseFail.url).host; } catch { /* keep the raw string */ }
  const n = el("div", "note");
  n.id = "basenote";
  n.innerHTML = `<b>${S.baseFail.label} could not be loaded.</b> The basemap ` +
    `comes from <code>${host}</code>, an external tile service — the forecast ` +
    `field and every number on this page are unaffected. It will clear itself ` +
    `if the service comes back; otherwise set Basemap to Off.`;
  stack.prepend(n);
}

/* Credit for the basemap, shown and hidden with the layer that requires it.
 *
 * This is page chrome, not L.control.attribution. A Leaflet control anchors to
 * one *map's* corner, and in side-by-side that map's bottom-right is the middle
 * of the screen — directly on top of pane 1's label, which then silently
 * disappears behind it. Nothing errors when two absolutely positioned boxes
 * collide, so this is asserted in check_map_render.js rather than watched for. */
function syncAttribution(def) {
  const box = $("attrib");
  if (!box) return;
  box.innerHTML = def ? def.attribution : "";
  box.hidden = !def;
}

function applyFieldOpacity() {
  const def = basemapDef(S.basemapId);
  // Only an opaque basemap needs seeing through. Under a transparent overlay
  // the field stays at full strength, which is the whole reason that layer is
  // offered first.
  const o = def && !def.over ? S.mapcfg.field_opacity : 1;
  S.fieldOpacity = o;
  Object.values(S.layers).forEach((ls) => (ls || []).forEach((l) => l.setOpacity(o)));
}

/* Auto mode: the basemap follows the zoom until the reader states a preference.
 * Symmetric on purpose — zooming back out restores the clean global field
 * rather than leaving street cartography under a whole-world view. */
function autoBasemap(m) {
  if (!S.basemapAuto || !S.basemaps.length) return;
  const want = m.getZoom() >= S.mapcfg.basemap_zoom ? S.basemaps[0].id : "off";
  if (want === S.basemapId) return;
  S.basemapId = want;
  applyBasemap();
}

function buildPanelTabs() {
  $("paneltabs").querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      S.panes = Number(b.dataset.panes);
      updateDisplaySummary();
      $("paneltabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      $("maprow").classList.toggle("two", S.panes === 2);
      $("panel2").hidden = S.panes === 1;
      $("model2wrap").hidden = S.panes === 1;
      // Halving a pane's width changes what zoom counts as covering it.
      Object.values(S.maps).forEach((m) => setTimeout(() => {
        m.invalidateSize(); coverWorld(m, true);
      }, 0));
      await render();
    };
  });
}

/* ---------- leaflet ---------- */

S.maps = {}; S.layers = {}; S.syncing = false;
S.baseLayers = {};   // per pane: the WMS layer, or null
S.geo = {};          // per pane: the vendored coast/border/graticule polylines

const WORLD = L.latLngBounds([[-90, -180], [90, 180]]);

/* Longitude offsets at which every layer is repeated.
 *
 * The Pacific is the problem this solves. A single overlay spanning -180..180
 * puts a seam down the middle of the one basin that most needs to be read as
 * continuous, and the old fix — a hard maxBounds wall at the antimeridian — made
 * the map simply refuse to pan further. Drawing the world three times and
 * letting Leaflet's worldCopyJump fold the centre back into the middle copy
 * gives continuous eastward and westward panning with no seam and no wall.
 *
 * Three is enough, and this is worth checking rather than assuming: at minZoom
 * the viewport is at most 288 deg wide (the world is 2:1, the viewport is wider
 * than that, so latitude binds first), worldCopyJump keeps the centre within
 * +-180, so the visible span never leaves -324..324 — comfortably inside the
 * -540..540 the three copies cover. Raising maxZoom only narrows the viewport,
 * so the margin grows. Widening the *page* does not: a viewport past 2:1 would
 * make longitude bind instead, and this needs revisiting if that ever happens.
 */
const COPIES = [-360, 0, 360];

/* Latitude is still walled; longitude is not. Leaflet has no lat-only maxBounds,
 * so the longitude range is set absurdly wide instead of removing the bounds
 * altogether — that keeps the poles clamped, which is what the wall was
 * actually for. Panning north past 90 shows blank page; panning east past 180
 * now shows the Pacific. */
const PAN_BOUNDS = L.latLngBounds([[-90, -1e6], [90, 1e6]]);

/* A geojson line collection as ONE Leaflet polyline, shifted by `dx` degrees.
 *
 * Not L.geoJSON, which makes a layer — and so an SVG <path> — per feature.
 * Natural Earth 50 m is 1186 coastline features and 389 border features; across
 * three world copies that was 9558 path elements and a 1.6 s boot. Leaflet
 * renders a multi-polyline as a single path with several subpaths, so passing
 * every ring to one L.polyline draws exactly the same picture out of nine
 * elements instead of nine thousand.
 *
 * Nothing styles or labels by attribute, so collapsing the features loses
 * nothing. EPSG:4326 is linear in longitude, so shifting coordinates by dx is
 * exactly a shift on screen — no reprojection needed.
 */
function lineLayer(geo, dx, className) {
  const rings = [];
  for (const f of geo.features) {
    const g = f.geometry;
    const parts = g.type === "MultiLineString" ? g.coordinates : [g.coordinates];
    for (const part of parts) rings.push(part.map(([lon, lat]) => [lat, lon + dx]));
  }
  return L.polyline(rings, { className, interactive: false });
}

/* Fill the viewport rather than fit inside it.
 *
 * fitWorld() and fitBounds() pick the zoom where the world fits *within* the
 * view, which letterboxes: the world is 2:1 and a browser window is not, so one
 * axis gets bands of empty page either side of the field. Worse, panning then
 * runs off the overlay entirely and you see coastlines drawn over blank
 * background — a monochrome strip that looks like a repeat of the map.
 *
 * getBoundsZoom(bounds, true) is the opposite operation: the minimum zoom at
 * which the *view* fits inside the bounds. That is `background-size: cover`.
 * The zoom floor still comes from WORLD, not from PAN_BOUNDS: what it has to
 * guarantee is that no blank page shows above or below the field, and that is a
 * statement about latitude only. */
function coverWorld(m, keepCenter = false) {
  const z = m.getBoundsZoom(WORLD, true);
  const center = keepCenter && m._loaded ? m.getCenter() : L.latLng(0, 0);
  m.setMinZoom(z);
  if (!m._loaded || m.getZoom() < z) m.setView(center, z, { animate: false });
  else m.setView(center, m.getZoom(), { animate: false });
}

/* A lat/lon grid as geojson. Meridians are drawn as many short segments rather
 * than two endpoints because the map is EPSG:4326 — straight in that projection
 * is straight on screen, but Leaflet still clips per segment, and a two-point
 * line from pole to pole disappears entirely once either end is off screen. */
function graticule(step) {
  const lines = [];
  for (let lon = -180; lon <= 180; lon += step) {
    const pts = [];
    for (let lat = -90; lat <= 90; lat += 5) pts.push([lon, lat]);
    lines.push(pts);
  }
  for (let lat = -90 + step; lat < 90; lat += step) {
    const pts = [];
    for (let lon = -180; lon <= 180; lon += 5) pts.push([lon, lat]);
    lines.push(pts);
  }
  return { type: "FeatureCollection", features: lines.map((coordinates) => (
    { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates } })) };
}

/* City markers from the manifest's `cities` list.
 *
 * These are not decoration and not a duplicate of what a basemap labels: they
 * are exactly the points the pipeline sampled out of every forecast zarr, so a
 * dot here means "there is a per-model series for this place" and clicking it
 * gets that series rather than a value read off the picture. That is why the
 * dots stay visible at every zoom and under every basemap, while the *names*
 * come and go — the name is the redundant part, the dot is the affordance.
 *
 * The names are hidden below zoom 3, where thirty labels are a layer of noise
 * over the only thing the page exists to show, and under a basemap, which
 * prints its own.
 *
 * The halo (a CSS paint-order stroke) is not decoration either. A label sits
 * directly on a saturated field, so a plain glyph is legible over the pale part
 * of a ramp and invisible over the dark part — and which part it lands on
 * changes with the variable, the lead and the scale mode. */
function addCityLabels(m, mapId) {
  const cities = S.manifest.cities || [];
  if (!cities.length) return;
  const markers = [];
  for (const dx of COPIES) {
    for (const c of cities) {
      const mk = L.marker([c.lat, c.lon + dx], {
        keyboard: false,
        title: `${c.name} — click for every model's forecast`,
        icon: L.divIcon({
          className: "citylabel",
          html: `<i class="dot"></i><span>${c.name}</span>`,
          iconSize: [0, 0],
        }),
      });
      mk.on("click", (e) => {
        L.DomEvent.stop(e);        // don't also fire the map's own click popup
        openCityPopup(mapId, c, e.latlng);
      });
      markers.push(mk);
    }
  }
  const layer = L.layerGroup(markers).addTo(m);
  const sync = () => m.getContainer().classList.toggle("zoomed", m.getZoom() >= 3);
  m.on("zoomend", sync);
  sync();
}

function makeMap(id) {
  const m = L.map(id, {
    crs: L.CRS.EPSG4326,
    maxZoom: S.mapcfg.max_zoom,
    zoomControl: false,          // the floating panel is the chrome; see below
    // Basemap credit is rendered as page chrome instead — see syncAttribution()
    // for why a per-map control is the wrong shape here.
    attributionControl: false,
    // Fold the centre back into the middle copy when it crosses the
    // antimeridian. Because all three copies are drawn and identical, the fold
    // is invisible — it is what makes the panning continuous rather than
    // eventually running off the end of the outermost copy.
    worldCopyJump: true,
    maxBounds: PAN_BOUNDS,       // latitude only; longitude wraps, see COPIES
    maxBoundsViscosity: 1.0,     // hard edge, not a rubber band
  });
  coverWorld(m);
  if (id === "map1") L.control.zoom({ position: "bottomright" }).addTo(m);
  S.maps[id] = m;

  // A pane above the field for transparent basemaps. Leaflet's overlayPane
  // (where the field image sits) is 400 and markerPane is 600, so 450 puts
  // roads and labels over the field and still under the city markers. Clicks
  // have to fall through it or the whole map would stop responding.
  const top = m.createPane("basetop");
  top.style.zIndex = 450;
  top.style.pointerEvents = "none";

  // Geography on top of the field, no basemap underneath. Drawn weakest-first
  // so the coastline stays the strongest line on the map: a graticule or a
  // border that competes with it turns the land/sea edge into one line among
  // many, and the land/sea edge is what a reader actually navigates by.
  //
  // Shifted copies are built once here rather than rebuilt as the user pans:
  // re-parsing 600 KB of coastline on every moveend would be the whole cost of
  // the feature, paid repeatedly, for geometry that never changes.
  const grat = graticule(30);
  S.geo[id] = [];
  for (const dx of COPIES) {
    for (const l of [lineLayer(grat, dx, "grat"),
                     lineLayer(S.borders, dx, "border"),
                     lineLayer(S.coast, dx, "coast")]) {
      l.addTo(m);
      S.geo[id].push(l);          // kept so a basemap can take them off again
    }
  }
  addCityLabels(m, id);

  /* Keep the centre inside the primary world copy.
   *
   * worldCopyJump folds the centre mid-drag, which is what makes dragging
   * across the antimeridian seamless — but it only hooks the drag. Nothing
   * folds a centre that arrives any other way: a programmatic setView, an
   * inertia overshoot, a restored permalink. Land past +-540 and the view is
   * off the outermost copy and the map is blank, with no error to say so.
   *
   * The three-copy argument depends on "the centre is always in the middle
   * copy". worldCopyJump makes that true for drags; this makes it true
   * unconditionally. The fold is invisible because the copies are identical,
   * and it cannot recurse — after folding, the centre is in range and the
   * handler returns immediately. */
  m.on("moveend", () => {
    const c = m.getCenter();
    if (c.lng >= -180 && c.lng <= 180) return;
    m.setView([c.lat, ((c.lng + 180) % 360 + 360) % 360 - 180], m.getZoom(),
              { animate: false });
  });

  // Linked pan/zoom. The guard matters: without it each map's move handler
  // drives the other and they ring against each other indefinitely.
  m.on("move zoom", () => {
    if (S.syncing || S.panes !== 2) return;
    S.syncing = true;
    const other = id === "map1" ? S.maps.map2 : S.maps.map1;
    if (other) other.setView(m.getCenter(), m.getZoom(), { animate: false });
    S.syncing = false;
  });

  m.on("mousemove", (e) => showReadout(e.latlng));
  m.on("mouseout", () => { $("readout").textContent = ""; });
  m.on("click", (e) => openPointPopup(id, e.latlng));
  // Dismissing the popup has to drop the context with it, or the next redraw
  // would reopen the thing the reader just closed.
  m.on("popupclose", (e) => { if (e.popup === S.popup) S.pop = null; });
  // Only map1 drives auto mode: in side-by-side the two are view-locked, so
  // pane 2 would ask for the same switch a moment later.
  if (id === "map1") m.on("zoomend", () => autoBasemap(m));

  // Stretch mode is defined against the visible data, so the visible data
  // changing is what makes it stale. Only map1 drives: in side-by-side the two
  // are view-locked, so pane 2's moveend would recompute an identical scale.
  if (id === "map1") m.on("moveend", () => { if (S.stretch) draw(); });
  return m;
}

/* How much finer than the grid the overlay is rasterised.
 *
 * The overlay covers 360 deg of longitude; at 1 deg data and a 1440px viewport
 * the browser was stretching it about 4x, so 4 puts the raster at roughly one
 * canvas pixel per screen pixel at the default zoom. Higher costs encode time
 * on every redraw for detail the source does not contain. */
const UPSCALE = 4;

/* Draw a decoded field as an image overlay covering the whole globe.
 *
 * The array is rolled to -180, bicubically upsampled, then coloured — in that
 * order. Rolling first means the upsampler's longitude wrap is around the
 * dateline where it belongs; colouring last means every pixel is a colour the
 * ramp actually contains. */
function drawField(mapId, field, lut, lo, hi) {
  const g = S.grid;

  // The grid runs 0..360 east from Greenwich; Leaflet, the coastlines and
  // maxBounds all run -180..180. Drawing the array as-is puts the overlay at
  // 0..360, so everything west of the prime meridian has no field beneath it —
  // a blank half with coastlines floating on it. Roll each row so column 0
  // lands on -180 instead of renumbering the world.
  const shift = Math.round(((180 - g.lon_start) / g.lon_step)) % field.w;
  const rolled = { w: field.w, h: field.h, data: new Float32Array(field.w * field.h) };
  for (let y = 0; y < field.h; y++) {
    const row = y * field.w;
    for (let x = 0; x < field.w; x++) rolled.data[row + x] = field.data[row + (x + shift) % field.w];
  }

  const fine = upsample(rolled, UPSCALE);
  const c = document.createElement("canvas");
  c.width = fine.w; c.height = fine.h;
  const ctx = c.getContext("2d");
  const img = ctx.createImageData(fine.w, fine.h);
  const span = hi === lo ? 1 : hi - lo;
  for (let i = 0, p = 0; i < fine.data.length; i++, p += 4) {
    const v = fine.data[i];
    if (Number.isNaN(v)) { img.data[p + 3] = 0; continue; }      // missing -> transparent
    let k = Math.round(((v - lo) / span) * 255);
    k = k < 0 ? 0 : k > 255 ? 255 : k;
    const q = k * 3;
    img.data[p] = lut[q]; img.data[p + 1] = lut[q + 1]; img.data[p + 2] = lut[q + 2];
    // Fully opaque: transparency is the overlay's job now (see the imageOverlay
    // options below). Baking 235 in here as well would multiply with it, so the
    // slider would read 72% while the field was actually at 65%, and at rest the
    // drawn colours would sit slightly off the ones in the colourbar.
    img.data[p + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);

  const north = g.lat_start;
  const south = g.lat_start + g.lat_step * (g.height - 1);

  // One encode, three overlays. The raster is identical in every copy, so the
  // data URL is built once and shared — the copies cost three <img> elements,
  // not three PNG encodes.
  const url = c.toDataURL();
  const map = S.maps[mapId];
  (S.layers[mapId] || []).forEach((l) => map.removeLayer(l));
  S.layers[mapId] = COPIES.map((dx) => {
    const l = L.imageOverlay(url, [[south, -180 + dx], [north, 180 + dx]], {
      // Opacity lives on the overlay, not in the raster's alpha channel, so the
      // slider can move it without re-encoding a PNG per frame.
      opacity: S.fieldOpacity == null ? 1 : S.fieldOpacity,
      interactive: false,
      // The primary copy is tagged so scripts/check_map_render.js can find the
      // one whose bounds it expects to be -180..180. Without it, the gate's
      // querySelector picks whichever copy is first in the DOM.
      className: dx === 0 ? "fieldcopy fieldcopy-primary" : "fieldcopy",
    });
    l.addTo(map);
    l.bringToBack();
    return l;
  });
}

/* ---------- render ---------- */

function fileFor(model, kind, lead) {
  const node = S.f.variables[S.variable][kind];
  const prefix = kind === "error" ? "e" : "f";
  return `${DATA_DIR}/${S.f.dir}/${model}/${S.variable}/${prefix}${lead}.png`
    .replace("{model}", model) + (node && node.file ? "" : "");
}

function scaleForLead(kind, lead) {
  const node = S.f.variables[S.variable][kind];
  return node && node.scales ? node.scales[String(lead)] : null;
}

async function render() {
  buildModelSelects();
  buildVarTabs();
  renderProvenance();

  const leads = S.f.leads;
  const slider = $("leadslider");
  slider.max = String(leads.length - 1);
  if (S.leadIdx > leads.length - 1) S.leadIdx = 0;
  slider.value = String(S.leadIdx);
  slider.oninput = async () => { S.leadIdx = Number(slider.value); await draw(); };

  await draw();
  prefetchNeighbours();
}

async function draw() {
  const lead = S.f.leads[S.leadIdx];
  const kind = S.kind;
  const scale = scaleForLead(kind, lead);
  const meta = S.manifest.variables[S.variable] || {};
  const unit = unitFor(meta.units, S.units).label;

  $("leadlabel").textContent =
    `+${lead} h · valid ${validTime(lead)}Z` + (kind === "error" ? "  ·  model − truth" : "");

  const notes = $("notes"); notes.innerHTML = "";
  renderBaseNote();     // survives the rebuild; see the function for why
  if (!scale) {
    notes.appendChild(el("div", "note",
      `No ${kind} field for ${SHORT[S.variable] || S.variable} at +${lead} h in this init.`));
    return;
  }
  if (kind === "error") {
    const n = el("div", "note");
    n.innerHTML = `<b>Error is model − ${S.f.truth_source}.</b> The scale is ` +
      `symmetric about zero and grey means no error. This init is ` +
      `<b>${S.f.tier}</b>, so what counts as truth changes with init age — ` +
      `real-time inits are scored against GFS analysis, historic ones against ERA5.`;
    notes.appendChild(n);
  }

  const targets = S.panes === 2 ? [["map1", S.model], ["map2", S.model2]] : [["map1", S.model]];

  // Load every pane BEFORE choosing a colour scale. In stretch mode the scale
  // is derived from the data, and both panes have to end up on the same one —
  // deciding it inside the per-pane loop would give pane 2 a scale computed
  // without pane 1 in it, which is a comparison that quietly lies.
  const loaded = [];
  for (const [mapId, model] of targets) {
    const info = modelInfo(model);
    const title = $(mapId === "map1" ? "t1" : "t2");
    title.innerHTML = "";
    const sw = el("span", "sw"); sw.style.background = `var(${info.css_var}, ${info.color})`;
    title.append(sw, document.createTextNode(
      `${info.label} · ${meta.label || S.variable}${unit ? ` (${unit})` : ""} · +${lead} h`));
    try {
      const f = await loadField(fileFor(model, kind, lead), scale, S.enc);
      S.fields = S.fields || {}; S.fields[mapId] = f;
      loaded.push([mapId, f]);
    } catch (e) {
      title.append(document.createTextNode("  — field unavailable"));
      console.warn(e.message);
    }
  }

  const shown = S.stretch ? stretchedScale(loaded, kind, scale) : scale;
  S.shownScale = shown;

  if (S.stretch && kind !== "error" && isAnchored(meta.palette) && shown !== scale) {
    const n = el("div", "note");
    n.innerHTML = "<b>Colours are stretched to this view.</b> They show local " +
      "contrast, not absolute temperature — read values off the bar, not the hue.";
    notes.appendChild(n);
  }
  const colour = scaleFor(kind, shown[0], shown[1], meta.palette);
  S.colour = colour;          // test hook: check_map_render.js re-derives pixels
  const lut = buildLUT(colour, shown[0], shown[1]);
  loaded.forEach(([mapId, f]) => drawField(mapId, f, lut, shown[0], shown[1]));

  renderColorbar(kind, shown, meta.units, meta.palette);
  refreshPopup();
}

/* The colour scale for "stretch to view": the 2nd-98th percentile of whatever
 * is currently on screen, unioned across panes so a side-by-side comparison
 * stays honest.
 *
 * Error fields are exempt from the union-and-use rule in one respect: the range
 * is re-symmetrised about zero afterwards. divergingScale() would do that
 * anyway, but the colourbar ticks read the range directly, and a bar labelled
 * -3.1 .. +4.7 over a ramp whose neutral colour sits at zero is a bar that
 * misstates where zero is.
 *
 * Falls back to the manifest's global scale whenever the view contains no
 * finite data — over the poles at high zoom, say — rather than producing a
 * degenerate scale nothing can be read against. */
function stretchedScale(loaded, kind, globalScale) {
  const b = S.maps.map1.getBounds();
  // Longitude is unbounded now, so the view can sit on a wrap copy (west 190,
  // east 250) or span more than one whole world. Fold it back to -180..180 for
  // the cell test; a view wider than 360 deg means every column is visible.
  const wrap = (x) => { const y = ((x + 180) % 360 + 360) % 360 - 180; return y; };
  const spansWorld = b.getEast() - b.getWest() >= 360;
  const bounds = {
    north: b.getNorth(), south: b.getSouth(),
    west: spansWorld ? -180 : wrap(b.getWest()),
    east: spansWorld ? 180 : wrap(b.getEast()),
  };
  let lo = Infinity, hi = -Infinity;
  for (const [, f] of loaded) {
    const e = extentInBounds(f, S.grid, bounds);
    if (!e) continue;
    lo = Math.min(lo, e[0]); hi = Math.max(hi, e[1]);
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi === lo) return globalScale;
  if (kind === "error") { const m = Math.max(Math.abs(lo), Math.abs(hi)); return [-m, m]; }
  return [lo, hi];
}

function validTime(lead) {
  const t = new Date(S.f.init_time);
  t.setUTCHours(t.getUTCHours() + lead);
  return t.toISOString().slice(0, 16).replace("T", " ");
}

function prefetchNeighbours() {
  const lead = S.f.leads[S.leadIdx + 1];
  if (lead == null) return;
  const scale = scaleForLead(S.kind, lead);
  if (scale) loadField(fileFor(S.model, S.kind, lead), scale, S.enc).catch(() => {});
}

/* `storedUnit` is the manifest's unit, not a display label: the tick values come
 * out of the scale in stored units and have to be converted here, with the
 * absolute/delta distinction that units.js exists to enforce. */
function renderColorbar(kind, scale, storedUnit, palette) {
  const ramp = rampFor(kind, palette);
  const stops = [];
  for (let i = 0; i <= 10; i++) stops.push(`${rgbCss(ramp(i / 10))} ${i * 10}%`);
  $("cbar").style.background = `linear-gradient(90deg, ${stops.join(",")})`;

  const u = unitFor(storedUnit, S.units);
  const conv = kind === "error" ? u.delta : u.abs;
  const ticks = colorbarTicks(kind, scale[0], scale[1]).map(conv);
  const box = $("cbarticks"); box.innerHTML = "";
  ticks.forEach((t) => box.appendChild(el("span", null,
    Math.abs(t) >= 100 ? t.toFixed(0) : t.toFixed(u.decimals))));
  // Just the unit. The "symmetric about zero, grey means no error" explanation
  // used to live here, and at 43 characters it wrapped the whole bottom bar
  // onto a second line, which then covered the pane labels. It belongs with the
  // rest of the error semantics in the note at top-left anyway.
  $("cbarunit").textContent = u.label;
}

/* ---------- popups ----------
 *
 * Two kinds, and the difference between them is the point of the feature.
 *
 * Clicking anywhere reads the *picture*: one number per pane, nearest-cell out
 * of the 1 deg PNG that is on screen. Clicking a city marker reads the *store*:
 * every model's bilinearly-sampled value at that exact location plus the truth
 * it was scored against, straight out of points/<init>/<city>.json — the same
 * document compare.html draws. So the city popup can rank models and show each
 * one's error, which no amount of sampling the raster could do, and its numbers
 * will differ slightly from the hover readout for exactly that reason.
 */

const popCache = new Map();

/* The point store is indexed by init *time*; the map is indexed by init *key*
 * (a directory name). They are usually the same init and occasionally not —
 * fields.py and export.py prune on their own schedules — so this looks it up
 * rather than assuming, and returns null when the field init has no point data.
 */
function pointsDirFor(initTime) {
  const e = (S.manifest.inits || []).find((i) => i.init_time === initTime);
  return e && e.points ? e.points.dir : null;
}

async function cityDoc(cityId) {
  const dir = pointsDirFor(S.f.init_time);
  if (!dir) return null;
  const key = `${dir}/${cityId}`;
  if (!popCache.has(key)) {
    popCache.set(key, fetch(`${DATA_DIR}/${key}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null));
  }
  return popCache.get(key);
}

function openPointPopup(mapId, latlng) {
  S.pop = { kind: "point", mapId, latlng };
  showPopup();
}

function openCityPopup(mapId, city, latlng) {
  S.pop = { kind: "city", mapId, city, latlng };
  showPopup();
}

/* Re-render whatever popup is open. Called from draw(), so dragging the lead
 * slider or switching model updates an open popup in place instead of leaving a
 * stale number pinned to the map — which would be worse than closing it, since
 * nothing about a stale popup looks stale.
 *
 * `isOpen` is asked of the popup, not the map: L.Map has openPopup/closePopup
 * but no isPopupOpen — that one belongs to L.Layer, and calling it on the map
 * throws. */
function refreshPopup() {
  if (S.pop && S.popup && S.popup.isOpen()) showPopup();
}

async function showPopup() {
  const p = S.pop;
  const m = S.maps[p.mapId];
  if (!m) return;
  const node = p.kind === "city" ? await cityBody(p.city) : pointBody(p.latlng);
  // The await above can outlive the popup: a click elsewhere while the city
  // document is still in flight would otherwise reopen the one being replaced.
  if (S.pop !== p) return;

  // Re-content the open popup rather than replacing it. Scrubbing the lead
  // slider calls this on every frame, and openOn() would restart the open
  // animation each time — the popup would flicker and fight the autopan.
  if (S.popup && S.popup.isOpen() && S.popup._map === m) {
    S.popup.setContent(node);
    return;
  }
  const pop = L.popup({ className: "mappop", maxWidth: 340, autoPanPadding: [24, 90] })
    .setLatLng(p.latlng)
    .setContent(node);
  // Assign before opening: openOn() closes the previous popup, and the
  // popupclose handler has to be able to tell that one from this one.
  S.popup = pop;
  pop.openOn(m);
}

const popHead = (title, sub) => {
  const h = el("div", "pophead");
  h.append(el("b", null, title), el("span", null, sub));
  return h;
};

/* The value a pane is currently drawing, at one location. */
function pointBody(latlng) {
  const meta = S.manifest.variables[S.variable] || {};
  const u = unitFor(meta.units, S.units);
  const conv = S.kind === "error" ? u.delta : u.abs;
  const lead = S.f.leads[S.leadIdx];

  const box = el("div");
  box.append(popHead(`${latlng.lat.toFixed(2)}°, ${wrapLon(latlng.lng).toFixed(2)}°`,
                     `${meta.label || S.variable} · +${lead} h · valid ${validTime(lead)}Z`));

  const targets = S.panes === 2 ? [["map1", S.model], ["map2", S.model2]] : [["map1", S.model]];
  const tbl = el("table", "poptbl");
  for (const [id, model] of targets) {
    const f = (S.fields || {})[id];
    const v = f ? sampleAt(f, S.grid, latlng.lat, latlng.lng) : NaN;
    const info = modelInfo(model);
    const tr = el("tr");
    const sw = el("span", "sw"); sw.style.background = `var(${info.css_var}, ${info.color})`;
    const td = el("td", "name"); td.append(sw, document.createTextNode(info.label));
    tr.append(td, el("td", "val", Number.isNaN(v) ? "—" : `${conv(v).toFixed(u.decimals)} ${u.label}`));
    tbl.append(tr);
  }
  box.append(tbl);
  box.append(el("div", "popfoot",
    S.kind === "error"
      ? `model − ${S.f.truth_source}, nearest cell of the ${S.grid.resolution_deg}° grid`
      : `nearest cell of the ${S.grid.resolution_deg}° grid`));
  return box;
}

/* Every model at a sampled city, ranked, against the truth it was scored on. */
async function cityBody(city) {
  const lead = S.f.leads[S.leadIdx];
  const meta = S.manifest.variables[S.variable] || {};
  const u = unitFor(meta.units, S.units);
  const doc = await cityDoc(city.id);

  const box = el("div");
  const i = doc ? doc.leads.indexOf(lead) : -1;
  box.append(popHead(city.name,
    `${meta.label || S.variable} · +${lead} h · valid ` +
    `${(doc && i >= 0 ? doc.valid_times[i].slice(0, 16).replace("T", " ") : validTime(lead))}Z`));

  // No point document for this init, or no sample at this lead: fall back to
  // the raster rather than showing an empty popup. Say which it is — a reader
  // comparing this against compare.html deserves to know they are different
  // numbers from different sources.
  if (!doc || i < 0) {
    const f = (S.fields || {}).map1;
    const v = f ? sampleAt(f, S.grid, city.lat, city.lon) : NaN;
    const conv = S.kind === "error" ? u.delta : u.abs;
    const tbl = el("table", "poptbl");
    const tr = el("tr");
    tr.append(el("td", "name", modelInfo(S.model).label),
              el("td", "val", Number.isNaN(v) ? "—" : `${conv(v).toFixed(u.decimals)} ${u.label}`));
    tbl.append(tr);
    box.append(tbl, el("div", "popfoot",
      "No point series for this init — value read off the map instead."));
    return box;
  }

  const truth = (doc.truth[S.variable] || {});
  const tv = truth.status === "ok" && truth.values ? truth.values[i] : null;

  const rows = [];
  for (const id of doc.models_expected) {
    const s = (doc.models[id] || {})[S.variable];
    if (!s || s.status !== "ok" || !s.values) continue;
    const v = s.values[i];
    if (v == null || !isFinite(v)) continue;
    rows.push({ id, v, err: tv == null ? null : v - tv });
  }
  // Ranked by |error| when there is a truth to rank against, and that is the
  // whole reason to show every model at once here; without truth, fall back to
  // value order so the list is at least stable and readable.
  rows.sort((a, b) => (tv == null ? a.v - b.v : Math.abs(a.err) - Math.abs(b.err)));

  const tbl = el("table", "poptbl");
  if (tv != null) {
    const tr = el("tr", "truth");
    tr.append(el("td", "name", `${S.f.truth_source} (truth)`),
              el("td", "val", `${u.abs(tv).toFixed(u.decimals)} ${u.label}`),
              el("td", "err", ""));
    tbl.append(tr);
  }
  for (const r of rows) {
    const info = modelInfo(r.id);
    const tr = el("tr");
    const sw = el("span", "sw"); sw.style.background = `var(${info.css_var}, ${info.color})`;
    const td = el("td", "name"); td.append(sw, document.createTextNode(info.label));
    // Errors are differences, so they take the delta conversion — u.abs here
    // would render a 2 K miss as -271 °C. units.js exists for this.
    const err = r.err == null ? ""
      : `${r.err >= 0 ? "+" : "−"}${Math.abs(u.delta(r.err)).toFixed(u.decimals)}`;
    tr.append(td, el("td", "val", `${u.abs(r.v).toFixed(u.decimals)} ${u.label}`),
              el("td", "err", err));
    tbl.append(tr);
  }
  box.append(tbl);

  if (!rows.length) box.append(el("div", "popfoot", "No model has a series here at this lead."));
  else if (tv == null) {
    box.append(el("div", "popfoot",
      truth.status === "truth_pending"
        ? `No truth yet at +${lead} h — this init is ${doc.tier} and only verified `
          + `through ${doc.truth_valid_through.slice(0, 16).replace("T", " ")}Z, so there is no error column.`
        : "No truth for this variable, so there is no error column."));
  }

  const foot = el("div", "popfoot");
  const a = el("a", null, `All ${doc.leads.length} leads for ${city.name} →`);
  a.href = `compare.html?city=${encodeURIComponent(city.id)}&init=${encodeURIComponent(doc.init_time)}`;
  foot.append(a);
  box.append(foot);
  return box;
}

const wrapLon = (x) => ((x + 180) % 360 + 360) % 360 - 180;

function showReadout(latlng) {
  const f = (S.fields || {}).map1;
  if (!f) return;
  const v = sampleAt(f, S.grid, latlng.lat, latlng.lng);
  const meta = S.manifest.variables[S.variable] || {};
  const u = unitFor(meta.units, S.units);
  const conv = S.kind === "error" ? u.delta : u.abs;
  const lon = ((latlng.lng % 360) + 360) % 360;
  $("readout").textContent = Number.isNaN(v)
    ? `${latlng.lat.toFixed(1)}°, ${lon.toFixed(1)}° — no data`
    : `${latlng.lat.toFixed(1)}°, ${lon.toFixed(1)}°  ·  ` +
      `${conv(v).toFixed(u.decimals)} ${u.label}` +
      (S.kind === "error" ? "  (model − truth)" : "");
}

function renderProvenance() {
  $("prov").innerHTML =
    `<span>init <b>${S.f.init_time.slice(0, 16).replace("T", " ")}Z</b></span>` +
    `<span>grid <b>${S.grid.width}×${S.grid.height}</b> @ ${S.grid.resolution_deg}°</span>` +
    `<span>init source <b>${S.f.init_source}</b></span>` +
    `<span>truth <b>${S.f.truth_source}</b></span>` +
    `<span>tier <b>${S.f.tier.toUpperCase()}</b></span>`;
}

// Test hook for scripts/check_map_render.js. The orientation of a drawn field
// cannot be checked from outside the page — a north-up and a south-up render
// are both perfectly plausible pictures — so the gate needs the decoded array
// and the overlay bounds that were actually used.
window.__S = S;
// Test hook: check_map_render.js swaps a basemap's URL for an unresolvable host
// and re-applies, to force the tile-failure path without waiting for the real
// service to misbehave.
S.applyBasemap = applyBasemap;
window.__sampleAt = (lat, lon) => sampleAt(S.fields.map1, S.grid, lat, lon);
// The primary copy — the one anchored at -180..180. The wrap copies either side
// of it are the same raster at +-360 and would report shifted bounds.
window.__overlayBounds = () => {
  const l = (S.layers.map1 || [])[COPIES.indexOf(0)];
  if (!l) return null;
  const b = l.getBounds();
  return { north: b.getNorth(), south: b.getSouth(), west: b.getWest(), east: b.getEast() };
};

window.__mapReady = boot()
  .then(() => { window.__mapOK = true; })
  .catch((e) => {
    window.__mapError = e.message;
    fatal("Could not load the map data", String(e.message));
    console.error(e);
  });
