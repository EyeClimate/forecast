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
     blob, the header "inits …" span, the `init source` / `truth` / `tier`
     chips, and the footer "scores generated" timestamp (parquet mtime) —
     leaving all other bytes untouched. Models present in the parquet but
     missing from the page's hand-curated `MODELS` array trigger a loud
     warning; colors are assigned manually, never invented. Committing this
     file and pushing is what updates the live site (see Hosting below).

     Page weight grows with init count (~130 KB per init). Past a few
     hundred inits, switch to monthly aggregates or on-demand JSON files
     rather than one inlined blob.
   - **`docs/charts.html`** — the plain matplotlib page: scorecard table at
     24/72/120 h, RMSE and ACC lead-time curves, precip CSI/FSS panels, and
     a forecast-vs-truth precip map for the latest scored init. Regenerated
     each run and gitignored along with `docs/assets/`.

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
config.yaml              # model registry, verification config, regime cutoff
scoreboard/
  run_range.py           # entrypoint: forecast → verify → publish over a range
  sources.py             # regime-aware data source resolution + derived r/tp06
  forecast.py            # model loading (incl. Aurora precip chain), zarr output
  verify.py              # metrics computation + locked parquet append
  sweep.py               # retention: delete scored forecast zarrs (cron)
  publish.py             # static site generation
data/                    # gitignored
  forecasts/<model>/<init>.zarr
  metrics.parquet
docs/                    # GitHub Pages root (branch: main, folder: /docs)
  index.html             # designed single-file page — the published site
  charts.html            # plain matplotlib page (gitignored)
  assets/                # PNGs for charts.html (gitignored)
```

## Hosting

`docs/index.html` is self-contained (metrics inlined, no external requests),
so hosting is just static file serving:

- **GitHub Pages** — repo Settings → Pages → *Deploy from a branch*,
  `main` + `/docs`. Every `git push` updates the live site; no CI needed.
- **Locally** — `python3 -m http.server 8899 --bind 127.0.0.1` from `docs/`,
  then open <http://127.0.0.1:8899/>.

See `PLAN.md` for the full design, including the Phase 2 real-time regime.
