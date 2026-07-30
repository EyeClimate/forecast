# Explorer Pages — AI Prompt Playbook

Implementation steps for [PLAN_EXPLORER.md](PLAN_EXPLORER.md). Same convention as
[ai_prompt.md](ai_prompt.md): feed one prompt at a time, in order, each
self-contained and assuming a fresh session, each ending in acceptance criteria
you can check before moving on.

Repo-wide ground rules now live in [CLAUDE.md](CLAUDE.md) and load automatically —
no need to paste them.

**Why the steps are finer than the plan's phases:** Phase 2 is a week of work and
splits at a natural seam — the `manifest.json` schema. Freeze that contract in E2
and the Python export side (E4) and the JS render side (E5) become independently
verifiable.

**Do not run these in parallel by phase.** The chain is sequentially dependent:
E1 emits config + `models.json` + `site.css` that everything downstream consumes;
E4 extends E2's exporter; E5 decodes E4's output. The only genuine parallel pair
is **E4 ‖ E5**, and only after E2 has frozen the format.

**Human prerequisites (the AI can't do these):**

- [ ] Before E2: decide the ~30-city list (name, lat, lon) — this blocks the
      points exporter. Open question §9.1.
- [ ] Before E4: pick the §7 repo-growth option (orphan branch / off-repo /
      stay small).
- [ ] Before Phase 2b steps: decide whether IMERG Late truth lands first
      (§9.4). Without it, precip error maps are historic-only.

---

## E1 — Phase 0 groundwork

**Why first:** every later step reads the config block and the shared stylesheet
this creates. It also touches `docs/index.html`, the file the daily cron rewrites,
so it must be verified in isolation before anything builds on it.

> In `forecast_scoreboard/`, do the Phase 0 groundwork described in
> `PLAN_EXPLORER.md` §6 Phase 0. Read `PLAN_EXPLORER.md` and `config.yaml`
> first.
>
> 1. Add a `display:` block to `config.yaml`: for each model in the existing
>    `models:` registry, a `label` and `color` (plus `color_dark` if the page
>    needs one); a `cities:` list of `{name, lat, lon}`; `map_variables`,
>    `map_leads`, and `map_resolution_deg` keys for later steps. Take the
>    current colours from the hand-written `const MODELS` array in
>    `docs/index.html` (around line 568) so the published page does not change
>    appearance.
> 2. Create `scoreboard/export.py` with a function that writes
>    `docs/data/models.json` from that block.
> 3. Make `docs/index.html` use `models.json` as the single source of truth for
>    model labels/colours — either fetch it at runtime or have `publish.py`
>    inject it, whichever preserves the existing rendering. Then remove the
>    now-impossible drift warning at `scoreboard/publish.py:257-266`.
> 4. Create `docs/lib/css/site.css` by extracting the shared shell styles from
>    `docs/index.html`'s `<style>` block (lines 2–425) — header, nav, cards,
>    dark theme — and have `index.html` link it. Leave page-specific rules
>    inline. The page must render identically.
> 5. Add a `python -m scoreboard.publish` entry point (an `if __name__ ==
>    "__main__"` that loads `config.yaml` and calls `publish`). There isn't one
>    today — publish only runs via `run_range` — and every later step needs to
>    re-run it to verify.
> 6. `.gitignore`: keep `docs/assets/` ignored; make sure `docs/lib/` and
>    `docs/data/` are tracked.
>
> Acceptance: (a) `conda run -n earth2 python -m scoreboard.publish` exits 0;
> (b) `git diff --stat docs/index.html` shows only the intended structural
> edits and the page still renders with all models in their original colours;
> (c) `docs/data/models.json` contains exactly the models in `config.yaml`'s
> registry; (d) `git status` shows `docs/lib/` and `docs/data/` as tracked, not
> ignored.

## E2 — Freeze the data contract: `manifest.json` + points export

**Why before any UI:** this schema is the interface both pages and both later
steps code against. Changing it after E3/E5 exist means editing three places.
No front-end work in this step.

> In `forecast_scoreboard/`, extend `scoreboard/export.py` to write the
> explorer's data payloads. Read `PLAN_EXPLORER.md` §3, §4 and §5a first. No
> HTML or JS in this step — data only.
>
> 1. `docs/data/manifest.json`: the available init times, models per init,
>    variables, lead hours, and (reserved, empty for now) a `fields` section
>    for per-field value scales. Include the init source / truth source / tier
>    for each init, read from the same place `publish.py` gets them, so pages
>    can label the regime.
> 2. `docs/data/points/<init>/<city>.json` for each city in `config.yaml`'s
>    `display.cities`: for every model and every scored variable, the value at
>    each lead time at that lat/lon, plus a `truth` series.
>
>    **Read these from a durable `data/points.parquet`, not from the forecast
>    zarrs** — see PLAN_EXPLORER.md §4a. Point values are only obtainable while a
>    zarr exists, and `sweep.py` purges zarrs after scoring, so an exporter that
>    reads zarrs silently emits historic inits with 1 model of 10. Add the
>    bilinear city sampling to `scoreboard/verify.py`, where the zarr is already
>    open and the leads are already being iterated, and append to
>    `data/points.parquet` under the same file lock discipline as
>    `metrics.parquet`. Already-purged inits are accepted as unrecoverable.
>
>    **Real-time inits must be topped up, never written once.**
>    `verify.py:141-155` re-scores real-time inits incrementally as GFS analysis
>    lands; the points store and the JSON export must follow the same discipline.
>    An export that skips an init because its output directory already exists
>    will freeze truth at whatever fraction of leads was available the first
>    time — 7 of 20 in the rejected first attempt. Re-emit when the truth window
>    has advanced.
> 3. Precipitation must be included, and the two distinct absence cases must
>    be representable in the JSON and distinguishable by a consumer:
>    **`no_variable`** (the model has no `precip_variable` in `config.yaml` —
>    `fengwu`, `sfno`, `fcn3`) versus **`truth_pending`** (real-time init, so
>    `verify.py:146-149` has no IMERG truth). Do not collapse them to `null`.
> 4. Write a JSON Schema for both file types under `docs/data/schema/`, and a
>    `scripts/check_export.py` that validates every emitted file against it.
>    The gate must validate **every city**, not just the first — a partially
>    failed export produces per-city divergence (one city missing a model, or
>    disagreeing with the rest on the truth window), and E3 iterates all 32.
>    It must also **fail loudly rather than skip** when `data/metrics.parquet` is
>    absent, so a manifest that lies about tier or init source cannot pass on a
>    fresh clone.
>
> Acceptance: (a) `conda run -n earth2 python scripts/check_export.py` exits 0;
> (b) one city's JSON, pretty-printed, shows all models present with a `truth`
> series — and for a historic init that means **all ten models**, not just those
> whose zarr happens to survive; (c) `fengwu`/`sfno`/`fcn3` carry `no_variable`
> for precip, and a real-time init carries `truth_pending`; (d) total size of
> `docs/data/points/` is under ~1 MB per init; (e) a real-time init re-exported
> after its truth window advances gains the newly available truth leads instead
> of being skipped; (f) corrupting a single non-first city fails the gate.

## E3 — `compare.html`: point time series

**Why here:** the cheapest data in the project (§4) and the most legible
comparison. It also exercises `manifest.json` before the heavier field pipeline
depends on it.

> In `forecast_scoreboard/`, build `docs/compare.html` per `PLAN_EXPLORER.md`
> §6 Phase 1. Read that section, plus `docs/data/schema/`, first. Fetch data
> from `docs/data/`; do not inline it.
>
> - City selector (dropdown plus a small clickable locator map), variable tabs,
>   and a multi-model chart with `truth` drawn as a heavier reference line.
> - Model toggles coloured from `docs/data/models.json` — never hardcode
>   colours.
> - A table beneath the chart: per-model bias and MAE vs. truth at each lead.
> - Precipitation renders as accumulation bars, not lines. Where a series is
>   absent, label which case it is: "no native precip" for `no_variable`,
>   "truth pending" for `truth_pending`. These are different facts and the page
>   must not imply a model failed when it simply has no precip head.
> - Reuse the charting approach already in `docs/index.html`; do not add a
>   second charting library. Link `docs/lib/css/site.css` so the shell matches.
> - Add a nav link between `index.html` and `compare.html`.
>
> Before writing any chart code, load the `dataviz` skill.
>
> Acceptance: (a) page loads with no console errors; (b) every model in
> `models.json` appears and toggles; (c) switching city and variable updates
> chart and table together; (d) a model with no precip shows the "no native
> precip" label rather than an empty or zero series; (e) the bias/MAE numbers
> for one model at one lead match a hand-computed value from the same JSON.

## E4 — `fields.py`: `t2m` field export

**Why `t2m` only:** §5a. All ten models have it, truth exists in both regimes,
and uint8 quantization is benign — so the pipeline gets proven before
precipitation's log-scale and categorical-error complications arrive.

> In `forecast_scoreboard/`, add `scoreboard/fields.py` to export gridded
> fields for the map page. Read `PLAN_EXPLORER.md` §4 and §5a first.
> **`t2m` only in this step** — do not add other variables.
>
> For each init, model, and each lead in `config.yaml`'s `display.map_leads`:
> regrid `t2m` to `display.map_resolution_deg` (start at 1.0° for fast
> iteration), and write two single-channel PNGs under
> `docs/data/fields/<init>/<model>/t2m/`: `f<lead>.png` (the forecast) and
> `e<lead>.png` (`model − truth`, computed in float before quantization —
> never by differencing quantized fields, see §4).
>
> - Quantize each to uint8 and record its `[min, max]` scale in
>   `manifest.json`'s `fields` section. Error fields get a **symmetric**
>   scale centred on zero.
> - Reserve one uint8 value for missing/no-truth and mark it in the manifest so
>   the renderer can draw it transparent.
> - Prune inits beyond `retention_days`, matching `sweep.py`'s existing
>   convention.
> - Add `scripts/check_fields.py` that, for every emitted PNG, decodes it back
>   to float using the manifest scale and asserts the max absolute error
>   against the source array is **≤ half the quantization step**. This is the
>   test that makes the encoding trustworthy.
>
> Acceptance: (a) `conda run -n earth2 python scripts/check_fields.py` exits 0;
> (b) PNG count equals models × leads × 2; (c) every emitted field has a scale
> in `manifest.json`, and error scales are symmetric about zero; (d) report the
> actual total bytes written per init and compare it to §4's ~6 MB estimate for
> single-variable 0.5°.

## E5 — `map.html`: field rendering

**Why this is the step to watch.** Every earlier step has a mechanically
checkable output. Here a lat-flip or hemisphere-swap bug renders a completely
convincing picture of the wrong thing, and no acceptance criterion catches it
unless you build one deliberately — hence the synthetic fields below, which are
not optional.

> In `forecast_scoreboard/`, build `docs/map.html` plus `docs/lib/js/`
> (`field.js`, `colormap.js`, `map.js`) per `PLAN_EXPLORER.md` §5 and §6
> Phase 2. Read those sections first.
>
> **Start with the orientation test, before any real data.** Have
> `scripts/synthetic_fields.py` emit two fields in the E4 format — one whose
> value equals latitude, one whose value equals longitude — and render those
> first. The latitude field must appear as a smooth pole-to-pole gradient in
> the correct direction; the longitude field must run the correct way and not
> tear at the antimeridian. Do not proceed to real fields until both are
> visibly right, and keep the script committed as a regression check.
>
> Then:
> - Leaflet with `L.CRS.EPSG4326`, vendored locally into
>   `docs/lib/vendor/` — no CDN. **No basemap tile layer**: draw a Natural
>   Earth 110 m coastline GeoJSON over the field instead (§5).
> - `field.js`: decode PNG → `Float32Array` via the manifest scale, cache by
>   URL, and expose a bilinear sample for hover readout.
> - `colormap.js`: a sequential ramp for absolute fields and a
>   **zero-centred diverging** ramp for error fields. The neutral point must sit
>   exactly at zero — an error map whose neutral colour drifts off zero
>   misrepresents the sign of a bias.
> - Controls for model, lead, and a forecast/error toggle; a colorbar that
>   relabels itself from the manifest scale; the reserved missing value drawn
>   transparent.
> - Display the init's tier/truth-source from `manifest.json`, the way
>   `index.html` shows its chips — the meaning of "error" changes with init age
>   (§9.2).
>
> Acceptance: (a) both synthetic fields render with correct orientation;
> (b) hover readout at a chosen lat/lon matches the value obtained by sampling
> the source zarr at that point, to within the quantization step; (c) the error
> view's colorbar is symmetric with neutral exactly at zero; (d) switching lead
> is instant after first load (cache working); (e) no console errors.

## E6 — Side-by-side comparison and lead slider

**Why last in Phase 2:** it is the payoff view for a comparison site, but it
depends on a single-pane renderer that is already known-correct.

> In `forecast_scoreboard/`, extend `docs/map.html`: add a two-pane
> side-by-side mode with **linked pan/zoom**, each pane independently
> selectable by model, and a lead-time slider that prefetches adjacent leads.
> Reuse the `field.js` cache. Per `PLAN_EXPLORER.md` §6 Phase 2.
>
> Acceptance: (a) panning either pane moves both, with no drift after repeated
> pan/zoom; (b) both panes show the same lead and update together from the
> slider; (c) slider scrubbing does not refetch already-cached leads (verify in
> the network panel); (d) no console errors.

---

## After E6

Phase 2b (precipitation as a raster: log quantization, dry mask, categorical
hit/miss/false-alarm against the 1/5/10 mm thresholds) and Phase 3 (animation,
wind particles, 16-bit fields) get their own steps once E1–E6 are verified and
the §9.4 IMERG decision is made. Do not write them speculatively — E4's actual
measured sizes and E5's rendering choices should inform them.

Also fold the new pages into the daily cron: export runs after publish, and the
§7 repo-growth choice must be in place before field PNGs start accumulating.
