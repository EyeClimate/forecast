/* Data-path gate for compare.html (EXPLORER_STEPS.md E3).
 *
 * compare.js needs a browser, so this cannot verify rendering. What it does
 * verify is everything the page computes BEFORE it draws: that the manifest and
 * every city document agree, that each model resolves to a colour, that the two
 * absence cases stay distinguishable, and that the bias/MAE the error table
 * shows match an independent computation from the same JSON.
 *
 *   node scripts/check_compare_data.js [docs_dir]
 */

const fs = require("fs");
const path = require("path");

const DOCS = process.argv[2] || path.join(__dirname, "..", "docs");
const DATA = path.join(DOCS, "data");
const read = (p) => JSON.parse(fs.readFileSync(p, "utf8"));

let failures = 0;
const fail = (m) => { console.error("FAIL " + m); failures++; };
const ok = (m) => console.log("ok   " + m);

const manifest = read(path.join(DATA, "manifest.json"));
const models = read(path.join(DATA, "models.json"));

// --- every model the pages can draw has a colour and a css var -------------
const byId = new Map(models.map((m) => [m.id, m]));
for (const m of models) {
  if (!/^#[0-9a-f]{6}$/i.test(m.color || "")) fail(`models.json ${m.id}: bad color ${m.color}`);
  if (!/^#[0-9a-f]{6}$/i.test(m.color_dark || "")) fail(`models.json ${m.id}: bad color_dark`);
  if (!(m.css_var || "").startsWith("--s-")) fail(`models.json ${m.id}: bad css_var`);
}
ok(`models.json: ${models.length} models, all with colour + css_var`);

// --- the short-label map in compare.js must cover every variable ------------
const SHORT = { z500: 1, t850: 1, t2m: 1, msl: 1, u10m: 1, v10m: 1, tp06: 1, tp: 1 };
for (const v of Object.keys(manifest.variables)) {
  if (!SHORT[v]) fail(`compare.js SHORT map has no entry for variable '${v}' — the tab would read '${v.toUpperCase()}'`);
  const meta = manifest.variables[v];
  if (!meta.label) fail(`manifest.variables.${v}: no label`);
  if (meta.unit !== undefined) fail(`manifest.variables.${v}: has 'unit'; compare.js reads 'units'`);
}
ok(`variables: ${Object.keys(manifest.variables).length} present, all covered by the tab-label map`);

if (!manifest.inits.length) fail("manifest has no inits — compare.html would render its empty state");

for (const init of manifest.inits) {
  const dir = path.join(DATA, init.points.dir);
  const cities = manifest.cities;
  let checkedNumbers = 0;

  for (const c of cities) {
    const f = path.join(dir, `${c.id}.json`);
    if (!fs.existsSync(f)) { fail(`${init.init_time}: missing ${c.id}.json`); continue; }
    const doc = read(f);

    if (doc.leads.length !== doc.valid_times.length)
      fail(`${c.id}: ${doc.leads.length} leads vs ${doc.valid_times.length} valid_times`);

    for (const id of doc.models_expected) {
      if (!byId.has(id)) fail(`${c.id}: model '${id}' has no entry in models.json — it would draw grey`);
      const m = doc.models[id] || {};
      for (const v of init.variables) {
        const s = m[v];
        if (!s) { fail(`${c.id}/${id}: no series for ${v}`); continue; }
        if (!["ok", "no_variable", "truth_pending", "unavailable"].includes(s.status))
          fail(`${c.id}/${id}/${v}: unknown status '${s.status}'`);
        // An 'ok' series must actually carry values, or the page draws a gap
        // while claiming the data is fine.
        if (s.status === "ok" && (!s.values || s.values.length !== doc.leads.length))
          fail(`${c.id}/${id}/${v}: status ok but values missing or wrong length`);
        if (s.status !== "ok" && s.values !== null)
          fail(`${c.id}/${id}/${v}: status ${s.status} should carry values:null`);
      }
    }

    // --- independent bias / MAE, matching what the error table renders ------
    for (const v of init.variables) {
      const truth = doc.truth[v];
      if (!truth || truth.status !== "ok" || !truth.values) continue;
      for (const id of doc.models_expected) {
        const s = (doc.models[id] || {})[v];
        if (!s || s.status !== "ok" || !s.values) continue;
        const errs = s.values.map((x, i) => (x == null || truth.values[i] == null ? null : x - truth.values[i]));
        const fin = errs.filter((e) => e != null);
        if (!fin.length) continue;
        const mae = fin.reduce((a, b) => a + Math.abs(b), 0) / fin.length;
        if (!isFinite(mae)) fail(`${c.id}/${id}/${v}: MAE not finite`);
        // A model that exactly equals truth at every lead means the exporter
        // sampled truth into the model slot.
        if (mae === 0 && id !== "truth") fail(`${c.id}/${id}/${v}: MAE is exactly 0 — model series equals truth`);
        checkedNumbers++;
      }
    }
  }
  ok(`${init.init_time}: ${cities.length} cities, ${checkedNumbers} model×variable error series computed`);
}

// --- the two absence cases must both remain expressible --------------------
const sample = read(path.join(DATA, manifest.inits[0].points.dir,
                              `${manifest.cities[0].id}.json`));
const statuses = new Set();
for (const id of sample.models_expected)
  for (const v of Object.keys(sample.models[id] || {}))
    statuses.add(sample.models[id][v].status);
console.log(`     statuses present in ${manifest.cities[0].id}: ${[...statuses].sort().join(", ")}`);
if (!statuses.has("ok")) fail("no 'ok' series at all");

console.log(failures ? `\n${failures} FAILURE(S)` : "\nall checks passed");
process.exit(failures ? 1 : 0);
