# Forecast Scoreboard — Build Plan

**Goal:** a website that reports, every day, real-time verification metrics
(precipitation first, other downstream tasks later) comparing the accuracy of
multiple AI weather models. **MVP goal: one model running end-to-end.**

---

## 1. Evaluation ranges and the two data regimes

The primary input to the pipeline is an **evaluation range** — a set of init
times to forecast from and score (`--start/--end/--stride`, e.g. every 00z in
Jan 2023, or "yesterday" for the daily cron). The data sources are not chosen
globally; they are resolved **per init time** by where it falls relative to
truth availability:

| | **Historic regime** (init older than ~6 days) | **Real-time regime** (recent/today) |
|---|---|---|
| Init data | **ERA5** via ARCO — reanalysis quality, in-distribution for ERA5-trained models | **GFS analysis** — free, no creds, ~4–6 h latency. (Later: IFS from ECMWF open data) |
| Precip truth | **ERA5 `tp06`** (WB2/ARCO); optionally IMERG Final (obs, ~3.5-month lag) | **IMERG Late Run** — observational, ~14 h latency (Early: ~4 h, less accurate) |
| Other-variable truth | **ERA5** | **GFS analysis at valid time** (provisional) → **ERA5 backfill** ~5 days later (final) |
| Score tier | `final` immediately | `provisional` → upgraded to `final` later |

So yes: a historic evaluation range runs entirely on ERA5 — better data, zero
credential hassle, truth available in the same job. A **source resolver**
(`sources.py`) encapsulates this: given an init/valid time and a variable, it
returns the right data source; the rest of the pipeline never hard-codes one.
The daily real-time job is just the degenerate range "yesterday", where truth
is not yet available and verification lags (below).

The `init_source` is recorded per run and shown on the site — ERA5-init and
GFS-init scores are kept comparable but distinguishable, since ERA5-trained
models score systematically better off ERA5 initial conditions.

Consequences the real-time regime must embrace:

1. **Verification is always lagged.** A forecast made today at 00z can only be
   scored as truth arrives over the following days. Each daily job scores *all
   past forecasts whose valid times now have truth*, not today's forecast.
2. **Two-tier scores.** Metrics are `provisional` (vs GFS analysis / IMERG Late)
   and get re-scored to `final` when ERA5 / IMERG Final lands. The site shows
   provisional immediately and silently upgrades.
3. **Distribution mismatch is accepted.** ERA5-trained models (FuXi, Atlas)
   initialized from GFS analysis lose a bit of skill. This is standard practice
   (WeatherBench-style operational comparisons do the same) and it is identical
   for every model, so the *comparison* stays fair.

## 2. Architecture

```
   run_range.py --start ... --end ... --stride 24h --models fuxi
   (daily cron = run_range.py with range "yesterday")
                                  │
  ┌───────────────────────────────▼────────────────────────────────┐
  │ orchestrator — for each init time in the evaluation range:     │
  │                                                                │
  │  0. RESOLVE    sources.py picks regime per init time:          │
  │                historic → ERA5 (ARCO) init + truth             │
  │                real-time → GFS init, IMERG/GFS truth           │
  │  1. INGEST     init-condition data for the model               │
  │  2. FORECAST   for each model in registry:                     │
  │                run N-step forecast → zarr store                │
  │  3. INGEST     truth available for this range                  │
  │  4. VERIFY     score every (model, init, lead) pair whose      │
  │                valid time has truth → append metrics           │
  │  5. PUBLISH    regenerate static site from metrics store       │
  └────────────────────────────────────────────────────────────────┘

  Storage:
    data/forecasts/{model}/{init_time}.zarr     (scored vars only, retention ~30 d)
    data/truth/{source}/...                      (cache)
    data/metrics.parquet                         (append-only long table)
  Site:
    site/index.html (+ per-model pages)          (regenerated daily, static)
```

**Metrics table schema** (one row per score — the single source of truth for the site):

```
init_time | model | lead_hours | variable | region | metric | value | init_source | truth_source | tier
```

`metric`: rmse_latwt, acc, bias, fss_100km, csi_1mm/5mm/10mm (precip).
`region`: global, NH, tropics, CONUS (cheap to add via lat/lon masks).

## 3. Key decisions

- **MVP model: Atlas** (NVIDIA, 2026, `hf://nvidia/atlas-era5`) — ERA5-trained,
  0.25°, 74 output variables including `t2m`, `tp`, `sst`, 100 m winds — both
  MVP downstream tasks (temperature, precip) are native outputs, no diagnostic
  needed. Generative (stochastic interpolant): sharper precip than MSE-trained
  models, and ensembles come nearly free later. Caveats:
  - Its Earth2Studio badge says **gpu:80gb**; our RTX 6000 Ada cards are 48 GB.
    Phase 1 step 0 is an OOM smoke test — if it doesn't fit (try bf16 / fewer
    `sinterpolant_sample_steps`), **fall back to FuXi** (`tp06`, deterministic,
    modest memory) for the MVP; that's a config change, not a redesign.
  - Atlas must be rolled out via the *iterator* interface (its docstring warns
    manual step-by-step calls give wrong results — internal latent state).
    `run.deterministic` uses the iterator, so the pipeline is fine; any custom
    loop must be too.
  - It outputs `tp` (assumed 6 h accumulation per 6 h step) — verify units
    against ERA5 `tp06` in Phase 1 before trusting scores.
  - Needs the physicsnemo extras: `pip install "earth2studio[atlas]"`.
  The registry makes adding FuXi / GraphCastOperational / AIFS a config entry.
  GraphCastOperational and AIFS are *better matched* to GFS/IFS initialization
  — good Phase 3 additions for the real-time regime.
- **Storage discipline:** a full 0.25° run (~70 vars × 20 steps) is ~23 GB per
  model. Store **only scored variables** (`tp, t2m, u10m, v10m, z500, t850, msl`)
  → ~2–3 GB/day/model, with a 30-day retention sweep. Metrics parquet is KBs.
- **Website MVP = static HTML** (plotly/vega embedded, generated from parquet).
  Zero ops: serve via nginx on this box, or push to GitHub Pages. Upgrade to
  FastAPI only if interactive filtering outgrows static pages.
- **One conda env per model family if needed.** FuXi needs onnxruntime,
  GraphCast needs JAX, AIFS needs anemoi/flash-attn — these conflict. The
  orchestrator shells out per-model (`conda run -n <env> python forecast.py
  --model X`), so envs stay isolated. MVP: everything in `earth2`.
- **Idempotent jobs.** Every step keyed by (model, init_time); reruns skip
  completed work, so a crashed cron run is safely retried.
- **IMERG access:** requires free NASA Earthdata credentials (~/.netrc) and a
  downloader for GPM_3IMERGHHL. The custom source in
  `earth2studio_examples/imerg_source.py` reads the downloaded files.
- Hardware: 1 model/day is minutes on one RTX 6000 Ada for most models —
  Atlas is the exception (80 GB badge, generative sampling is slower); see the
  MVP caveat above.

## 4. Downstream tasks (what the site can score)

**Tier 1 — MVP (both native Atlas outputs, no diagnostic model needed):**
- **Temperature** `t2m`: lat-weighted RMSE, bias, ACC; regional breakdowns;
  later heat-extreme hit rates (e.g. CSI on t2m > 35 °C).
- **Precipitation** `tp`: FSS, CSI at 1/5/10 mm per 6 h, bias — vs ERA5 `tp06`
  (historic regime) / IMERG (real-time regime).

**Tier 2 — more direct outputs (just more rows in config, zero new code):**
`u10m/v10m` and `u100m/v100m` winds (wind-energy relevant), `z500` (synoptic
skill — the headline metric in most model intercomparisons), `msl`, `tcwv`,
`sst`, pressure-level `t850`/`q700`.

**Tier 3 — Earth2Studio diagnostic models bolted onto any prognostic output:**
- `DerivedWS` wind speed, `DerivedRH` relative humidity (cheap, closed-form).
- `WindgustAFNO` wind gusts; `SolarRadiationAFNO` surface solar radiation.
- `tc_tracking` tropical-cyclone tracks — verified against IBTrACS best tracks
  (position error km vs lead time; a compelling page for the site).
- `ClimateNet` atmospheric-river / TC segmentation.
- `CorrDiff` / `CBottleSR` km-scale regional downscaling (case studies, not
  daily metrics).

Every task is just new `variable`/`metric` rows in the same metrics table —
the site needs no schema change as tasks are added.

## 5. Phases

### Phase 1 — MVP: one model over a historic range (all-ERA5)  ← current target
Deliverable: `run_range.py --start 2023-01-01 --end 2023-01-07 --models atlas`
produces an HTML page with temperature + precipitation metrics for Atlas. Runs
entirely in the historic regime: **ERA5 init (ARCO), ERA5 truth (WB2)** — no
credentials, no lag handling, truth available in the same job.

0. **Atlas viability check on our 48 GB cards**: load model, run 2 steps, watch
   memory (bf16 if needed). Also sanity-check `tp` units vs ERA5 tp06 for one
   date. If OOM → set MVP model to FuXi in config and continue unchanged.
1. Scaffold repo (layout below), `config.yaml` (models, variables, thresholds,
   paths, retention).
2. `sources.py` — source resolver; historic branch only for now (the
   real-time branch raises NotImplementedError until Phase 2).
3. `forecast.py` — Atlas from resolved init source →
   `data/forecasts/atlas/{init}.zarr` (scored vars only; iterator interface).
4. `verify.py` — lat-weighted RMSE/ACC/bias for `t2m` (+ Tier 2 vars); FSS +
   CSI at 1/5/10 mm per 6 h for `tp`; append to `metrics.parquet` (tier=final).
5. `publish.py` — parquet → `site/index.html`: metric-vs-lead-time curves
   aggregated over the range, scorecard table, forecast-vs-truth precip maps.
6. Smoke test: single init date first, then the week-long range.

### Phase 2 — Real-time regime + daily automation
- Real-time branch of `sources.py`: GFS init; IMERG Late downloader +
  generalized `imerg_source.py` for precip truth; GFS analysis for state vars.
- Cron/systemd timer running `run_range.py` for "yesterday".
- Rolling verification window (score whatever truth newly arrived);
  provisional→final tier upgrades.
- Retention sweep; structured logging + a status line on the site
  ("last successful run", data gaps).

### Phase 3 — Multi-model comparison
- Model registry: add FuXi, GraphCastOperational, Pangu+PrecipitationAFNOv2
  (tests the diagnostic path), AIFS. Per-model conda envs where deps conflict.
- Site becomes comparative: models overlaid per metric, head-to-head scorecard,
  significance shading (paired differences over the trailing 30 days).

### Phase 4 — Final-tier scoring & hardening
- ERA5 backfill worker (ARCO, ~5-day lag) re-scores to `tier=final`.
- IMERG Final re-scoring (~3.5-month lag) if desired.
- More downstream tasks: cyclone tracks (e2s `tc_tracking`), wind-gust,
  solar radiation diagnostics — same table schema, new `variable` rows.
- Public hosting + domain, if the site leaves this box.

## 6. Repo layout

```
forecast_scoreboard/
├── PLAN.md                # this file
├── config.yaml            # models, variables, metrics, thresholds, paths
├── scoreboard/
│   ├── run_range.py       # entrypoint: evaluation range → forecasts+scores+site
│   ├── sources.py         # per-init-time source resolver (historic vs real-time)
│   ├── forecast.py        # run one model for one init → zarr
│   ├── truth.py           # fetch/cache truth (ERA5 / GFS analysis / IMERG)
│   ├── imerg_source.py    # (moved from earth2studio_examples, generalized)
│   ├── verify.py          # metrics → parquet
│   └── publish.py         # parquet → static site
├── data/                  # gitignored: forecasts/, truth/, metrics.parquet
└── site/                  # generated HTML
```

## 7. Open questions (non-blocking for Phase 1)

- Init cadence within a range: 00z only, or 00z+12z? (MVP: 00z, `--stride`.)
- Forecast length: 5 days (20 steps) to start; 10 days doubles storage/compute.
- Historic/real-time cutoff: ~6 days (ERA5 ARCO lag); verify actual ARCO lag
  and make it a config value.
- NASA Earthdata account for IMERG — needs to be created once (free; Phase 2).
- Where the site will ultimately be hosted (this box vs GitHub Pages vs cloud).
