# Explorer Pages — Build Plan

**Goal:** add two pages to the existing scoreboard site — a **point comparison**
page (pick a location, see every model's forecast vs. truth as time series) and a
**field map** page (global maps of forecasts and their errors). Together these are
the capability that `meteo.weatherex.ai` and the `weatherex.ai` Leaflet map
provide, driven by our own `metrics.parquet` + forecast zarrs.

Sibling of [PLAN.md](PLAN.md) (pipeline) and [MODEL_METHODS.md](MODEL_METHODS.md)
(scoring definitions).

> **Correction (E2 review).** This plan originally claimed nothing here changes
> the forecast or verification stages. That was wrong, and §4's "point data is
> nearly free" subsection explains why: point values exist only while a forecast
> zarr does, and `sweep.py` purges those after verification. Capturing them
> requires a change to `verify.py`. See §4a.

---

## 1. The constraint that drives every decision

**GitHub Pages is static.** There is no API to query, so every byte the browser
reads must be a file committed to the repo. That is why weatherex.ai can serve
`api.weatherex.ai/v1/forecast` and hourly-refreshed raster tiles from a GCS
bucket, and we cannot — not without a backend.

Three consequences, and they shape the whole design:

1. **Gridded fields must be pre-rendered and budgeted.** A single global 0.25°
   field is 1440×721 = 1.04 M cells. Shipping all models × all variables × all
   20 lead times is tens of MB per init. We subsample deliberately (§4).
2. **The current "inline everything" pattern stops here.** `docs/index.html` is
   already 5.0 MB because `publish.py` regex-injects `const DATA = {...}` into it
   (`scoreboard/publish.py:246`). A map payload cannot go the same way. New pages
   `fetch()` their data from separate files.
3. **Daily binary churn accumulates in git history forever.** Committing ~12 MB
   of PNGs every day adds ~4 GB/year to the repo even if only the latest init is
   ever present in the working tree. Mitigation in §7.

---

## 2. What exists today

| Piece | Path | Status |
|---|---|---|
| Designed leaderboard | `docs/index.html` | Committed; 1149 lines, 5.0 MB (inlined `DATA`); `<style>` L2–425, `<script>` L562, `const MODELS` L568 |
| Plain matplotlib page | `docs/charts.html` | Generated, **gitignored** |
| Matplotlib PNGs | `docs/assets/*.png` | Generated, **gitignored** |
| Site generator | `scoreboard/publish.py` | 379 lines; `refresh_scoreboard()` does regex surgery on `index.html` |
| Scores | `data/metrics.parquet` | gitignored (`data/`) |
| Forecast fields | `data/forecasts/<model>/<init>.zarr` | gitignored |

Two frictions worth fixing before adding pages, because a third page makes both
worse:

- **`const MODELS` (colors + labels) is hand-curated inside `index.html`.**
  `publish.py:257-266` already has to warn when `metrics.parquet` contains a
  model the page's array doesn't. With three pages that becomes three
  hand-maintained copies of the same list. Move it to `config.yaml`, emit it once
  as JSON.
- **`.gitignore` ignores `docs/assets/` wholesale.** Hand-written JS/CSS placed
  there would be silently untracked. Put authored code in a new `docs/lib/` and
  leave `docs/assets/` as the generated-artifact dumping ground. Cleaner than
  fighting the ignore rule with negations.

---

## 3. Folder layout

```
forecast_scoreboard/
├── config.yaml                     # + `display:` block (colors, cities, map settings)
├── scoreboard/
│   ├── forecast.py  verify.py  sources.py  sweep.py  run_range.py
│   ├── publish.py                  # unchanged role: index.html + charts.html
│   ├── export.py            NEW    # writes docs/data/** (points + manifest)
│   └── fields.py            NEW    # regrid → quantize → PNG encode helpers
└── docs/
    ├── index.html                  # existing leaderboard, untouched
    ├── charts.html                 # generated (gitignored)
    ├── compare.html         NEW    # point time series — Phase 1
    ├── map.html             NEW    # global field maps — Phase 2
    ├── lib/                 NEW    # AUTHORED, committed
    │   ├── css/site.css            #   shared shell: header, nav, cards, dark theme
    │   ├── js/
    │   │   ├── models.js           #   loads models.json; color/label lookup
    │   │   ├── colormap.js         #   scientific colormaps + diverging error ramp
    │   │   ├── field.js            #   PNG → Float32Array decode, bilinear sample
    │   │   ├── compare.js          #   Phase 1 page logic
    │   │   └── map.js              #   Phase 2 page logic
    │   └── vendor/
    │       ├── leaflet.js  leaflet.css      # vendored, no CDN
    │       └── coastlines-110m.json         # Natural Earth, ~200 KB
    ├── assets/                     # GENERATED matplotlib PNGs (gitignored)
    └── data/                NEW    # GENERATED, committed, retention-bounded
        ├── manifest.json           #   inits available, models, vars, leads, scales
        ├── models.json             #   id → {label, color, colorDark}
        ├── points/
        │   └── <init>/<city>.json  #   all models × vars × leads + truth, one city
        └── fields/
            └── <init>/<model>/<var>/
                ├── f<lead>.png     #   forecast field, uint8 + scale in manifest
                └── e<lead>.png     #   error field (model − truth), precomputed
```

**Why `docs/data/` and not inlining:** a `fetch()`-ed payload is cached by the
browser, diffable in review, and lets the map page load one variable at a time
instead of paying for all of them upfront.

---

## 4. Data budget — the numbers

Global 0.25° is 1440×721. Smooth meteorological fields compress well as
single-channel PNG; measured ballpark:

| Resolution | Cells | ≈ PNG size |
|---|---|---|
| 0.25° | 1,038,240 | 250–400 KB |
| 0.5° | 259,920 | 70–120 KB |
| 1.0° | 65,160 | 20–40 KB |

Combinations: ~6 models with real skill × 4 map-worthy variables
(`t2m`, `tp/tp06`, `z500`, `msl`) × leads.

| Choice | Fields/init | Size/init |
|---|---|---|
| All 20 leads @ 0.5° | 480 | ~48 MB ❌ |
| All 20 leads @ 1.0° | 480 | ~14 MB |
| **5 leads (24/48/72/96/120 h) @ 0.5°** | **120 forecast + 120 error** | **~24 MB** |
| 5 leads @ 1.0° | 240 | ~7 MB |

**Recommendation: 0.5°, five canonical leads, forecast + error, latest init
only.** ~24 MB in the working tree. Start at 1.0° during Phase 2 development to
keep iteration fast, then raise once the page works.

Note this table is the *eventual* four-variable budget. Phase 2 ships `t2m`
alone (§5a), so its actual footprint is a quarter of it — ~6 MB/init at 0.5°,
which makes the §7 repo-growth decision less urgent until `z500`/`msl`/precip
land.

### Quantization: ship error fields, don't difference in the browser

The tempting design is uint8 fields + client-side subtraction, giving free
model-A-minus-model-B maps. It does not survive contact with the numbers: uint8
over a 220–320 K `t2m` range is 0.39 K per level, so differencing two quantized
fields carries ~0.55 K of quantization noise — **the same order as the actual
24 h forecast error we are trying to show.** The error signal would be buried in
its own encoding artifacts.

So: **precompute `model − truth` in Python**, where the float data already
exists, and ship it as its own uint8 file with its own symmetric scale. Each
error field then spends all 256 levels on a ±5 K range instead of ±50 K.

Model-vs-model differencing stays available client-side but must be labelled
approximate, or deferred to Phase 3 with 16-bit encoding (pack 16 bits across a
PNG's R+G channels) for the variables that need it.

### Precipitation needs a different encoding entirely

Everything above assumes a roughly Gaussian field. Precipitation is not one: it
is zero across most of the map with a heavy tail. Three consequences that rule
out reusing the `t2m` path (see §5a for the ordering this implies):

- **Linear uint8 destroys the signal.** 0–100 mm/6 h across 256 levels is
  0.4 mm/level, which erases the 0.1–1 mm drizzle band that the 1 mm CSI
  threshold in `config.yaml` depends on. Precip must be **log-quantized**.
- **A linear colormap renders a near-blank map**, for the same reason.
- **Pointwise `model − truth` is the wrong error notion.** That is precisely why
  `verify.py` scores precip with `_csi` (categorical) and `_fss` (neighborhood)
  rather than plain RMSE. A precip error map should render **hit / miss / false
  alarm** against the 1/5/10 mm thresholds, not a diverging ramp.

### Point data is nearly free

30 cities × 6 models × 7 variables × 20 leads ≈ 25 K floats. Split per city that
is ~10 KB of JSON each, ~300 KB total, **all 20 leads at full precision**. This
is why Phase 1 comes first: it is the cheapest data in the project and the most
directly useful — a scoreboard's job is comparison, and a per-location time
series is comparison at its most legible.

### 4a. Point data is cheap but *perishable* — capture it in `verify.py`

Cheap to store, yes. But it can only be **obtained** while the forecast zarr
exists, and `sweep.py` purges zarrs once their scores are in the parquet
(`retention_days: 30`, or immediately with `--purge-after-verify`).
`metrics.parquet` holds region-aggregated scores, not point values, so a city
time series cannot be reconstructed from it.

Measured on 2026-07-30: `metrics.parquet` had **39 inits scored**, while surviving
zarrs were one historic init for `atlas` alone plus one real-time init for the
other nine models. An exporter reading zarrs therefore emits a historic init
containing **1 model of 10**, unbackfillable.

**Decision: sample the configured cities inside `verify.py`**, while it already
has the zarr open and is already looping the leads, and append to a durable
`data/points.parquet` alongside the metrics. `export.py` then reads that store
rather than the forecast zarrs, and history accumulates from now on.

Two consequences to accept:

- **The 38 already-purged inits are gone** for point purposes. Re-forecasting
  them to recover point values was considered and rejected as not worth the GPU
  time. `compare.html` will therefore be thin until inits accumulate — the
  leaderboard's own history is unaffected.
- Real-time inits must be **topped up**, not written once. `verify.py:141-155`
  already re-scores real-time inits incrementally as GFS analysis arrives; the
  points store has to follow the same discipline, and the export has to re-emit
  an init whose truth window has advanced rather than skipping it because a
  directory already exists.

---

## 5. Rendering decisions

**No basemap tile layer.** Leaflet with `L.CRS.EPSG4326`, a canvas overlay for
the field, and a Natural Earth 110 m coastline GeoJSON on top. This avoids the
OSM tile-usage policy question entirely, keeps the site self-contained and
offline-capable, and for a global scientific field a clean coastline outline
reads better than street-level cartography. Leaflet still provides pan/zoom for
free (~42 KB gzipped, vendored not CDN'd).

**Field drawing:** decode the PNG into an `ImageData`, map each uint8 through the
colormap into an `OffscreenCanvas` at native grid resolution, then let the canvas
layer scale it. One decode per field, cached by URL — switching lead times is
then instant, which is what makes a time slider feel responsive.

**Colormaps:** sequential (viridis/magma) for absolute fields, a
perceptually-balanced **diverging** ramp centred on zero for error fields. The
zero-centring is not cosmetic — an error map whose neutral point drifts off zero
misleads about the sign of a bias.

---

## 5a. Which variable ships first — `t2m`, not precipitation

Precipitation is the project's flagship (PLAN.md: "precipitation first") and the
most valuable variable to get right. It is still the wrong **first** map layer,
for one blocking reason and two hard ones.

**Blocking: there is no real-time precip truth.** `scoreboard/verify.py:146-149`
sets `precip_var = None` for every real-time init — IMERG Late is not
implemented. With `historic_cutoff_days: 120`, precip skill exists *only* for
inits older than 120 days. A live precip error or skill map has nothing to score
against today.

**Coverage is 6 of 10 models, unevenly.** Per `config.yaml`, `fengwu`, `sfno`,
and `fcn3` have no `precip_variable` at all; of the six that do, `aurora` and
`pangu6` obtain it through chained `PrecipitationAFNOv2` diagnostics rather than
natively. The debut comparison would be partial and not like-for-like.

**The encoding and error model both differ** — see §4's precip subsection.

So the field pipeline is built on **`t2m`**: all ten models have it, truth exists
in *both* regimes (GFS analysis real-time, ERA5 historic), it is near-Gaussian so
the diverging ±error ramp is honest, uint8 quantization is benign, and
`rmse`/`bias`/`acc` are already in `metrics.parquet`. That proves the export →
decode → render → colorbar chain without simultaneously solving precip.

**Precipitation still ships in Phase 1**, as a chart. A city time series has no
colormap problem, and the truth gap appears as an honestly absent line rather
than a broken map. It then returns as a first-class raster in Phase 2b.

> The real unlock for a live precip map is implementing IMERG Late truth in
> `sources.py`/`verify.py`. That is a pipeline task, not a visualization one, and
> it gates the entire precip story no matter how good the front end is.

---

## 6. Phases

### Phase 0 — Groundwork (~half a day)

Prep that both pages depend on. No user-visible change.

1. Add a `display:` block to `config.yaml`: per-model `label` + `color` +
   `color_dark`, the city list (name, lat, lon), map variables, canonical leads,
   export resolution.
2. `scoreboard/export.py`: emit `docs/data/models.json` from that block.
3. Point `index.html`'s `const MODELS` at `models.json` (or have `publish.py`
   inject it from config) so the colour list has **one** source of truth. Retire
   the `publish.py:257-266` warning — with config as the source it cannot drift.
4. Create `docs/lib/` with `site.css` extracted from `index.html`'s `<style>`
   (L2–425) so all three pages share a shell.
5. `.gitignore`: keep `docs/assets/` ignored, ensure `docs/lib/` and
   `docs/data/` are tracked.

> Step 3 touches the page the daily cron rewrites. Do it in one commit, run
> `publish` once, and confirm the leaderboard still renders before moving on —
> `refresh_scoreboard()` raises on any anchor it cannot find, so a mistake here
> fails loudly rather than silently publishing a broken page.

### Phase 1 — `compare.html`, point time series (~2 days)

The `meteo.weatherex.ai` analogue, and the better first deliverable.

- **Export:** extend `export.py` to read each init's forecast zarrs plus the
  truth field, sample at each configured city (bilinear), and write
  `docs/data/points/<init>/<city>.json` — `{model: {var: [v per lead]}}` plus a
  `truth` entry. Also write `manifest.json`.
- **Page:** city selector (dropdown + clickable mini-map), variable tabs,
  multi-model line chart with truth as a heavier reference line, model toggles
  keyed to the shared palette, and a per-model error table (bias / MAE vs. truth
  at each lead) beneath the chart.
- **Include precipitation from day one** (§5a). Render it as accumulation bars
  rather than lines, and where a model has no `precip_variable` or the init is
  real-time (no IMERG truth), omit the series and label *why* — "no native
  precip" vs. "truth pending" are different facts and the page should say which.
- **Charting:** reuse whatever `index.html` already draws with; do not introduce
  a second charting library. Load the `dataviz` skill before writing chart code.
- Extend the daily cron to call the export step after `publish`.

### Phase 2 — `map.html`, global fields, `t2m` only (~1 week)

Scope deliberately narrowed to one variable (§5a) so the machinery is proven
before the awkward variables arrive. `z500`/`msl` then cost almost nothing —
same code path, different scale.

- **Export:** `fields.py` — regrid to the target resolution, compute
  `model − truth`, derive symmetric error scales, quantize to uint8, write PNGs
  and record every scale in `manifest.json`. Prune inits beyond retention.
- **Page:** Leaflet + canvas field layer + coastlines; controls for model,
  variable, lead, and a forecast/error toggle; colorbar that relabels itself from
  the manifest scale; hover readout of the value under the cursor
  (`field.js` bilinear sample); and a **side-by-side two-pane mode with linked
  pan/zoom** — the single most useful view for a model comparison site.
- Lead-time slider with prefetch of adjacent leads.

### Phase 2b — Precipitation as a raster layer (~3 days)

Once Phase 2's chain works, precip gets its own treatment rather than being
forced through the `t2m` path:

- **Log quantization** with an explicit dry mask, so 0 mm is a distinct
  transparent value and not the bottom of a colour ramp.
- **Community-standard precip palette** (§10) — readers already know how to read
  one, and inventing a new one costs comprehension for nothing.
- **Categorical error rendering** — hit / miss / false alarm against the 1/5/10 mm
  thresholds from `config.yaml`, with a threshold selector. This is the spatial
  companion to the CSI/FSS numbers the leaderboard already reports, and it is
  what makes the map a verification tool rather than a weather picture.
- **Regime labelling is mandatory here.** Only historic inits have precip truth,
  so the error view must state that it is showing inits ≥120 days old — or be
  disabled outright until IMERG Late lands.

### Phase 3 — Optional polish (~1 week, only if wanted)

Animated lead-time playback; wind-particle layer from `u10m`/`v10m` (the
weatherex.ai signature effect, `leaflet-velocity` is the reference); 16-bit
fields enabling honest model-vs-model differencing; regional zoom presets.

---

## 7. Repo growth — decide before Phase 2 lands

24 MB/day of new binaries is ~8.7 GB of git history per year. GitHub Pages'
recommended repo ceiling is ~1 GB. Three options:

1. **Publish from a force-pushed orphan branch.** The daily job commits the site
   to a `gh-pages` branch with `--force`, discarding prior history, so binary
   churn never accumulates. Cleanest fix; requires switching the Pages source
   from `/docs` on the main branch to the `gh-pages` branch.
2. **Host `docs/data/fields/` off-repo** (GCS, Cloudflare R2) and fetch
   cross-origin — the same move weatherex.ai makes, serving its wind grids from
   GCS rather than its own origin (§10). Best long-term, needs a bucket, CORS
   config, and the site is no longer fully self-contained.
3. **Stay at 1.0° / 3 leads (~4 MB/day)** and accept ~1.4 GB/year, revisiting
   later.

**Recommendation: (1) now, (2) when fields outgrow it.** (1) costs one change to
the publish job and nothing else, and it does not foreclose (2).

---

## 8. Effort summary

| Phase | Scope | Effort |
|---|---|---|
| 0 | Config/colour/CSS groundwork | ~0.5 day |
| 1 | `compare.html` + point export (incl. precip charts) | ~2 days |
| 2 | `map.html` + field export, `t2m` only | ~1 week |
| 2b | Precip raster: log scale + categorical error | ~3 days |
| 3 | Animation, wind particles, 16-bit | ~1 week (optional) |

**Phases 0–2: roughly 1.5–2 weeks** for a genuinely useful map + comparison
explorer over the models already running (`graphcast_oper`, `aifs`, `aurora`,
`fengwu`, `pangu6`, `sfno`, `fcn3`, `atlas`, `fuxi`) — broader model coverage
than the five models weatherex.ai compares (AIGFS, GFS, IFS, AIFS, ICON).
**Add ~3 days for 2b** to bring precipitation up to first-class.

---

## 9. Open questions

1. **Cities** — which ~30 locations? A global spread for credibility, or
   weighted toward a region of interest?
2. **Error definition** — error vs. the same truth source the scoreboard uses
   per regime (ERA5 historic, GFS analysis real-time)? That means the map's
   meaning shifts with init age, so the page must display the tier chip the way
   `index.html` already does.
3. **Repo growth** — confirm the §7 choice before Phase 2 export lands.
4. **IMERG Late truth** — is implementing it (`sources.py`/`verify.py`) worth
   scheduling before or after Phase 2b? Before → precip error maps work for
   recent inits. After → 2b ships historic-only.

*Resolved:* variable ordering — `t2m` first, then `z500`/`msl` for free, precip
as its own Phase 2b (§5a).

---

## 10. Reference — what weatherex.ai actually ships

Extracted from their `LeafletMap` bundle, as a scope yardstick. Not a target
list; most of it is out of reach without their ocean and satellite feeds.

**Atmosphere:** temperature, precipitation, humidity, pressure / MSL, CAPE, cloud
cover (total + low/mid/high, with a Windy-style cloud-top-temperature ramp), solar
radiation, satellite IR + visible, lightning, and wind speed at **eight levels** —
10 m, 100 m, 800 m, 1500 m, 3000 m, 4200 m, 5600 m, 9200 m (labelled
925/850/700/600/500/300 hPa) — plus wind direction.

**Ocean (Copernicus Marine / CMEMS):** SST, salinity, ocean current, wave height,
wave period, wave direction.

**Models:** AIGFS (their own), GFS, ECMWF IFS, ECMWF AIFS, ICON.

**Architecture — and it validates §5.** They do *not* serve rendered weather
tiles. A thin endpoint,
`/public/tile/{region}/{model}/f{cycle}/{lead}/{param}/{level}?fallback=1&date=YYYYMMDD`,
returns tile **metadata** (the failure string is `tile meta fetch failed`),
cached in `sessionStorage` with an expiry; the client then fetches the grid and
rasterizes it itself. GRIB2 `gridDefinitionTemplate` metadata is carried through,
wind grids come from GCS as JSON, and `velocityScale` drives the particle
animation. Only *borders* are true server tiles
(`storage.googleapis.com/map-borders-tiles/borders`).

That metadata endpoint is exactly the role `manifest.json` plays in our static
design — good evidence the split between "what to fetch and how to scale it" and
"the grid itself" is the natural one.

---

## 11. On copying weatherex.ai

Rebuilding equivalent functionality is fine and is what this plan does. Their
page copy, layout, and branding are theirs — the visual language here should
stay our own. The one thing genuinely worth borrowing is a *convention*: their
Windy-style cloud-top-temperature colormap follows an established meteorological
palette, and matching community-standard colormaps aids readability rather than
imitating a competitor.
