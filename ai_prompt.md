# AI Prompt Playbook — Forecast Scoreboard

Step-by-step prompts to feed to an AI coding agent, one at a
time, in order. Each prompt is self-contained — it assumes a fresh session
with no memory of previous ones — and ends with acceptance criteria so you
can verify the step before moving on.

**Ground rules to paste along with any prompt if the session seems lost:**

> Work in `/home/bowen/projects/fundation/forecast_scoreboard`. Read
> `PLAN.md`, `README.md`, and `config.yaml` before writing code. Python runs
> in the conda env `earth2` (`conda run -n earth2 python -m scoreboard.run_range ...`).
> Model/data caches live in `/home/bowen/projects/fundation/checkpoints`
> (`EARTH2STUDIO_CACHE`). The metrics store is `data/metrics.parquet` with
> schema `init_time | model | lead_hours | variable | region | metric | value
> | init_source | truth_source | tier`, appended under a file lock and
> deduplicated. Do not break idempotency: re-runs must skip completed work.

**Parallel execution (use in any run/backfill prompt):** the box has 4×
RTX 6000 Ada (48 GB each), and `verify.py` appends to `metrics.parquet`
under an exclusive file lock, so **one `run_range` process per GPU is safe
and supported**. Launch each model group as its own process pinned with
`CUDA_VISIBLE_DEVICES`:

```bash
CUDA_VISIBLE_DEVICES=0 conda run -n earth2 python -m scoreboard.run_range ... --models atlas &
CUDA_VISIBLE_DEVICES=1 conda run -n earth2 python -m scoreboard.run_range ... --models fuxi &
CUDA_VISIBLE_DEVICES=2 conda run -n earth2 python -m scoreboard.run_range ... --models aurora &
CUDA_VISIBLE_DEVICES=3 conda run -n earth2 python -m scoreboard.run_range ... --models fengwu pangu6 sfno persistence &
```

Guidelines: give Atlas a GPU to itself (generative sampling, slowest);
stack several fast models on one GPU sequentially rather than giving each
its own process; `persistence` needs no GPU. Stagger the launches by a
couple of minutes so one process primes the shared ERA5/truth cache
(`EARTH2STUDIO_CACHE`) before the others hit the same chunks. Use
`--no-publish` on all but skip publish entirely until the whole range is
done, then publish once.

**Human prerequisites (do these yourself, the AI can't):**

- [ ] Before Step 4: create a free NASA Earthdata account and put the
      credentials in `~/.netrc` (needed for IMERG precipitation truth).
- [ ] Decide forecast disk budget — a month × 4 models is ~70 GB without
      Step 1's purge flag.

---

## Step 1 — Forecast retention: `--purge-after-verify`

**Why first:** each init costs ~584 MB per model; the Step 2 backfill would
write ~70 GB of zarr that has no value once scored.

> In `forecast_scoreboard/`, add forecast retention to the pipeline.
> Currently `scoreboard/run_range.py` keeps every
> `data/forecasts/<model>/<init>.zarr` forever (~584 MB per model per init).
> Add a `--purge-after-verify` CLI flag: after an init's verification
> completes successfully and its rows are confirmed present in
> `data/metrics.parquet`, delete that init's forecast zarr (keep the
> `.init_source` sidecar or fold its content into the metrics rows —
> whichever is cleaner). Never delete on a failed or partial verify. Also
> add a standalone `scoreboard/sweep.py` that deletes forecast zarrs older
> than N days (default 30, from `config.yaml`) whose scores exist in the
> parquet — this will later run from cron. Keep re-runs idempotent: a purged
> init must not be re-forecast unless `--rescore` semantics require it;
> skipping should key off the metrics table, not the zarr's existence.
> Update README.md. Acceptance: run a single already-scored init
> (2023-01-15T00) with `--purge-after-verify` and show that (a) forecasting
> is skipped or completes, (b) metrics rows are intact, (c) the zarr is gone.

## Step 2 — Backfill a real evaluation range

**Why:** everything on the board currently rests on ONE init time
(2023-01-15 00z). Rankings from one init are noise. This step is mostly
compute; expect it to run for a while — launch it in the background.

> In `forecast_scoreboard/`, run the historic backfill. First delete Aurora's
> existing rows for 2023-01-15 from `data/metrics.parquet` and its forecast
> zarr if present — they predate the Aurora precip chain and lack
> precipitation scores; it must be re-run fresh. Then run, for all four
> models (`atlas fuxi aurora persistence`):
> `conda run -n earth2 python -m scoreboard.run_range --start 2023-01-01T00
> --end 2023-01-31T00 --models atlas fuxi aurora persistence
> --purge-after-verify`, followed by the same for a summer week
> `--start 2023-07-01T00 --end 2023-07-07T00`. Parallelize across the four
> GPUs: one process per model pinned with `CUDA_VISIBLE_DEVICES` (the
> parquet append is file-locked, so parallel per-GPU runs are safe) — Atlas
> alone on one GPU, and use `--no-publish` everywhere, publishing once at
> the end. Run one init end-to-end first
> as a smoke test before launching the full range. Monitor GPU memory for
> Atlas (48 GB cards, model has an 80 GB badge — if it OOMs on some inits,
> record which and continue with the other models rather than aborting the
> range). When done, report: init count per model in the parquet, any
> failed/missing (model, init) pairs, and the new mean z500 RMSE and t2m
> RMSE at 24/72/120 h per model so I can compare against the single-init
> numbers in README.md. Update the "Results so far" section of README.md
> with the multi-init numbers.

## Step 3 — Wire the designed page into `publish.py`

**Why before Phase 2:** a daily cron is pointless while the public page
needs manual surgery after every run.

> In `forecast_scoreboard/`, automate refreshing the designed scoreboard
> page. `site/scoreboard.html` is a self-contained designed page whose data
> is an inline JSON blob assigned to `const DATA = {...};` (shape:
> `leads` array + `data[model][region][var][metric]` arrays aligned to
> leads, nulls for missing). Today it is refreshed by hand;
> `scoreboard/publish.py` only regenerates the old plain `site/index.html`.
> Extend `publish.py` so every publish also rewrites `scoreboard.html` in
> place: (1) re-export `data/metrics.parquet` to that nested JSON shape and
> replace the `const DATA = ...;` line; (2) update the footer "scores
> generated" timestamp from the parquet mtime and the header provenance
> line / stat labels (init count, date range) — find where these are
> hardcoded and replace them with values computed from the parquet;
> (3) leave all other markup, styles, and scripts byte-identical. If a model
> appears in the parquet but not in the page's `MODELS` array, warn loudly
> but do not invent a color for it — new models get colors manually (next
> free categorical slot is yellow #eda100/#c98500). Do NOT re-add the
> scorecard heatmap section — it was deliberately removed. Acceptance: run
> publish, diff `scoreboard.html` against git/backup to show only DATA,
> timestamp, and provenance changed, and confirm the page renders in a
> browser with the new init counts. Then republish the updated file to the
> existing Artifact (pass
> `url: https://eyeclimate.github.io/forecast/`
> to the Artifact tool so it updates in place, favicon 🛰️).

## Step 4 — Phase 2a: real-time initial conditions (GFS)

> In `forecast_scoreboard/`, implement the real-time init branch of the
> source resolver. Read `PLAN.md` section 1 first — it defines the two
> regimes. `scoreboard/sources.py` currently raises NotImplementedError for
> inits younger than `historic_cutoff_days` (6). Implement the real-time
> branch using Earth2Studio's GFS analysis data source for initial
> conditions, mirroring what `ARCOInit` does for the historic branch —
> including synthesizing the variables GFS may lack that the models need as
> inputs (check each registered model's input coords; the historic branch
> already synthesizes `r{level}` and `tp06`, port that logic where
> applicable). Record `init_source='gfs'` in the sidecar/metrics rows.
> Acceptance: `conda run -n earth2 python -m scoreboard.run_range --start
> <yesterday 00z> --end <yesterday 00z> --models fuxi --no-publish` produces
> a forecast zarr initialized from GFS (verification will mostly be skipped
> — truth isn't available yet; that's Step 5's job). Show the zarr exists
> and its init_source says gfs.

## Step 5 — Phase 2b: lagged verification + truth sources

> In `forecast_scoreboard/`, implement lagged verification for the real-time
> regime, per `PLAN.md` section 1. Requirements: (1) a daily job scores ALL
> past forecasts whose valid times now have truth, not the forecast made
> today — add a verification pass that scans `data/forecasts/**` for
> (model, init, lead) pairs missing from `data/metrics.parquet` and scores
> whichever now have truth; (2) truth sources for the real-time regime: GFS
> analysis at valid time for state variables (tier=`provisional`) and IMERG
> Late Run for precipitation (`~/.netrc` Earthdata credentials exist; there
> is a starting-point custom source at
> `../earth2studio_examples/imerg_source.py` to generalize into
> `scoreboard/truth.py` or similar — note IMERG rain rate needs converting
> to 6 h accumulations and regridding to the 0.25° grid); (3) tier upgrade:
> when an init crosses `historic_cutoff_days`, re-score against ERA5/ARCO
> and overwrite the provisional rows with tier=`final` (the dedup key
> ignores tier — make sure final replaces provisional rather than
> duplicating). Acceptance: for the GFS-initialized forecast from the
> previous step, provisional state-variable scores appear in the parquet;
> demonstrate the final-tier upgrade path on an init old enough to have
> ERA5 truth.

## Step 6 — Phase 2c: daily automation

> In `forecast_scoreboard/`, wire up daily automation. Create a script
> `scoreboard/daily.py` (or a shell wrapper) that runs, in order: (1)
> `run_range` for yesterday 00z across all models in `config.yaml` with
> `--purge-after-verify`; (2) the lagged-verification pass over all pending
> past forecasts; (3) the tier-upgrade pass; (4) publish (which now also
> refreshes `site/scoreboard.html`); (5) the retention sweep. Each step
> must log to a dated file under `data/logs/` and a failure in one model
> must not abort the others. Install it as a systemd timer (or cron) for
> this box running daily at a time when GFS 00z analysis and IMERG Late are
> available (~14:00 UTC is safe). Add a status line to the published page
> footer: last successful run time and any (model, init) gaps in the
> trailing 7 days. Acceptance: trigger the job manually once end-to-end,
> show the log, show `systemctl list-timers` (or crontab -l) with the entry.

## Step 7 — Statistical honesty: error bars + head-to-head

**Only after Step 2's backfill — significance over one init is decoration.**

> In `forecast_scoreboard/`, make the comparison statistical, now that
> `data/metrics.parquet` holds many init times per model. (1) In the
> lead-time curves on both `publish.py` outputs, aggregate across inits:
> line = mean, shaded band = ±1 stderr across inits. (2) Add a head-to-head
> section: for each model pair and headline metric (z500 RMSE, t2m RMSE,
> precip CSI ≥1 mm at 24/72/120 h), compute the paired difference per init
> and a paired t-test (or sign test) across inits; render as a compact
> matrix with the leader named per cell and cells greyed out when not
> significant at p<0.05. Keep the visual language of `site/scoreboard.html`
> (same fonts/colors/spacing; run the dataviz skill before designing any
> new chart). Only use inits where BOTH models have scores — the pairing
> must be on common inits. Acceptance: screenshots of the new sections, and
> a sanity check that persistence loses to every model with p≈0 while
> close model pairs show some non-significant cells.

## Model roster (Earth2Studio v0.15.0, verified installed)

Candidates for the board, filtered to global 0.25° medium-range models that
fit the pipeline's 6 h-step verification. **Wave 1** runs in the existing
`earth2` env (ONNX/torch, like FuXi); **wave 2** needs its own conda env.

| Model | Class | Precip | Deps / env | Notes |
|---|---|---|---|---|
| `fengwu` | `FengWu` | none | ONNX — same env as FuXi | 0.25°, 6 h, 69 vars, two-time-step input like FuXi. State vars only. |
| `pangu6` | `Pangu6` | none | ONNX — same env | 0.25°, interleaved 24 h+6 h models. Precip possible via the existing `PrecipitationAFNOv2` chain built for Aurora — exercises the diagnostic path on a second model. |
| `sfno` | `SFNO` | none | torch — same env | NVIDIA, 0.25°, 6 h, 73 vars. Modest VRAM. |
| `fcn3` | `FCN3` | none | torch — same env | FourCastNet 3, probabilistic-capable; run deterministic single member for now. |
| `graphcast_oper` | `GraphCastOperational` | native `tp06` | **JAX — new env** `e2s-graphcast` | 0.25°, 13 levels, fine-tuned on HRES → best added for Phase 2 real-time (GFS/HRES init). Precip-native. |
| `aifs` | `AIFS` | native `tp06` | **anemoi + flash-attn — new env** `e2s-aifs` | ECMWF operational AIFS, 0.25°. flash-attn build is slow/fragile — budget time. |

Skipped deliberately: `Pangu24/3` (redundant with pangu6), `FCN` (superseded
by SFNO/FCN3), `GraphCastSmall`/`GenCastMini` (1.0° — grid mismatch with the
0.25° verification), `DLWP`/`DLESyM`/`ACE2ERA5` (coarse/S2S/climate class,
not medium-range), `AIFSENS` (ensemble — needs CRPS metrics first),
`InterpModAFNO` (interpolator, not a forecast model).

**Color budget warning:** scoreboard.html's `MODELS` array has fixed color
slots and a categorical palette stops being readable around 7 series. With
4 models on the board + 6 candidates, don't add them all to every chart —
prompt for a model on/off toggle in the page when the roster passes ~6.

## Step 8 — Add wave-1 models (same env, parallel backfill)

**After Step 2 (so baselines exist) — doesn't need to wait for Phase 2.**

> In `forecast_scoreboard/`, add four models to the registry: `fengwu`
> (`earth2studio.models.px.FengWu`), `pangu6` (`Pangu6`), `sfno` (`SFNO`),
> `fcn3` (`FCN3`) — all global 0.25°, 6 h steps, runnable in the existing
> `earth2` env. For each: add a `config.yaml` entry (`scored_variables:
> [t2m, u10m, v10m, z500, t850, msl]` — none outputs precip natively) and a
> `load_model` mapping in `scoreboard/forecast.py`. For `pangu6` ONLY, also
> wire the existing Aurora-style `PrecipitationAFNOv2` diagnostic chain
> (`build_precip_chain`) so it scores `tp06` — check which chain inputs
> Pangu6 actually outputs and derive the rest as the Aurora chain does.
> Smoke-test each model on one init (2023-01-15T00) sequentially, watching
> VRAM (48 GB cards). Then backfill the same ranges as the existing models
> (Jan 2023 + Jul 1–7) in parallel across the 4 GPUs per the parallel
> cheat-sheet at the top of this file, with `--purge-after-verify` and
> `--no-publish`, publishing once at the end. Add each model to
> scoreboard.html's `MODELS` array with colors: next free categorical slot
> is yellow #eda100/#c98500; for the rest, run the dataviz palette
> validator on the full set before committing. Update README's model table.
> Acceptance: parquet init counts for the new models match the old ones,
> and the published page shows them with distinguishable colors.

## Step 8b — Add wave-2 models (new conda envs)

**After Step 8. Both models are precip-native and matched to operational
initialization, so they're at their best once Phase 2 (GFS init) exists —
but backfilling them on ERA5 now is still valid and comparable.** Note the
env setup involves real waiting (flash-attn compiles from source); consider
running the two env builds in parallel terminals.

> In `forecast_scoreboard/`, add the two models that need their own conda
> envs: `graphcast_oper` (`earth2studio.models.px.GraphCastOperational`,
> JAX-based) and `aifs` (`earth2studio.models.px.AIFS`, needs
> anemoi + flash-attn). Steps:
> (1) **Per-model env support.** `config.yaml` model entries gain an
> optional `conda_env:` key. `PLAN.md` says the orchestrator shells out
> per-model — check whether `scoreboard/run_range.py` actually does; if it
> loads models in-process, add the mechanism: when a model's `conda_env`
> differs from the current env, run its forecast step via
> `conda run -n <env> python -m scoreboard.forecast --model X --init ...`
> (add a CLI to `forecast.py` if it lacks one). Verification stays in the
> `earth2` env — it only reads zarr, no model deps.
> (2) **Envs.** Create `e2s-graphcast` (python 3.12,
> `pip install "earth2studio[graphcast]"` — pulls JAX; check whether the
> CUDA-enabled jaxlib matched to CUDA 12.6 installs, and fix explicitly if
> it defaults to CPU) and `e2s-aifs` (`pip install "earth2studio[aifs]"` —
> flash-attn compiles from source; if it fails, try a prebuilt wheel
> matching torch/CUDA 12.6 before debugging the build). There is a local
> `earth2studio-install` skill — use it.
> (3) **Registry.** Add both to `config.yaml` with `scored_variables:
> [tp06, t2m, u10m, v10m, z500, t850, msl]` and `precip_variable: tp06`
> (both output tp06 natively — no diagnostic chain). Check each model's
> `input_coords()` against what `ARCOInit` provides and extend the derived
> -variable synthesis if an input is missing.
> (4) **Smoke test then backfill.** One init (2023-01-15T00) per model
> first, watching VRAM (48 GB cards). Then backfill Jan 2023 + Jul 1–7 in
> parallel on two free GPUs per the cheat-sheet, `--purge-after-verify
> --no-publish`, publish once at the end.
> (5) **Site.** Add both to scoreboard.html's `MODELS` array — the color
> budget is now past 6 series, so implement the model on/off toggle from
> the roster's color-budget warning (default the toggle to a sensible
> headline subset rather than all-on). Run the dataviz palette validator
> on the full color set. Update README's model table.
> Acceptance: both models have full-range parquet rows including tp06
> scores, `conda run -n earth2` can still drive the whole pipeline
> end-to-end (shelling out per-model where configured), and the published
> page shows the toggle working.

## Later (don't prompt until the above is done)
- **Downstream tasks**: TC tracks vs IBTrACS (`tc_tracking`), wind gusts,
  solar radiation — new `variable`/`metric` rows, no schema change.
- **Public hosting** if the site leaves this box (GitHub Pages push from
  the daily job).
