/* compare.html — one city, every model, against the truth it was scored on.
 *
 * Data comes from docs/data/ (manifest.json + points/<init>/<city>.json), never
 * inlined: see PLAN_EXPLORER.md §3. Model colours come from models.json, which
 * publish.py generates from config.yaml — never hardcode them here, or this page
 * and the leaderboard drift apart the way index.html's hand-curated array did.
 */

const DATA_DIR = "data";
const S = {};                      // manifest, models, city doc, selection

/* ---------- helpers (same primitives index.html draws with — no new library) */

function svgEl(tag, attrs) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}

function niceTicks(min, max, n) {
  const span = max - min || 1;
  const step0 = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(v);
  return out;
}

function fmt(v, d) {
  if (v == null || !isFinite(v)) return "–";
  if (d != null) return v.toFixed(d);
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 100) return v.toFixed(1);
  if (a >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

function tickLabel(v) {
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (a >= 10 || v === Math.round(v)) return String(Math.round(v * 10) / 10);
  return String(Math.round(v * 100) / 100);
}

const el = (t, cls, txt) => {
  const e = document.createElement(t);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};

/* Precipitation is drawn as accumulation bars, not a line: it is a per-interval
 * total, and a line between two 6 h accumulations implies an instantaneous rate
 * that was never forecast. */
const isPrecip = (v) => v === "tp06" || v === "tp";

const modelInfo = (id) => S.models.find((m) => m.id === id) || { id, label: id, color: "#888" };

// Short tab labels, matching the leaderboard's so the two pages name the same
// variable the same way. The manifest carries `label`/`units`/`decimals` but no
// abbreviation, and "U10M" reads worse than "U10" in a row of tabs.
const SHORT = { z500: "Z500", t850: "T850", t2m: "T2M", msl: "MSL",
                u10m: "U10", v10m: "V10", tp06: "Precip", tp: "Precip" };

const varMeta = (v) => {
  const m = S.manifest.variables[v] || {};
  return { label: m.label || v, unit: m.units || "", decimals: m.decimals, short: SHORT[v] || v.toUpperCase() };
};

/* ---------- data ---------- */

async function boot() {
  const [manifest, models] = await Promise.all([
    fetch(`${DATA_DIR}/manifest.json`).then((r) => r.json()),
    fetch(`${DATA_DIR}/models.json`).then((r) => r.json()),
  ]);
  S.manifest = manifest;
  S.models = models;

  if (!manifest.inits || !manifest.inits.length) {
    document.querySelector("main").innerHTML =
      '<p class="empty">No init has complete point data yet. ' +
      "Run <code>python -m scoreboard.export</code> after a verification pass.</p>";
    return;
  }

  // Newest init first — the live board leads with the most recent scores.
  S.inits = manifest.inits.slice().sort((a, b) => b.init_time.localeCompare(a.init_time));

  // ?city= and ?init= let map.html's popups link here without losing the place
  // the reader clicked. Both are validated against the manifest rather than
  // trusted: a stale bookmark must land on the default page, not fetch a
  // points/<init>/<city>.json that does not exist and fail blank.
  const q = new URLSearchParams(location.search);
  S.init = S.inits.find((i) => i.init_time === q.get("init")) || S.inits[0];
  const asked = q.get("city");
  const known = (manifest.cities || []).some((c) => c.id === asked)
    && S.init.points.cities.includes(asked);
  S.cityId = known ? asked : (manifest.cities[0] || {}).id;
  S.variable = null;
  S.active = null;

  buildInitSelect();
  buildCitySelect();
  await loadCity();
}

async function loadCity() {
  const dir = S.init.points.dir;
  S.doc = await fetch(`${DATA_DIR}/${dir}/${S.cityId}.json`).then((r) => r.json());

  const vars = S.init.variables;
  if (!S.variable || !vars.includes(S.variable)) S.variable = vars.includes("t2m") ? "t2m" : vars[0];

  // Default to a small active set. Nine series exceed what one categorical
  // palette can keep separable — validated: in dark mode aifs #a8892c and
  // fengwu #c98500 sit at ΔE 5.4 for normal vision, far below the 15 floor. The
  // page therefore starts legible and lets you opt into more, rather than
  // painting every model at once and hoping.
  if (!S.active) {
    const pref = ["graphcast_oper", "aifs", "aurora", "fuxi", "persistence"];
    const have = S.doc.models_expected;
    S.active = new Set(pref.filter((m) => have.includes(m)).slice(0, 5));
    if (!S.active.size) have.slice(0, 4).forEach((m) => S.active.add(m));
  }
  render();
}

/* ---------- controls ---------- */

function buildInitSelect() {
  const sel = document.getElementById("initsel");
  sel.innerHTML = "";
  S.inits.forEach((i) => {
    const o = el("option", null, `${i.init_time.slice(0, 16).replace("T", " ")}Z · ${i.tier}`);
    o.value = i.init_time;
    sel.appendChild(o);
  });
  sel.value = S.init.init_time;
  sel.onchange = async () => {
    S.init = S.inits.find((i) => i.init_time === sel.value);
    S.active = null;
    await loadCity();
  };
}

function buildCitySelect() {
  const sel = document.getElementById("citysel");
  sel.innerHTML = "";
  S.manifest.cities.slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach((c) => {
      const o = el("option", null, c.name);
      o.value = c.id;
      sel.appendChild(o);
    });
  sel.value = S.cityId;
  sel.onchange = async () => { S.cityId = sel.value; await loadCity(); };
}

function renderVarTabs() {
  const box = document.getElementById("vartabs");
  box.innerHTML = "";
  S.init.variables.forEach((v) => {
    const meta = varMeta(v);
    const b = el("button", S.variable === v ? "on" : null, meta.short);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(S.variable === v));
    b.title = meta.label || v;
    b.onclick = () => { S.variable = v; render(); };
    box.appendChild(b);
  });
}

function renderChips() {
  const box = document.getElementById("modelchips");
  box.innerHTML = "";
  S.doc.models_expected.forEach((id) => {
    const info = modelInfo(id);
    const on = S.active.has(id);
    const b = el("button");
    b.setAttribute("aria-pressed", String(on));
    const sw = el("span", info.baseline ? "swatch dash" : "swatch");
    sw.style.background = `var(${info.css_var}, ${info.color})`;
    b.append(sw, document.createTextNode(info.label));
    b.onclick = () => {
      if (on) S.active.delete(id); else S.active.add(id);
      render();
    };
    box.appendChild(b);
  });
}

/* ---------- series assembly ---------- */

function seriesFor(variable) {
  const out = [];
  for (const id of S.doc.models_expected) {
    if (!S.active.has(id)) continue;
    const s = (S.doc.models[id] || {})[variable];
    if (!s || s.status !== "ok" || !s.values) continue;
    const info = modelInfo(id);
    out.push({ id, label: info.label, color: `var(${info.css_var}, ${info.color})`,
               raw: info.color, values: s.values, baseline: !!info.baseline,
               width: info.baseline ? 1.5 : 2 });
  }
  return out;
}

/* The two ways a series can be absent are different facts and the page must say
 * which: a model with no precipitation head never had a forecast, while a
 * real-time init simply has no IMERG truth yet (verify.py:146-149). Rendering
 * both as a gap would imply the model failed. */
function absenceNotes(variable) {
  const noVar = [], pending = [], unavailable = [];
  for (const id of S.doc.models_expected) {
    const s = (S.doc.models[id] || {})[variable];
    if (!s) continue;
    if (s.status === "no_variable") noVar.push(modelInfo(id).label);
    else if (s.status === "truth_pending") pending.push(modelInfo(id).label);
    else if (s.status === "unavailable") unavailable.push(modelInfo(id).label);
  }
  return { noVar, pending, unavailable };
}

function truthFor(variable) {
  const t = S.doc.truth[variable];
  if (!t) return { status: "unavailable", values: null };
  return t;
}

/* ---------- chart ---------- */

const PAD = { l: 66, r: 108, t: 18, b: 44 };
const W = 900, H = 400;

function render() {
  renderVarTabs();
  renderChips();
  renderProvenance();

  const variable = S.variable;
  const leads = S.doc.leads;
  const series = seriesFor(variable);
  const truth = truthFor(variable);
  const svg = document.getElementById("chart");
  svg.innerHTML = "";

  const meta = varMeta(variable);
  const unit = meta.unit ? ` (${meta.unit})` : "";
  document.getElementById("charttitle").textContent =
    `${meta.label || variable}${unit} at ${S.doc.city.name} — forecast from ` +
    `${S.doc.init_time.slice(0, 16).replace("T", " ")}Z. ` +
    (truth.status === "ok"
      ? "The heavy grey line is the truth each model was scored against."
      : "No truth line: " + explainTruth(truth.status) + ".");

  renderNotes(variable, truth);

  const vals = [];
  series.forEach((s) => s.values.forEach((v) => v != null && isFinite(v) && vals.push(v)));
  if (truth.values) truth.values.forEach((v) => v != null && isFinite(v) && vals.push(v));
  if (!vals.length) {
    svg.appendChild(svgEl("text", { x: W / 2, y: H / 2, "text-anchor": "middle",
      class: "axis-label" })).textContent = "No data for this selection.";
    renderTable(variable, series, truth);
    return;
  }

  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (isPrecip(variable)) lo = 0;
  const pad = (hi - lo) * 0.08 || 1;
  hi += pad;
  if (!isPrecip(variable)) lo -= pad;

  const x = (i) => PAD.l + (i / (leads.length - 1)) * (W - PAD.l - PAD.r);
  const y = (v) => H - PAD.b - ((v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);

  // grid + y axis
  niceTicks(lo, hi, 5).forEach((t) => {
    if (t < lo || t > hi) return;
    svg.appendChild(svgEl("line", { x1: PAD.l, x2: W - PAD.r, y1: y(t), y2: y(t), class: "gridline" }));
    const lb = svgEl("text", { x: PAD.l - 10, y: y(t) + 4, "text-anchor": "end", class: "axis-label" });
    lb.textContent = tickLabel(t);
    svg.appendChild(lb);
  });
  svg.appendChild(svgEl("line", { x1: PAD.l, x2: W - PAD.r, y1: H - PAD.b, y2: H - PAD.b, class: "axisline" }));

  // x axis — label every 24 h so the day boundaries read cleanly
  leads.forEach((lh, i) => {
    if (lh % 24) return;
    const lb = svgEl("text", { x: x(i), y: H - PAD.b + 18, "text-anchor": "middle", class: "axis-label" });
    lb.textContent = `+${lh}h`;
    svg.appendChild(lb);
  });
  const xl = svgEl("text", { x: (PAD.l + W - PAD.r) / 2, y: H - 6, "text-anchor": "middle", class: "axis-label" });
  xl.textContent = "lead time";
  svg.appendChild(xl);

  if (isPrecip(variable)) drawBars(svg, series, leads, x, y, lo);
  else drawLines(svg, series, leads, x, y);

  // Truth last so it sits on top, and heavier than any model line.
  if (truth.values) {
    const d = truth.values.map((v, i) => (v == null ? null : `${x(i)},${y(v)}`))
      .filter(Boolean).join(" L ");
    if (d) {
      svg.appendChild(svgEl("path", { d: "M " + d, fill: "none", stroke: "var(--ink-2)",
        "stroke-width": 3.25, "stroke-linejoin": "round", "stroke-linecap": "round", opacity: .9 }));
      const last = truth.values.reduce((acc, v, i) => (v == null ? acc : i), -1);
      if (last >= 0) {
        const t = svgEl("text", { x: x(last) + 8, y: y(truth.values[last]) + 4,
          class: "serieslabel", fill: "var(--ink-2)" });
        t.textContent = "Truth";
        svg.appendChild(t);
      }
    }
  }

  // Direct labels: identity must never be colour-alone.
  series.forEach((s) => {
    const last = s.values.reduce((acc, v, i) => (v == null ? acc : i), -1);
    if (last < 0) return;
    const t = svgEl("text", { x: x(last) + 8, y: y(s.values[last]) + 4, class: "serieslabel", fill: s.color });
    t.textContent = s.label;
    svg.appendChild(t);
  });

  attachHover(svg, series, truth, leads, x, variable);
  renderTable(variable, series, truth);
}

function drawLines(svg, series, leads, x, y) {
  series.forEach((s) => {
    const pts = s.values.map((v, i) => (v == null ? null : `${x(i)},${y(v)}`)).filter(Boolean);
    if (!pts.length) return;
    const a = { d: "M " + pts.join(" L "), fill: "none", stroke: s.color,
      "stroke-width": s.width, "stroke-linejoin": "round", "stroke-linecap": "round" };
    // The baseline is dashed as well as grey: it is the one series whose colour
    // is deliberately near-neutral, so shape carries its identity too.
    if (s.baseline) a["stroke-dasharray"] = "5 4";
    svg.appendChild(svgEl("path", a));
  });
}

function drawBars(svg, series, leads, x, y, lo) {
  const n = series.length || 1;
  const slot = (x(1) - x(0)) * 0.8;
  const bw = Math.max(1.5, slot / n - 2);   // 2px surface gap between adjacent bars
  series.forEach((s, si) => {
    s.values.forEach((v, i) => {
      if (v == null || !isFinite(v)) return;
      const h = Math.max(0, y(lo) - y(v));
      svg.appendChild(svgEl("rect", {
        x: x(i) - slot / 2 + si * (bw + 2), y: y(v), width: bw, height: h,
        fill: s.color, rx: Math.min(2, bw / 2),
      }));
    });
  });
}

/* ---------- hover ---------- */

function attachHover(svg, series, truth, leads, x, variable) {
  const tip = document.getElementById("tip");
  let rule = null;
  const clear = () => { tip.style.display = "none"; if (rule) { rule.remove(); rule = null; } };

  svg.onmouseleave = clear;
  svg.onmousemove = (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    let best = 0, bd = Infinity;
    leads.forEach((_, i) => { const d = Math.abs(x(i) - px); if (d < bd) { bd = d; best = i; } });

    if (!rule) {
      rule = svgEl("line", { y1: PAD.t, y2: H - PAD.b, class: "axisline", "stroke-dasharray": "3 3" });
      svg.appendChild(rule);
    }
    rule.setAttribute("x1", x(best));
    rule.setAttribute("x2", x(best));

    const rows = [];
    if (truth.values && truth.values[best] != null) {
      rows.push(`<tr><td><span class="sw" style="background:var(--ink-2)"></span>Truth</td>
                 <td class="v">${fmt(truth.values[best])}</td></tr>`);
    }
    series.forEach((s) => {
      const v = s.values[best];
      if (v == null) return;
      const err = truth.values && truth.values[best] != null ? v - truth.values[best] : null;
      rows.push(`<tr><td><span class="sw" style="background:${s.color}"></span>${s.label}</td>
                 <td class="v">${fmt(v)}${err == null ? "" :
                   ` <span style="color:var(--muted)">(${err > 0 ? "+" : ""}${fmt(err)})</span>`}</td></tr>`);
    });

    const meta = varMeta(variable);
    tip.innerHTML = `<h4>+${leads[best]} h · ${(S.doc.valid_times[best] || "").slice(0, 16).replace("T", " ")}Z` +
      `${meta.unit ? " · " + meta.unit : ""}</h4><table>${rows.join("")}</table>`;
    tip.style.display = "block";
    const w = tip.offsetWidth;
    tip.style.left = Math.min(ev.clientX + 16, window.innerWidth - w - 12) + "px";
    tip.style.top = Math.min(ev.clientY + 14, window.innerHeight - tip.offsetHeight - 12) + "px";
  };
}

/* ---------- notes, provenance, table ---------- */

function explainTruth(status) {
  if (status === "truth_pending")
    return "this init is real-time, and precipitation truth (IMERG Late) is not implemented yet";
  if (status === "no_variable") return "no model here produces this variable";
  return "truth is unavailable for this variable";
}

function renderNotes(variable, truth) {
  const box = document.getElementById("notes");
  box.innerHTML = "";
  const a = absenceNotes(variable);

  if (truth.status !== "ok") {
    const n = el("div", "note");
    n.innerHTML = `<b>No truth line.</b> ${explainTruth(truth.status)[0].toUpperCase()}` +
      `${explainTruth(truth.status).slice(1)}. Forecasts are still shown — they are ` +
      `real output — but nothing on this page scores them.`;
    box.appendChild(n);
  } else if (S.doc.truth_valid_through) {
    const through = S.doc.truth_valid_through;
    const last = S.doc.valid_times[S.doc.valid_times.length - 1];
    if (through < last) {
      const n = el("div", "note");
      n.innerHTML = `<b>Truth is still arriving.</b> This init is ${S.doc.tier}; ` +
        `GFS analysis exists only through ${through.slice(0, 16).replace("T", " ")}Z, ` +
        `so later leads have no reference line yet. They fill in on subsequent runs.`;
      box.appendChild(n);
    }
  }

  const bits = [];
  if (a.noVar.length) bits.push(`<b>${a.noVar.join(", ")}</b> — no native ${isPrecip(variable) ? "precipitation" : variable} head, so there is no forecast to draw`);
  if (a.pending.length) bits.push(`<b>${a.pending.join(", ")}</b> — forecast exists, truth pending`);
  if (a.unavailable.length) bits.push(`<b>${a.unavailable.join(", ")}</b> — declared but missing from the forecast store`);
  document.getElementById("missing").innerHTML =
    bits.length ? "Not shown: " + bits.join(" · ") + "." : "";
}

function renderProvenance() {
  const d = S.doc;
  document.getElementById("prov").innerHTML =
    `<span>init <b>${d.init_time.slice(0, 16).replace("T", " ")}Z</b></span>` +
    `<span>location <b>${d.city.name}</b> · ${d.city.lat.toFixed(2)}, ${d.city.lon.toFixed(2)}</span>` +
    `<span>init source <b>${d.init_source}</b></span>` +
    `<span>truth <b>${d.truth_source}</b></span>` +
    `<span>tier <b>${d.tier.toUpperCase()}</b></span>`;
}

/* Bias and MAE against the same truth the scoreboard used. Reported at the
 * canonical lead marks rather than every lead, so the table stays readable. */
function renderTable(variable, series, truth) {
  const t = document.getElementById("errtable");
  const intro = document.getElementById("errintro");
  t.innerHTML = "";
  if (truth.status !== "ok" || !truth.values) {
    intro.textContent = "No truth for this variable, so there is nothing to score against — " +
      explainTruth(truth.status) + ".";
    return;
  }
  const marks = S.doc.leads.filter((lh) => lh % 24 === 0);
  intro.textContent = `Signed bias (forecast − truth) and mean absolute error at ${S.doc.city.name}, ` +
    `for this single init. One location and one init is an anecdote, not a ranking — ` +
    `the leaderboard is where skill is actually measured.`;

  const head = el("tr");
  head.append(el("th", null, "Model"), el("th", null, "MAE (all leads)"));
  marks.forEach((lh) => head.appendChild(el("th", null, `bias +${lh}h`)));
  t.appendChild(el("thead")).appendChild(head);

  const body = el("tbody");
  series.forEach((s) => {
    const errs = s.values.map((v, i) => (v == null || truth.values[i] == null ? null : v - truth.values[i]));
    const fin = errs.filter((e) => e != null);
    const mae = fin.length ? fin.reduce((a, b) => a + Math.abs(b), 0) / fin.length : null;
    const tr = el("tr", s.baseline ? "baseline" : null);
    tr.append(el("td", null, s.label), el("td", "num", fmt(mae)));
    marks.forEach((lh) => {
      const i = S.doc.leads.indexOf(lh);
      const e = i < 0 ? null : errs[i];
      tr.appendChild(el("td", "num", e == null ? "–" : (e > 0 ? "+" : "") + fmt(e)));
    });
    body.appendChild(tr);
  });
  t.appendChild(body);
}

boot().catch((e) => {
  document.querySelector("main").innerHTML =
    `<p class="empty">Could not load the explorer data: ${e.message}<br>` +
    `Run <code>python -m scoreboard.export</code> to generate <code>docs/data/</code>.</p>`;
  console.error(e);
});
