/* map.html — global forecast and error fields.
 *
 * Leaflet with L.CRS.EPSG4326 and NO basemap tile layer: the field is drawn to a
 * canvas overlay and a Natural Earth coastline is stroked on top. That avoids
 * the OSM tile-usage question entirely, keeps the page self-contained, and for a
 * global scientific field a clean outline reads better than street cartography.
 *
 * Everything about the grid and the encoding comes from manifest.json. Nothing
 * here infers geometry from array shape — that is how a north-up field gets
 * drawn south-up while every numeric check still passes.
 */

import { loadField, sampleAt, clearFieldCache } from "./field.js";
import { scaleFor, colorbarTicks, rgbCss, SEQUENTIAL, DIVERGING } from "./colormap.js";

const DATA_DIR = "data";
const S = { panes: 1, kind: "forecast" };

const $ = (id) => document.getElementById(id);
const el = (t, cls, txt) => { const e = document.createElement(t); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

const SHORT = { z500: "Z500", t850: "T850", t2m: "T2M", msl: "MSL",
                u10m: "U10", v10m: "V10", tp06: "Precip", tp: "Precip" };

/* ---------- boot ---------- */

async function boot() {
  const [manifest, models, coast] = await Promise.all([
    fetch(`${DATA_DIR}/manifest.json`).then((r) => r.json()),
    fetch(`${DATA_DIR}/models.json`).then((r) => r.json()),
    fetch("lib/vendor/coastlines-110m.json").then((r) => r.json()),
  ]);
  S.manifest = manifest; S.models = models; S.coast = coast;

  const fieldInits = Object.keys(manifest.fields || {});
  if (!fieldInits.length) {
    document.querySelector("main").innerHTML =
      '<p class="empty">No field data yet. Run ' +
      '<code>python -m scoreboard.fields</code> to generate <code>docs/data/fields/</code>.</p>';
    return;
  }
  S.initKeys = fieldInits.sort().reverse();
  S.initKey = S.initKeys[0];
  selectInit();

  buildInitSelect();
  buildViewTabs();
  buildPanelTabs();
  makeMap("map1"); makeMap("map2");

  // The map fills the viewport, so a resize changes its size rather than the
  // page's scroll height — Leaflet has to be told.
  // A resize changes what "cover" means, so the minimum zoom has to be
  // recomputed — otherwise widening the window reveals blank page beside the
  // field at a zoom that used to fill it.
  window.addEventListener("resize", () => {
    Object.values(S.maps).forEach((m) => { m.invalidateSize(); coverWorld(m, true); });
  });

  // Narrow screens: the control panel covers too much of the map to stay open.
  const btn = $("panelbtn");
  if (btn) btn.onclick = () => {
    const p = $("ctlpanel");
    p.hidden = !p.hidden;
    setTimeout(() => Object.values(S.maps).forEach((m) => m.invalidateSize()), 0);
  };
  if (window.matchMedia("(max-width: 820px)").matches) $("ctlpanel").hidden = true;

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

function buildPanelTabs() {
  $("paneltabs").querySelectorAll("button").forEach((b) => {
    b.onclick = async () => {
      S.panes = Number(b.dataset.panes);
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

const WORLD = L.latLngBounds([[-90, -180], [90, 180]]);

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
 * Combined with maxBounds it makes it impossible to see anything that is not
 * field. */
function coverWorld(m, keepCenter = false) {
  const z = m.getBoundsZoom(WORLD, true);
  const center = keepCenter && m._loaded ? m.getCenter() : L.latLng(0, 0);
  m.setMinZoom(z);
  if (!m._loaded || m.getZoom() < z) m.setView(center, z, { animate: false });
  else m.setView(center, m.getZoom(), { animate: false });
}

function makeMap(id) {
  const m = L.map(id, {
    crs: L.CRS.EPSG4326,
    maxZoom: 6,
    zoomControl: false,          // the floating panel is the chrome; see below
    attributionControl: false,
    worldCopyJump: false,
    maxBounds: WORLD,            // no panning into the void past +-180
    maxBoundsViscosity: 1.0,     // hard edge, not a rubber band
  });
  coverWorld(m);
  if (id === "map1") L.control.zoom({ position: "bottomright" }).addTo(m);
  S.maps[id] = m;

  // Coastlines on top of the field, no basemap underneath.
  L.geoJSON(S.coast, { className: "coast", interactive: false }).addTo(m);

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
  return m;
}

/* Draw a decoded field as an image overlay covering the whole globe. The canvas
 * is written at native grid resolution, one pixel per cell, and Leaflet scales
 * it — so switching lead is a redraw of a cached array, not a refetch. */
function drawField(mapId, field, colour) {
  const g = S.grid;
  const c = document.createElement("canvas");
  c.width = field.w; c.height = field.h;
  const ctx = c.getContext("2d");
  const img = ctx.createImageData(field.w, field.h);

  // The grid runs 0..360 east from Greenwich; Leaflet, the coastlines and
  // maxBounds all run -180..180. Drawing the array as-is puts the overlay at
  // 0..360, so everything west of the prime meridian has no field beneath it —
  // a blank half with coastlines floating on it. Roll each row so column 0
  // lands on -180 instead of renumbering the world.
  const shift = Math.round(((180 - g.lon_start) / g.lon_step)) % field.w;
  for (let y = 0; y < field.h; y++) {
    for (let x = 0; x < field.w; x++) {
      const src = (x + shift) % field.w;
      const v = field.data[y * field.w + src];
      const p = (y * field.w + x) * 4;
      if (Number.isNaN(v)) { img.data[p + 3] = 0; continue; }   // missing -> transparent
      const rgb = colour(v);
      img.data[p] = rgb[0]; img.data[p + 1] = rgb[1]; img.data[p + 2] = rgb[2];
      img.data[p + 3] = 235;
    }
  }
  ctx.putImageData(img, 0, 0);

  const north = g.lat_start;
  const south = g.lat_start + g.lat_step * (g.height - 1);
  const bounds = [[south, -180], [north, 180]];

  const map = S.maps[mapId];
  if (S.layers[mapId]) map.removeLayer(S.layers[mapId]);
  S.layers[mapId] = L.imageOverlay(c.toDataURL(), bounds, { opacity: 1, interactive: false });
  S.layers[mapId].addTo(map);
  S.layers[mapId].bringToBack();
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
  const unit = meta.units || "";

  $("leadlabel").textContent =
    `+${lead} h · valid ${validTime(lead)}Z` + (kind === "error" ? "  ·  model − truth" : "");

  const notes = $("notes"); notes.innerHTML = "";
  if (!scale) {
    notes.appendChild(el("div", "note",
      `No ${kind} field for ${SHORT[S.variable] || S.variable} at +${lead} h in this init.`));
    return;
  }
  if (kind === "error") {
    const n = el("div", "note");
    n.innerHTML = `<b>Error is model − ${S.f.truth_source}.</b> This init is ` +
      `<b>${S.f.tier}</b>, so what counts as truth changes with init age — ` +
      `real-time inits are scored against GFS analysis, historic ones against ERA5.`;
    notes.appendChild(n);
  }

  const colour = scaleFor(kind, scale[0], scale[1]);
  S.colour = colour;          // test hook: check_map_render.js re-derives pixels
  const targets = S.panes === 2 ? [["map1", S.model], ["map2", S.model2]] : [["map1", S.model]];

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
      drawField(mapId, f, colour);
    } catch (e) {
      title.append(document.createTextNode("  — field unavailable"));
      console.warn(e.message);
    }
  }

  renderColorbar(kind, scale, unit);
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

function renderColorbar(kind, scale, unit) {
  const ramp = kind === "error" ? DIVERGING : SEQUENTIAL;
  const stops = [];
  for (let i = 0; i <= 10; i++) stops.push(`${rgbCss(ramp(i / 10))} ${i * 10}%`);
  $("cbar").style.background = `linear-gradient(90deg, ${stops.join(",")})`;

  const ticks = colorbarTicks(kind, scale[0], scale[1]);
  const box = $("cbarticks"); box.innerHTML = "";
  ticks.forEach((t) => box.appendChild(el("span", null,
    Math.abs(t) >= 100 ? t.toFixed(0) : t.toFixed(1))));
  $("cbarunit").textContent = kind === "error"
    ? `${unit} — symmetric about zero (grey = no error)`
    : unit;
}

function showReadout(latlng) {
  const f = (S.fields || {}).map1;
  if (!f) return;
  const v = sampleAt(f, S.grid, latlng.lat, latlng.lng);
  const meta = S.manifest.variables[S.variable] || {};
  const lon = ((latlng.lng % 360) + 360) % 360;
  $("readout").textContent = Number.isNaN(v)
    ? `${latlng.lat.toFixed(1)}°, ${lon.toFixed(1)}° — no data`
    : `${latlng.lat.toFixed(1)}°, ${lon.toFixed(1)}°  ·  ${v.toFixed(2)} ${meta.units || ""}` +
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
window.__sampleAt = (lat, lon) => sampleAt(S.fields.map1, S.grid, lat, lon);
window.__overlayBounds = () => {
  const l = S.layers.map1;
  if (!l) return null;
  const b = l.getBounds();
  return { north: b.getNorth(), south: b.getSouth(), west: b.getWest(), east: b.getEast() };
};

window.__mapReady = boot()
  .then(() => { window.__mapOK = true; })
  .catch((e) => {
    window.__mapError = e.message;
    document.querySelector("main").innerHTML =
      `<p class="empty">Could not load the map data: ${e.message}</p>`;
    console.error(e);
  });
