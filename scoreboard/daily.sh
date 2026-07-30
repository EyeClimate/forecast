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

# Publish. The commit lands on whatever branch is checked out, so pushing a
# hardcoded `main` is only correct when main IS checked out — otherwise the
# commit strands on a feature branch while `git push origin main` pushes an
# unchanged ref, succeeds, and logs "pushed". That silent no-op cost a day of
# scores on 2026-07-30. Push the branch we actually committed to, and say which.
if ! git diff --quiet docs/index.html; then
    branch=$(git rev-parse --abbrev-ref HEAD)
    git add docs/index.html
    git commit --quiet -m "daily: refresh scoreboard $(date -u +%F)"
    if [ "$branch" = "main" ]; then
        git push --quiet origin main && echo "pushed docs/index.html (main)"
    else
        # Still push, so the site never silently goes stale, but make the
        # unusual situation impossible to miss in the log.
        echo "WARNING: on branch '$branch', not main — pushing $branch -> main"
        git push --quiet origin "HEAD:main" \
            && echo "pushed docs/index.html ($branch -> main)" \
            || echo "ERROR: push failed; docs/index.html is committed on '$branch' but NOT live"
    fi
fi

echo "=== done rc=$rc $(date -u +%FT%TZ) ==="
exit $rc
