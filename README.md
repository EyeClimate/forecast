# Forecast Scoreboard

Daily verification scoreboard for AI weather models (see `PLAN.md` for the
full design). Phase 1 (historic-range MVP, all-ERA5) is implemented: any
registered model can be initialized from ERA5, run out to 5 days, scored
against ERA5 truth, and published to a static site.

## Quick start

```bash
cd forecast_scoreboard
conda run -n earth2 python -m scoreboard.run_range \
    --start 2023-01-15T00 --end 2023-01-21T00 --models atlas fuxi aurora
# then open docs/index.html
```

- `--stride-hours 24` (default) picks one init per day at 00z.
- `--nsteps 20` (default) = 5-day forecasts at 6 h steps.
- `--models` accepts any key under `models:` in `config.yaml`.
- Re-runs are idempotent: inits already scored in the metrics table are
  skipped entirely (`--rescore` recomputes metrics, re-forecasting first if
  the zarr was purged).
- `--purge-after-verify` deletes each init's forecast zarr once its rows are
  confirmed in `metrics.parquet` (see Retention below).
- `--no-publish` skips site regeneration.

## Pipeline steps

For each init time in the range, `scoreboard/run_range.py` drives four steps:

1. **Resolve data sources** (`scoreboard/sources.py`). The pipeline never
   hard-codes a source — it asks this module, which picks a regime from the
   init time's age. Inits older than `historic_cutoff_days` (120 — ARCO's
   ERA5 publication lag is ~3 months, not the ~6 days originally assumed;
   e2s currently rejects ARCO requests past 2026-04-30) use the historic
   regime: **ERA5 via ARCO** (Google's zarr mirror, 1940→recent, no CDS
   credentials) for both initial conditions and truth. The `ARCOInit`
   wrapper also synthesizes two variables ARCO lacks:
   - `r{level}` (relative humidity, a FuXi input) from `q`/`t` via the Magnus
     saturation formula;
   - `tp06` (6 h precip accumulation, FuXi input + verification truth) by
     summing the six hourly ERA5 `tp` accumulations ending at each time.

   Younger inits are the real-time regime: **GFS analysis** via `GFSInit`
   (`init_source='gfs'`). GFS maps `r{level}` natively; what it can't serve
   is synthesized:
   - `tp`/`tp06` (6 h precip accumulation ending at t): GFS analysis files
     (f000) carry no accumulated precip, so it comes from the previous
     cycle's own short forecast (`GFS_FX` init t−6h at lead +6 h, i.e. APCP
     over (t−6h, t]). Model input only — never verification truth.
   - AIFS extras: `skt` ← surface TMP and `tcw` ← PWAT (both corr ≥0.99 vs
     ERA5); `stl1/stl2` ← TSOIL (land corr 0.98, ocean NaNs filled with
     `skt`); `swvl1/swvl2` are *not* mappable (GFS SOILW is a different
     quantity — total moisture incl. ice; corr 0.1–0.2) and are served from
     ERA5 one year earlier (t−364 d, same hour) — seasonally correct and
     inside ARCO's publication lag, at the cost of the current year's soil
     moisture anomaly.
   Variables GFS can neither serve nor synthesize (Atlas's `sst`) raise up
   front, so Atlas can't yet run in this regime. Real-time **truth** is GFS analysis at each valid time
   (`truth_source='gfs_analysis'`, `tier=provisional`; a later ERA5 re-score
   upgrades to final). Verification is *incremental*: each run scores only
   the leads whose valid time has truth (GFS analysis lands ~4 h after cycle
   time; we use a 6 h cutoff) and that aren't already in the metrics table,
   so the daily job progressively fills a 5-day forecast in over 5 days.
   An init is "done" (skipped, purgeable) only once its final lead is scored.
   Real-time **precipitation** truth (IMERG Late) is not implemented yet —
   precip metrics are skipped for real-time inits; the synthesized GFS-
   background `tp06` is a model input only, never verification truth.

2. **Forecast** (`scoreboard/forecast.py`). Load the model once per range,
   run `earth2studio.run.deterministic` for 20 × 6 h steps, and store only
   the `scored_variables` from `config.yaml` to
   `data/forecasts/<model>/<init>.zarr` (plus a `.init_source` sidecar
   recording where the init came from). A failed run deletes its partial
   zarr so retries start clean.

3. **Verify** (`scoreboard/verify.py`). Fetch truth at every valid time,
   align it to the forecast grid, and score:
   - **State variables** (`t2m u10m v10m z500 t850 msl`): latitude-weighted
     RMSE and bias, plus ACC (anomaly correlation vs the WeatherBench2
     climatology; skipped gracefully if the climatology is unavailable).
   - **Precipitation** (mm per 6 h): RMSE and bias, plus CSI (critical
     success index) and FSS (fractions skill score, 9-cell ≈ 250 km
     neighborhood) at 1 / 5 / 10 mm thresholds.
   - Every metric is computed over four regions: global, NH extratropics
     (20–90°N), tropics (20°S–20°N), SH extratropics (20–90°S).

   Rows append to `data/metrics.parquet` under an exclusive file lock (safe
   for parallel per-GPU runs), deduplicated on
   `init_time | model | lead_hours | variable | region | metric`:

   ```
   init_time | model | lead_hours | variable | region | metric | value
             | init_source | truth_source | tier
   ```

   The same pass also samples `config.yaml`'s `display.cities` bilinearly out
   of the forecast — every city, every lead, every scored variable — into a
   second store, `data/points.parquet`, under its own lock and dedup:

   ```
   init_time | model | lead_hours | variable | city | lat | lon | value
   ```

   It lives here, and not in the exporter, because this is the only moment
   both facts hold: the zarr is open, and it still exists. `metrics.parquet`
   holds region aggregates, so a city time series cannot be reconstructed from
   it, and the metrics written above are exactly what licenses retention to
   delete the forecast. Sampling therefore runs **before** anything else in
   `verify_forecast` can return, and a failure there is fatal — a re-run
   recovers the metrics, nothing recovers the points. Values are stored as the
   zarr holds them (precip in metres); unit presentation belongs to the
   exporter. Inits already verified before this existed can still be captured
   while their zarr survives:

   ```bash
   conda run -n earth2 python -m scoreboard.verify --backfill-points
   ```

4. **Publish** (`scoreboard/publish.py`). Writes two pages into `docs/`
   (named `docs/` because GitHub Pages serves only a branch root or `/docs`):

   - **`docs/index.html`** — the designed single-file page and the one
     visitors land on. It embeds the metrics as inline JSON, so it is fully
     self-contained (~5 MB, ~1.9 MB gzipped). Values are stored **per init
     time**, not pre-averaged:

     ```
     data[model][region][variable][metric][init_idx] = [value per lead]
     ```

     which is what lets the page re-average over any **init window** the
     visitor picks — month-preset chips (auto-derived from the init times
     present) plus two date inputs for an arbitrary start/end. Every chart,
     leaderboard, and table recomputes from the selected window, and the
     mean at each lead uses only the inits that actually have that lead, so
     a part-scored real-time init contributes its finished leads without
     skewing the rest.

     Each publish rewrites, in place, only these spots — the `const DATA`
     blob, the `const MODELS` array and the `<style id="model-colors">` block
     (both generated from `config.yaml`'s `display.models`, so the page's
     colours cannot drift from the registry), the header "inits …" span, the
     `init source` / `truth` / `tier` chips, and the footer "scores generated"
     timestamp (parquet mtime) — leaving all other bytes untouched. A model
     scored in the parquet but absent from `display.models` still triggers a
     loud warning: it lands in `DATA` with no colour or label to draw it with.
     Committing this file and pushing is what updates the live site (see
     Hosting below).

     Page weight grows with init count (~130 KB per init). Past a few
     hundred inits, switch to monthly aggregates or on-demand JSON files
     rather than one inlined blob.
   - **`docs/charts.html`** — the plain matplotlib page: scorecard table at
     24/72/120 h, RMSE and ACC lead-time curves, precip CSI/FSS panels, and
     a forecast-vs-truth precip map for the latest scored init. Regenerated
     each run and gitignored along with `docs/assets/`.

## Explorer data export

`scoreboard/export.py` writes what the explorer pages fetch, under `docs/data/`
(committed, retention-bounded — unlike `docs/assets/`, which is generated and
ignored):

```bash
conda run -n earth2 python -m scoreboard.export [--init YYYY-MM-DDTHH] [--force]
```

- **`models.json`** — `config.yaml`'s `display.models`, the one source of model
  labels and colours for all three pages.
- **`points/<init>/<city>.json`** — every model's forecast and the verification
  truth at one lat/lon, at every lead. The forecast half is reshaped from
  `data/points.parquet`; the truth half is fetched here, because ERA5 and GFS
  analysis stay available long after the forecast zarr does not.
- **`manifest.json`** — what exists and how to read it: inits, models, leads,
  variables, cities, per-init regime/tier/provenance, a `fields` section for the
  gridded export, and a `map` section carrying `config.yaml`'s `display.map`
  (zoom limits and the basemap list) so no page hardcodes them.

Two behaviours are load-bearing rather than incidental:

- **Real-time inits are topped up, not written once.** An init exported the day
  it runs has GFS-analysis truth for only its first few leads; the rest lands
  over the following five days. Every export re-visits the inits already on
  disk and fills in the truth that has since arrived, mirroring verification's
  own incremental discipline. Use `--now YYYY-MM-DDTHH` to rehearse that
  without waiting a day.
- **An init that cannot be made whole is omitted, loudly.** Point values only
  ever existed while the forecast zarr did, so an init verified before sampling
  was added — and swept since — can never be completed. The export prints which
  models are missing and leaves the init out of `manifest.json` altogether,
  rather than publishing a comparison that silently drops models.

`docs/data/schema/` holds a JSON Schema for both file types, and the gate
validates every emitted file against it:

```bash
conda run -n earth2 python scripts/check_export.py
```

It checks the schema, the semantics the schema cannot express (series lengths,
valid times, **every** city agreeing with the manifest, truth as complete as the
clock allows), the bilinear sampler against analytic latitude/longitude fields,
and — so that a gate which accepted everything could not pass for a working one
— itself, by feeding deliberately broken documents and corrupted trees through
and requiring each to be rejected. It fails rather than skips when
`data/metrics.parquet` is absent: the tier and source labels are exactly what
the exporter guesses without it.

## Gridded field export

`scoreboard/fields.py` writes the global maps `docs/map.html` will draw, as
quantized single-channel PNGs. **`t2m` only** — all ten models carry it, truth
exists in both regimes, and uint8 quantization is benign, so the chain gets
proven before precipitation's log scale and categorical error arrive
(PLAN_EXPLORER.md §5a).

```bash
conda run -n earth2 python -m scoreboard.fields [--init YYYY-MM-DDTHH] [--latest]
```

For each init, model, and lead in `config.yaml`'s `display.map_leads`, `t2m` is
regridded to `display.map_resolution_deg` and written to
`docs/data/fields/<init>/<model>/t2m/` as `f<lead>.png` (forecast) and
`e<lead>.png` (`model − truth`). `docs/data/fields/<init>/index.json` is the
durable record of every scale and grid parameter; `manifest.json`'s `fields`
section is aggregated from those sidecars by `export.write_manifest`, so the two
exporters can run in either order and a points-only export cannot drop the
field metadata.

Four encoding decisions are load-bearing:

- **The error field is differenced in float, at native 0.25°, before either side
  is quantized.** uint8 across a 220–320 K range is ~0.39 K per level, so a page
  that subtracted two quantized forecast fields would carry ~0.55 K of pure
  encoding noise — the same order as the 24 h error it was trying to show
  (PLAN_EXPLORER.md §4). Precomputing it here lets the error field spend all 254
  levels on a ±10 K range instead of ±50 K; measured steps are 0.05–0.20 K
  against 0.35–0.50 K for the forecast fields.
- **Error scales are exactly symmetric about zero** — built by negating one
  magnitude, so `scale[0] == -scale[1]` is an equality, not an approximation. A
  diverging ramp whose neutral colour drifts off zero misrepresents the sign of
  a bias.
- **Byte 0 is reserved for missing** and published in the manifest as
  `encoding.missing`; data occupies 1–255, giving 254 quantization intervals.
  The renderer draws the reserved value transparent, so "no data" is never
  painted as the bottom of the colour ramp.
- **One scale per (variable, lead, kind), shared across models.** Per-model
  scales would quantize each field a little more finely and would render the
  same temperature as two different colours in a side-by-side comparison. For a
  comparison site that trade is the wrong way round.

A lead whose truth has not landed yet gets **no** error PNG and is marked
`truth_pending` in the manifest, rather than a file containing nothing but the
missing byte. And unlike the points export, **fields are re-exported, not topped
up**: the fix for an init whose truth window has advanced is another full run,
which is only possible while its forecast zarr survives `retention_days`. The
gate insists on it.

```bash
conda run -n earth2 python scripts/check_fields.py
```

Its core is the decode gate: **every emitted PNG is decoded back to float
through the scale `manifest.json` publishes, and the maximum absolute difference
from the source array must be at most half the quantization step.** A scale that
is stale, mistyped or attached to the wrong lead produces a perfectly plausible
weather map of the wrong numbers, and inverting the encoding is the only way to
notice. Source arrays are regenerated by calling `scoreboard.fields` itself, so
the gate tests the pipeline rather than a second copy of it; an init whose zarr
has been swept is reported as unverifiable, and the run **fails if no init could
be verified at all**, so the gate cannot decay into a no-op as retention rolls
forward. Around that sit the schema, the structural invariants above, a
staleness check, and a self-test that corrupts a synthetic tree twenty-three
ways and requires every one to be caught.

Measured for one init, `t2m` only, five leads, forecast + error:

| Resolution | Per PNG | Per model (all leads, both kinds) | 9 models |
|---|---|---|---|
| 1.0° (181×360) | 20 KiB forecast, 28 KiB error | 0.23 MiB | 2.0 MiB |
| 0.5° (361×720) | 59–71 KiB forecast, 86–100 KiB error | 0.71–0.84 MiB | 6.4 MiB |

PLAN_EXPLORER.md §4 estimated ~6 MB per init for single-variable 0.5° output
over ~6 models; the same six models measure 4.3–5.0 MiB, so the plan's figure is
right and slightly conservative. `config.yaml` stays at **1.0°** for now, per
E4's "start at 1.0° for fast iteration" — raising it to 0.5° is a one-line
change once `docs/map.html` exists, and needs the §7 repo-growth decision first.

## Map rendering

`docs/map.html` draws those PNGs. Everything below is presentation — no stored
data changes — but each piece is the kind of choice that is invisible when wrong,
so the reasoning is recorded here and in the source.

**Per-variable colour ramps.** `config.yaml`'s `display.map_palettes` assigns
each variable a ramp by name; `export.py` writes the assignment into
`manifest.json`, and `docs/lib/js/colormap.js` holds the ramps. Unlisted
variables get viridis. The bar for beating viridis is a genuine pre-existing
reader convention: temperature is the clear case, because a reader decodes blue
and red directly without consulting the colourbar, and viridis asks them to
suppress a mapping they cannot suppress. **Error views ignore the assignment
entirely** and always use the diverging ramp pinned at zero — sign is the only
thing an error map exists to show. A ramp name that `colormap.js` does not define
raises at export time rather than silently falling back.

**Display units.** `docs/lib/js/units.js` converts at render time only; storage
stays SI everywhere. K→°C/°F, Pa→hPa/inHg, m²/s²→dam geopotential height,
m/s→mph. The one hazard it exists to contain: **an error field is a difference,
so offset units must convert as a delta** — a 2 K error is a 2 °C error, not
−271 °C. Absolute and delta conversions are separate functions per unit and the
caller must say which it wants. `npm run check:units` covers that specifically,
including sign preservation and zero-stays-zero across every unit and system.

**The floating chrome is glass, and its opacity is a measured number rather than
a taste.** Every panel, bar, popup, note and the zoom control share one material:
a 24 px backdrop blur, `saturate(150%)`, a bright inset rim along the top edge
for the specular highlight that gives the slab thickness, and a top-down sheen
fading by 45% so the light has a direction.

The tuning is where a map like this differs from a photograph. These panels float
over a temperature ramp that spans violet to dark red, so their effective
background is not a colour anyone picked — it is whatever the field happens to be
behind them. At the 58% fill that looks best over a photo, the panel's
micro-labels measured **1.9:1** in dark mode and the nav links **3.4:1**. Three
things brought it back: the fill went to 78%, `saturate()` came down from 180%
(it boosts the field's chroma *before* compositing, and the text has to survive
what comes out), and `--muted` is overridden *within glass containers only* — the
site token is correct on the solid surfaces of `index.html` and `compare.html`
and is left alone there. Colourbar ticks moved from `--muted` to `--ink-2` while
this was being measured: they are the values the whole field is read against and
were the faintest text on the page.

`check:map` section 13 measures it the way it has to be measured — screenshot the
composited page, take the modal colour inside each panel as its effective
background, and compute WCAG contrast against the tokens as resolved *inside* the
glass. Both themes, three viewports over the hot end of the ramp, floor of 4.5:1
for every token. Currently worst is 4.8:1. Thinning the glass fails the gate.
`prefers-reduced-transparency` and `prefers-contrast: more` drop the whole
material to an opaque panel and switch the blur off.

**The control panel is two tiers, and the split is by how often a control is
touched rather than by what it does.** Model, View and init time are the question
being asked and change constantly, so they are always visible. Basemap, colour
scale, units, layout and field opacity are preferences set once and left, so they
fold into a collapsed `Display` disclosure; provenance folds into a second one.
Flattening the two into one list is what made the panel 725 px tall — 81% of a
1440×900 window and already scrolling on a 1280×800 laptop. Split, it is 348 px
and scrolls nowhere.

Two rules keep the fold honest. A collapsed section **reports the state it is
hiding** on its own summary (`°F · stretched · streets · 2 panes`), listing only
what is away from its default — otherwise the panel could not answer "am I in °C
or °F" without being opened. And a control group with nothing to choose between
**hides itself**: the Variable tabs stay hidden while the export carries one
variable, rather than showing a tab group with a single button.

`<details>` rather than a scripted accordion, because open/closed is state the
browser already owns, it is keyboard- and screen-reader-correct for free, and
`querySelector` still finds the controls inside a closed one — which is what lets
the render gate drive them without opening it. Which sections are open persists
in `localStorage`; both default to closed.

**Narrow windows.** Below 820 px the panel becomes a drawer bounded by the nav
above and the bottom bar below, opened from the nav's `Layers` button. Three
things had to be true for that to work, and each had been false: the breakpoint
is a `matchMedia` **subscription**, not a check made once at boot (narrowing an
open window used to leave a full-width slab over the map); the nav wraps and
drops its brand below 520 px, so it cannot run off the edge taking the dismiss
button with it (which made the panel impossible to close on a phone); and the
drawer is bounded by `--navh`/`--barh` rather than capped at 46% of the viewport,
which used to clip it through the middle of a control. `check:map` asserts all
three at 820, 600 and 380 px.

**Colour scale modes.** Global (default) uses the scale `manifest.json`
publishes, which is comparable across leads, models and panes. *Stretch to view*
recomputes from the 2nd–98th percentile of the data inside the viewport on every
`moveend`. Percentiles, not min/max: one Antarctic cell at 197 K otherwise sets
the floor for the whole world. Two constraints hold it honest — in side-by-side
both panes share one scale derived from both, and stretched error scales are
re-symmetrised about zero so the ramp's neutral grey still means zero. Stretching
an *anchored* palette costs the colours their absolute meaning, which is the very
thing that justified choosing that palette, so the page says so on screen when
that happens.

**Geography.** `scripts/vendor_geography.py` fetches Natural Earth 50 m
coastlines and country borders, simplifies them, and writes
`docs/lib/vendor/*.json` (committed — at global zooms the page makes no external
requests). Raw 50 m is 2.3 MB; Douglas–Peucker at 0.02° with 2-decimal rounding
brings it to ~205 KB gzipped. The tolerance was tied to a `maxZoom` of 6, where
one pixel is ~0.02°; at the current `maxZoom` of 10 it is ~15 px of visible
stair-stepping, which is one of the reasons the vendored layers stand down as
soon as a basemap is on. A 30° graticule is generated in JS, and city names from
`manifest.json`'s `cities` appear at zoom ≥ 3 with a paint-order halo, since they
sit directly on a saturated field.

**Street basemap.** Past zoom `display.map.basemap_zoom` (6) the map switches on
a basemap and switches it off again on the way out, until the reader picks one
explicitly — an explicit choice is remembered and ends the automatic behaviour.
At world scale a 1° field wants a clean outline, not street cartography; at
street scale a coastline has stopped answering "where exactly is this".

The entries live in `config.yaml`'s `display.map.basemaps` and reach the page
through `manifest.json`; adding one adds a button, and `export.py` refuses an
entry with no attribution. Three ship: **Roads** (transparent OSM roads and place
names drawn *over* a full-strength field — the default, and the reason it is
listed first), **Streets** and **Terrain** (opaque, drawn *under* the field,
which is dimmed to `field_opacity` with a slider to override).

**These are WMS endpoints, not `{z}/{x}/{y}` tile services, and that is forced.**
The map's CRS is `EPSG:4326`, while every CDN raster basemap — CARTO, OSM's own,
Stamen — is pre-rendered in Web Mercator, and a projected raster cannot be
reprojected in the browser. Using one would mean moving the map to `EPSG:3857`,
which truncates at ±85.05° and would cut Antarctica and most of the Arctic off a
*global weather* map. A WMS renders per request in whichever CRS the request
names, so it costs a slower tile and keeps the poles. `check:map` asserts
`SRS=EPSG:4326` on the generated request for exactly this reason: a Mercator
source would look nearly right near the equator and drift further north, which is
the kind of wrong that ships.

Transparent layers go in a custom Leaflet pane at z-index 450 — above the field's
overlay pane (400), below the marker pane (600) — and pass pointer events
through. Opaque ones stay in the default tile pane, which is already below the
field. While any basemap is on, the vendored coastline and borders come off and
our city *names* are hidden (the basemap prints its own); the *dots* stay,
because they mean something different — see below.

**City popups.** The 32 dots are exactly the points the pipeline sampled out of
every forecast zarr, so a dot means "there is a per-model series here", not
"there is a city here" — which is why they survive a basemap that labels far more
places. Clicking one reads `points/<init>/<city>.json`, the same document
`compare.html` draws: every model at that lead, ranked by absolute error, against
the truth it was scored on, with a deep link through to `compare.html?city=…`.
Clicking anywhere else reads the *raster* instead — one nearest-cell value per
pane. The two disagree slightly by construction (bilinear from the zarr versus
nearest-cell from a 1° PNG) and the popups say which they are. Open popups
re-render on every redraw, so scrubbing the lead slider moves the numbers rather
than leaving a stale reading pinned to the map.

**Longitude wraps; latitude does not.** Every layer — field overlay, coastlines,
borders, graticule, city labels — is drawn three times, at −360°, 0° and +360°,
and `worldCopyJump` folds the centre back into the middle copy when it crosses
the antimeridian. Because the copies are identical the fold is invisible, so
panning east or west is continuous and the Pacific has no seam. `maxBounds` still
walls latitude (its actual job — no blank page above the pole) but its longitude
range is set absurdly wide, since Leaflet has no latitude-only bounds.

Three copies suffice, and the margin is worth knowing: at minZoom the viewport is
at most 288° wide (the world is 2:1 and the viewport is wider, so latitude binds
first), and `worldCopyJump` keeps the centre within ±180°, so the visible span
never leaves −324°..324° — inside the −540°..540° the copies cover. **A viewport
wider than 2:1 would make longitude bind instead and break that argument.**

Geography is drawn with one `L.polyline` per layer per copy, *not* `L.geoJSON`:
Leaflet renders a multi-polyline as a single `<path>`, so the 1186 coastline and
389 border features collapse from 9558 SVG elements to 18. Nothing styles by
attribute, so nothing is lost. The primary field copy carries
`.fieldcopy-primary` so `check_map_render.js` can find the one anchored at
−180..180.

**Raster quality.** Fields are rolled to −180, bicubically upsampled 4× in
*value* space, then coloured through a 256-entry LUT — in that order. Rolling
first puts the upsampler's longitude wrap at the dateline; interpolating values
rather than colours keeps every pixel a colour the ramp actually contains; the
LUT removes a million per-pixel array allocations. Upsampling adds no information
1° data lacks, but replaces the browser's bilinear facets with a smooth surface.
Redraw is ~70 ms at 1440×724. Missing cells stay transparent rather than being
interpolated from their neighbours.

```bash
npm run check:map      # orientation, alignment, layout, stretch scaling,
                       # city popups, basemap wiring
npm run check:units    # absolute vs delta conversion
```

`check:map` ignores the basemap hosts in its network assertions and never
requires a tile to arrive — it checks the request the page *builds*, so the gate
passes offline. What it does assert on the network is the opposite direction:
**no off-origin request is made at all until a basemap is asked for.**

## Daily automation

`scoreboard/daily.sh` runs from cron (installed for user `bowen`, 04:30
local, logging to `data/logs/daily.log`):

```
30 4 * * * .../scoreboard/daily.sh >> .../data/logs/daily.log 2>&1
```

Each run forecasts yesterday's 00z init for every real-time-capable model
(all but atlas) and re-runs the trailing 8 days — verification is
incremental, so this scores exactly the leads whose GFS-analysis truth
arrived since the last run; a 5-day forecast completes over ~6 daily runs.
It then sweeps old zarrs and, if `docs/index.html` changed, commits and
pushes it so GitHub Pages serves fresh scores. The separate published copy of
the page is not updated by cron — republish it manually when desired.

## Retention

Forecast zarrs are ~584 MB per model per init, so two mechanisms reclaim
them once their scores are safely in `data/metrics.parquet`:

- `run_range.py --purge-after-verify` deletes each init's zarr (and its
  `.init_source` sidecar — its content already lives in the metrics rows'
  `init_source` column) as soon as verification succeeds and the rows are
  confirmed present.
- `scoreboard/sweep.py`, meant for cron, deletes scored zarrs whose files
  are older than `retention_days` (config, default 30). Age is the zarr's
  mtime, not its init time, so a fresh historic backfill survives the sweep:

  ```bash
  conda run -n earth2 python -m scoreboard.sweep [--dry-run] [--days N]
  ```

A forecast with no metrics rows is never deleted, whatever its age: the
metrics append is all-or-nothing under the file lock, so missing rows mean a
failed or unfinished verify and the zarr is still needed. Purged inits are
not re-forecast — the pipeline's skip logic keys off the metrics table, not
the zarr's existence.

One consequence is permanent and worth stating plainly. Scores survive
retention; **point values do not, unless they were sampled before the sweep.**
Every init verified before `data/points.parquet` existed is unrecoverable for
the explorer's purposes — 38 of the 39 scored inits at the time city sampling
landed, including the historic January 2023 window, whose zarrs were purged
with only `atlas`'s surviving. Those inits keep their place on the leaderboard
and are simply absent from `manifest.json`. Point history accumulates from now
on.

## Models

All models are registered in `config.yaml`; `scoreboard/forecast.py:load_model`
maps names to Earth2Studio classes. All run at 0.25° global resolution with
6 h steps.

Models whose dependencies conflict with the main `earth2` env declare a
`conda_env:` in `config.yaml`; `run_range` shells their forecast step out via
`conda run -n <env> python -m scoreboard.forecast --model X --init T`, while
verification (zarr-only, no model deps) stays in the orchestrator's env.

| Model | Origin | Precip | Notes |
|---|---|---|---|
| `atlas` | NVIDIA Atlas | native `tp` | Predicts 6 h-accumulated precip directly (scored as `tp`, verified against ERA5 `tp06`). Checkpoint is ~17 GB. |
| `fuxi` | Fudan University FuXi | native `tp06` | ERA5-trained cascade model; outputs `tp06` as a prognostic variable, so precip verification is apples-to-apples with ERA5. Needs ONNX extras and the synthesized `r{level}` inputs. |
| `aurora` | Microsoft Aurora | chained diagnostics → `tp06` | Foundation model with no precip/sp/tcwv outputs. `build_precip_chain` nests three `DiagnosticWrapper`s: `DerivedSurfacePressure` (from z-levels + t) → `DerivedTCWV` (from q-levels + sp) → `PrecipitationAFNOv2` (tp06 from the surface state). Each wrapper is itself a prognostic, so the chain resolves the input dependencies step by step. A custom concat hook works around the default output-coords handler silently dropping prognostic variables when dim orders differ. |
| `persistence` | — | copies init `tp` | "Tomorrow equals today" baseline. No skill and no GPU; used to smoke-test the pipeline and as a skill floor. |
| `fengwu` | Shanghai AI Lab FengWu | none | ONNX; 69 vars, two-time-step input like FuXi. State variables only. Local patch in the `earth2` env: `torch.cuda.synchronize()` around the ORT call in `fengwu.py:_forward` — ORT's CUDA stream is unordered w.r.t. torch's, which intermittently fed half-written buffers back into the rollout (huge cold biases at random leads). |
| `pangu6` | Huawei Pangu-Weather | chained diagnostics → `tp06` | ONNX; interleaved 24 h + 6 h models. Output set is identical to Aurora's, so the same `build_precip_chain` (sp → tcwv → `PrecipitationAFNOv2`) gives it precip scores. |
| `sfno` | NVIDIA SFNO | none | Spherical Fourier neural operator, 73 vars. State variables only. Peaks ~47 GB VRAM — needs a 48 GB card to itself. |
| `fcn3` | NVIDIA FourCastNet 3 | none | Probabilistic-capable; run as a deterministic single member. State variables only. Badged for 80 GB cards: its decoder DISCO conv materializes a ~20 GiB contraction buffer, so `load_model` patches `DiscreteContinuousConvS2.forward` to accumulate the weight einsum over the kernel dim (numerically identical, K× smaller transients) — with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` it fits 48 GB. Needs makani + torch-harmonics built from source (CUDA disco kernels). |
| `graphcast_oper` | DeepMind GraphCast (operational) | native `tp06` | JAX-based; runs in the `e2s-graphcast` conda env (jax pinned to the CUDA-12 plugin — the `[graphcast]` extra's cuda13 jax needs a newer driver; XLA preallocation off so torch and JAX share the card; persistent XLA compile cache). Fine-tuned on HRES, 13 levels, two-time-step input. Local patch in the env: e2s 0.15's `xr.Dataset(inputs)` copies are `ds.copy()` (xarray ≥ 2026 forbids the former). |
| `aifs` | ECMWF AIFS | native `tp06` | anemoi + flash-attn (compiled from source for cu126) in the `e2s-aifs` conda env. Needs `tcw`/`swvl1-2`/`stl1-2` inputs that e2s's ARCO lexicon doesn't map — `ARCOInit.EXTRA_VOCAB` registers them at runtime (the ARCO store itself has them). |

## Results so far

Historic backfill over **38 init times** (2023-01-01 → 01-31 daily 00z, plus
the summer week 2023-07-01 → 07-07): ERA5 (ARCO) init, ERA5 truth, global
region, lat-weighted, tier `final`. All models below have the full 38 inits
× 20 leads **except atlas**, whose full range is still pending — its rows are
the single init 2023-01-15 and are not directly comparable to the multi-init
means. Values are means over scored inits at 24 / 72 / 120 h.

**z500 RMSE (m²/s²)** — headline synoptic skill:

| model | +24h | +72h | +120h |
|---|---|---|---|
| fengwu | 40.0 | 123.2 | 264.3 |
| aifs | 43.5 | 125.9 | 266.0 |
| fuxi | 45.6 | 133.6 | 281.6 |
| pangu6 | 46.3 | 138.6 | 302.7 |
| graphcast_oper | 46.8 | 134.5 | 291.9 |
| aurora | 47.5 | 150.8 | 308.6 |
| atlas (1 init) | 50.4 | 138.2 | 266.1 |
| fcn3 | 58.1 | 172.5 | 355.6 |
| sfno | 64.2 | 204.8 | 400.4 |
| persistence | 576.3 | 913.1 | 1036.1 |

(fengwu's first backfill produced garbage — RMSE ~6× the persistence floor —
until the ORT/torch CUDA-stream race in `fengwu.py:_forward` was patched, see
the model table above; it was then re-run and now leads z500.)

**t2m RMSE (K)**:

| model | +24h | +72h | +120h |
|---|---|---|---|
| fengwu | 0.73 | 1.05 | 1.46 |
| aurora | 0.73 | 1.25 | 1.78 |
| pangu6 | 0.77 | 1.14 | 1.66 |
| aifs | 0.78 | 1.08 | 1.49 |
| graphcast_oper | 0.92 | 1.28 | 1.72 |
| sfno | 0.95 | 1.51 | 2.16 |
| atlas (1 init) | 0.96 | 1.33 | 1.84 |
| fuxi | 1.01 | 1.33 | 1.75 |
| fcn3 | 1.08 | 1.48 | 2.01 |
| persistence | 2.20 | 3.18 | 3.57 |

**z500 ACC** at 24 / 72 / 120 h: every AI model sits at 0.997–0.999 /
0.968–0.988 / 0.873–0.944 (fengwu and aifs on top, sfno at the bottom) —
all above the 0.9 "useful forecast" convention through day 5 except sfno
(0.873 at 120 h); persistence: 0.75 / 0.37 / 0.19.

**Precipitation** (vs ERA5 tp06, global, CSI ≥1 mm at 24/72/120 h): the
native-precip models lead — aifs 0.68 / 0.53 / 0.42, fuxi 0.67 / 0.51 /
0.39, graphcast_oper 0.64 / 0.50 / 0.38 — while the AFNO-diagnostic-chain
models (aurora 0.35 / 0.29 / 0.23, pangu6 0.32 / 0.29 / 0.24) score roughly
half that, a chain penalty worth remembering when comparing. Persistence:
0.07 / 0.05 / 0.05. (fengwu, sfno, fcn3 have no precip path.)

Multi-init takeaways: **fengwu (post-fix) and aifs top the state variables**,
with aifs also leading precip; fuxi is the strongest of the older generation
on z500; aurora wins short-lead t2m but its single-init z500 edge from the
first sample did not survive averaging. The 2023-vintage MSE-trained models
(sfno, fcn3) trail the field. Single-init numbers were flattering: 24 h
multi-init means run ~15–20 % higher than the original 2023-01-15 sample.

## Model & data cache

All Earth2Studio downloads (model checkpoints, ARCO/WB2 data cache) live in
`/home/bowen/projects/fundation/checkpoints/` — set via `EARTH2STUDIO_CACHE`
in `~/.bashrc`, with a symlink at `~/.cache/earth2studio` for processes that
don't inherit the env var.

## Layout

```
config.yaml              # model registry, verification config, regime cutoff,
                         #   display block (colours, cities, map settings)
scoreboard/
  run_range.py           # entrypoint: forecast → verify → publish over a range
  sources.py             # regime-aware data source resolution + derived r/tp06
  forecast.py            # model loading (incl. Aurora precip chain), zarr output
  verify.py              # metrics + city point sampling, locked parquet appends
  sweep.py               # retention: delete scored forecast zarrs (cron)
  publish.py             # static site generation
  export.py              # docs/data/: models.json, manifest.json, points/**
  fields.py              # docs/data/fields/: regrid -> quantize -> PNG (t2m)
scripts/
  check_export.py        # gate for everything export.py writes
  check_fields.py        # gate for the PNGs: decode back, assert <= half a step
  check_map_render.js    # gate for map.html: orientation, alignment, stretch
  check_units.mjs        # gate for units.js: absolute vs delta conversion
  vendor_geography.py    # fetch + simplify Natural Earth into docs/lib/vendor/
data/                    # gitignored
  forecasts/<model>/<init>.zarr
  metrics.parquet         # region-aggregated scores
  points.parquet          # city samples — perishable, captured during verify
docs/                    # GitHub Pages root (branch: main, folder: /docs)
  index.html             # designed leaderboard page — the published site
  charts.html            # plain matplotlib page (gitignored)
  assets/                # PNGs for charts.html (gitignored)
  lib/                   # AUTHORED front-end code, committed
    css/site.css         #   shell shared by all pages
    js/colormap.js       #   per-variable ramps + diverging error ramp + LUT
    js/units.js          #   display units; absolute and delta conversions
    js/field.js          #   PNG decode, sampling, viewport percentiles, upsample
    vendor/              #   leaflet + Natural Earth coastlines/borders (50 m)
  data/                  # GENERATED payloads the explorer pages fetch, committed
    models.json  manifest.json  points/<init>/<city>.json  schema/
```

## Hosting

Nothing is fetched from a third party — metrics are inlined in
`docs/index.html`, and the stylesheet, `docs/lib/`, and `docs/data/` are all
served from the same directory — so hosting is just static file serving:

- **GitHub Pages** — repo Settings → Pages → *Deploy from a branch*,
  `main` + `/docs`. Every `git push` updates the live site; no CI needed.
- **Locally** — `python3 -m http.server 8899 --bind 127.0.0.1` from `docs/`,
  then open <http://127.0.0.1:8899/>.

See `PLAN.md` for the full design, including the Phase 2 real-time regime.
