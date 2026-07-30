# Forecast Scoreboard — working notes for AI sessions

Daily verification site comparing AI weather models. Read `PLAN.md` (pipeline),
`README.md`, and `config.yaml` before writing code. `MODEL_METHODS.md` documents
model architectures; `PLAN_EXPLORER.md` + `EXPLORER_STEPS.md` cover the map and
comparison pages.

## Environment

- Python runs in the conda env **`earth2`**:
  `conda run -n earth2 python -m scoreboard.run_range ...`
- Some models need their own env — `config.yaml`'s per-model `conda_env` key
  (`e2s-graphcast`, `e2s-aifs`). `run_range` shells out via `conda run`;
  verification always stays in the orchestrator's env since it only reads zarr.
- Model/data caches: `/home/bowen/projects/fundation/checkpoints`
  (`EARTH2STUDIO_CACHE`).
- 4× RTX 6000 Ada, 48 GB each.

## Data invariants

- **Metrics store** is `data/metrics.parquet`, schema
  `init_time | model | lead_hours | variable | region | metric | value |
  init_source | truth_source | tier`. Appended under an exclusive file lock and
  deduplicated. One `run_range` process per GPU is therefore safe.
- **Idempotency is a hard requirement.** Re-runs must skip completed work, and
  skipping keys off the metrics table, not the presence of a forecast zarr
  (zarrs get purged after verification).
- `data/` is gitignored — forecast zarrs are ~584 MB per model per init.

## Two truth regimes — get this right before touching verification

`config.yaml`'s `historic_cutoff_days: 120` splits inits into:

- **historic** (older): ERA5 init and truth, tier `final`.
- **real-time** (newer): GFS analysis init and truth, tier `provisional`.

The 120-day boundary encodes ARCO's actual ERA5 publication lag (~3 months), not
the ~6 days originally assumed. Don't lower it without checking that lag.

**Real-time inits have no precipitation truth at all** — `verify.py:146-149`
sets `precip_var = None` because IMERG Late is not implemented. So precip skill
exists only for inits older than 120 days. Any feature that scores precipitation
must handle this rather than assuming truth is present.

Precip is scored with `_csi` (categorical) and `_fss` (neighborhood) at the
1/5/10 mm thresholds, *not* RMSE — it is non-Gaussian and mostly zero, so
pointwise error is the wrong notion.

## Site publishing

- GitHub Pages serves `docs/` — hence `docs`, not `site`, in `config.yaml`.
- `docs/index.html` is **hand-designed and committed**. `publish.py`'s
  `refresh_scoreboard()` does regex surgery on it, injecting `const DATA = {...}`,
  `const MODELS = [...]`, and the `<style id="model-colors">` block, then
  rewriting the provenance chips and timestamp. It raises on any anchor it cannot
  find, so breakage is loud — but always re-run publish after editing that file,
  **twice**: the injections must survive being applied to their own output, since
  the cron runs daily.
- Run it standalone with `conda run -n earth2 python -m scoreboard.publish`.
- The page is no longer standalone — it links `docs/lib/css/site.css`, shared with
  `compare.html` and `map.html`. That stylesheet must be committed alongside it.
- `docs/charts.html` and `docs/assets/*.png` are regenerated every run and are
  **gitignored**; committing them would add ~1.3 MB of churn per run.
- Authored front-end code goes in `docs/lib/` (committed). `docs/assets/` is the
  generated-artifact directory and is ignored wholesale — don't put source there,
  it will be silently untracked.
- Generated data the pages fetch goes in `docs/data/` (committed, retention-bounded).

## Conventions

- Match the surrounding code's comment density and idiom. The existing modules
  explain *why* a non-obvious choice was made, not what the line does.
- Prefer extending `config.yaml` over hardcoding. Model colours/labels, city
  lists, and map settings belong in its `display:` block, emitted once as
  `docs/data/models.json` — not duplicated per page.
- New pipeline behaviour needs a `README.md` update in the same change.
