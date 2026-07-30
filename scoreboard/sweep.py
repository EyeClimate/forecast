"""Forecast retention: delete scored forecast zarrs to reclaim disk.

Two entry points:
  - purge_forecast(): delete one init's zarr iff its scores are in
    metrics.parquet (used by run_range.py --purge-after-verify).
  - CLI: delete zarrs whose files are older than the retention window
    (config `retention_days`, default 30) and whose scores exist. Meant to
    run from cron:
      conda run -n earth2 python -m scoreboard.sweep [--dry-run] [--days N]

Age is the zarr's mtime (when the forecast was produced), not its init time —
a historic backfill run yesterday for 2023 inits must survive the sweep.

A forecast with no metrics rows is never deleted, whatever its age: the
metrics append in verify.py is all-or-nothing, so missing rows mean a failed
or never-run verify and the zarr is still needed.
"""

import argparse
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .forecast import forecast_path
from .verify import scored_rows


def purge_forecast(model_name: str, init_time: datetime, cfg: dict,
                   dry_run: bool = False) -> bool:
    """Delete one (model, init) forecast zarr iff its scores are present.

    The .init_source sidecar goes with it: its content is already folded into
    the metrics rows' init_source column. Returns True if deleted (or would
    be, under dry_run).
    """
    zpath = forecast_path(Path(cfg["paths"]["data"]), model_name, init_time)
    if not zpath.exists():
        return False
    nrows = scored_rows(model_name, init_time, cfg)
    if nrows == 0:
        print(f"[purge] no metrics rows, keeping {zpath}")
        return False
    if dry_run:
        print(f"[purge] would delete {zpath} ({nrows} metric rows)")
        return True
    shutil.rmtree(zpath)
    zpath.parent.joinpath(zpath.name + ".init_source").unlink(missing_ok=True)
    print(f"[purge] deleted {zpath} ({nrows} metric rows retained)")
    return True


def parse_args():
    p = argparse.ArgumentParser(
        description="Delete scored forecast zarrs older than the retention window")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--days", type=int, default=None,
                   help="retention window (default: retention_days from config)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be deleted without deleting")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    days = args.days if args.days is not None else cfg.get("retention_days", 30)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    root = Path(cfg["paths"]["data"]) / "forecasts"
    n = 0
    for zpath in sorted(root.glob("*/*.zarr")):
        try:
            init = datetime.strptime(zpath.stem, "%Y-%m-%dT%H")
        except ValueError:
            print(f"[sweep] unrecognized name, skipping {zpath}")
            continue
        mtime = datetime.fromtimestamp(zpath.stat().st_mtime, tz=timezone.utc)
        if mtime >= cutoff:
            continue
        if purge_forecast(zpath.parent.name, init, cfg, dry_run=args.dry_run):
            n += 1
    verb = "would delete" if args.dry_run else "deleted"
    print(f"[sweep] {verb} {n} forecast zarr(s) older than {days} d")


if __name__ == "__main__":
    main()
