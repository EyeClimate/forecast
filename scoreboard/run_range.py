"""Entrypoint: run an evaluation range through forecast -> verify -> publish.

Usage:
  conda run -n earth2 python -m scoreboard.run_range \\
      --start 2023-01-01 --end 2023-01-07 --models atlas
"""

import argparse
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import yaml


def parse_args():
    p = argparse.ArgumentParser(description="Forecast scoreboard range runner")
    p.add_argument("--start", required=True, help="first init time (ISO, e.g. 2023-01-01T00)")
    p.add_argument("--end", required=True, help="last init time (inclusive)")
    p.add_argument("--stride-hours", type=int, default=24)
    p.add_argument("--models", nargs="+", default=None,
                   help="model names from config (default: atlas)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--nsteps", type=int, default=None, help="override forecast steps")
    p.add_argument("--rescore", action="store_true", help="recompute existing metrics")
    p.add_argument("--purge-after-verify", action="store_true",
                   help="delete an init's forecast zarr once its metrics rows "
                        "are confirmed in metrics.parquet")
    p.add_argument("--no-publish", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text())
    if args.nsteps:
        cfg["forecast"]["nsteps"] = args.nsteps
    models = args.models or ["atlas"]
    for m in models:
        if m not in cfg["models"]:
            raise SystemExit(f"Unknown model '{m}' — add it to {args.config}")

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    inits = []
    t = start
    while t <= end:
        inits.append(t)
        t += timedelta(hours=args.stride_hours)
    print(f"Evaluation range: {len(inits)} init times "
          f"({start:%Y-%m-%dT%H} .. {end:%Y-%m-%dT%H}), models: {models}")

    from .forecast import forecast_path, load_model, run_forecast
    from .sweep import purge_forecast
    from .verify import scored_rows, verify_forecast

    data_dir = Path(cfg["paths"]["data"])
    failures = []
    for name in models:
        # Models whose conda_env differs from ours get their forecast step in
        # a subprocess via `conda run` (deps conflict across model families);
        # verification only reads zarr, so it always runs here.
        conda_env = cfg["models"][name].get("conda_env")
        shell_out = bool(conda_env) and conda_env != os.environ.get(
            "CONDA_DEFAULT_ENV"
        )
        model = None
        for init in inits:
            try:
                # Skip keys off the metrics table, not the zarr: an init whose
                # forecast was purged must not be re-forecast.
                done = scored_rows(name, init, cfg)
                if done and not args.rescore:
                    print(f"[run] already scored ({done} rows), skipping "
                          f"{name} {init:%Y-%m-%dT%H}")
                    if args.purge_after_verify:
                        purge_forecast(name, init, cfg)
                    continue
                need_model = not forecast_path(data_dir, name, init).exists()
                if shell_out:
                    if need_model:
                        cmd = [
                            "conda", "run", "-n", conda_env,
                            "--no-capture-output",
                            "python", "-m", "scoreboard.forecast",
                            "--model", name, "--init", f"{init:%Y-%m-%dT%H}",
                            "--config", args.config,
                            "--nsteps", str(cfg["forecast"]["nsteps"]),
                        ]
                        print(f"[run] forecasting {name} in conda env "
                              f"'{conda_env}': {' '.join(cmd)}")
                        subprocess.run(cmd, check=True)
                else:
                    if need_model and model is None and name != "persistence":
                        print(f"[run] loading model {name} (once)...")
                        model = load_model(name)
                    run_forecast(name, init, cfg, model=model)
                try:
                    verify_forecast(name, init, cfg, rescore=args.rescore)
                except NotImplementedError as e:
                    # Real-time inits: forecast now, score later when truth
                    # arrives — no metrics rows, so a re-run retries verify
                    # while the existing zarr makes the forecast step a no-op.
                    print(f"[run] verification deferred for {name} "
                          f"{init:%Y-%m-%dT%H}: {e}")
                    continue
                # purge_forecast re-checks the metrics table before deleting,
                # so a failed/partial verify (which raises above) never purges.
                if args.purge_after_verify:
                    purge_forecast(name, init, cfg)
            except Exception as e:  # noqa: BLE001 — keep the range going
                print(f"[run] FAILED {name} {init:%Y-%m-%dT%H}: {e}")
                failures.append((name, init, str(e)))
        # free GPU memory between models
        if model is not None:
            del model
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            model = None

    if not args.no_publish:
        from .publish import publish

        publish(cfg)

    if failures:
        print(f"\n{len(failures)} failures:")
        for name, init, err in failures:
            print(f"  {name} {init:%Y-%m-%dT%H}: {err[:200]}")
        raise SystemExit(1)
    print("Done.")


if __name__ == "__main__":
    main()
