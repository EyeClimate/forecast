#!/usr/bin/env bash
# Daily real-time scoreboard run, meant for cron:
#
#   30 4 * * * .../scoreboard/daily.sh >> .../data/logs/daily.log 2>&1
#
# Forecasts yesterday's 00z init for every real-time-capable model and
# re-verifies the trailing 8 days — verification is incremental, so each run
# scores only the leads whose GFS-analysis truth newly arrived (a 5-day
# forecast completes over ~6 daily runs). Then sweeps old zarrs, and pushes
# the regenerated docs/index.html so GitHub Pages serves fresh scores.
# The separate published copy is NOT updated here (manual step).
set -u
cd "$(dirname "$0")/.."

CONDA=/home/bowen/miniconda3/bin/conda
# fcn3 needs expandable segments to fit its DISCO conv on a 48 GB card.
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
# atlas is excluded: sst has no GFS mapping (and runs are slow) — see README.
MODELS="fuxi persistence fengwu aurora pangu6 sfno fcn3 graphcast_oper aifs"
START=$(date -u -d '8 days ago' +%Y-%m-%dT00)
END=$(date -u -d 'yesterday' +%Y-%m-%dT00)

echo "=== daily run $(date -u +%FT%TZ) range $START..$END ==="
mkdir -p data
flock -n data/.daily.lock \
    "$CONDA" run -n earth2 --no-capture-output python -m scoreboard.run_range \
    --start "$START" --end "$END" --models $MODELS
rc=$?

"$CONDA" run -n earth2 python -m scoreboard.sweep

if ! git diff --quiet docs/index.html; then
    git add docs/index.html
    git commit --quiet -m "daily: refresh scoreboard $(date -u +%F)"
    git push --quiet origin main && echo "pushed docs/index.html"
fi

echo "=== done rc=$rc $(date -u +%FT%TZ) ==="
exit $rc
