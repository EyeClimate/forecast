"""Score a stored forecast against truth; append rows to metrics.parquet.

Metrics table schema (one row per score):
  init_time | model | lead_hours | variable | region | metric | value
  | init_source | truth_source | tier

This module also captures the *point* samples the explorer pages need, into a
second store — points.parquet (PLAN_EXPLORER.md §4a):
  init_time | model | lead_hours | variable | city | lat | lon | value

They live together because they have to be produced together. metrics.parquet
holds region-aggregated skill, not fields, so a city time series cannot be
reconstructed from it; the only place a city value can be read is the forecast
zarr, and sweep.py deletes that as soon as the scores land. Sampling here —
where the zarr is already open and the leads are already being iterated — is
the one moment both facts are true.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from scipy.ndimage import uniform_filter

from . import sources
# export.py owns the points contract — the city list, the slugs used as ids and
# the sampler — and reads this store back. Its own imports of this module are
# deliberately function-local, so the dependency runs one way: verify -> export.
from .export import bilinear_sample, cities_payload
from .forecast import forecast_path

KEY_COLS = ["init_time", "model", "lead_hours", "variable", "region", "metric"]
POINT_KEY_COLS = ["init_time", "model", "lead_hours", "variable", "city"]


def metrics_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "metrics.parquet"


def points_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "points.parquet"


def scored_rows(model_name: str, init_time: datetime, cfg: dict) -> int:
    """Number of metric rows present for (model, init).

    The metrics append in verify_forecast is all-or-nothing (a single locked
    write after every lead is scored), so a nonzero count means verification
    completed for this init — safe to key skip/purge decisions off it.
    """
    mpath = metrics_path(cfg)
    if not mpath.exists():
        return 0
    df = pd.read_parquet(mpath, columns=["init_time", "model"])
    return int(((df.model == model_name)
                & (df.init_time == pd.Timestamp(init_time))).sum())


def scored_lead_hours(model_name: str, init_time: datetime, cfg: dict) -> set:
    """Lead hours already scored for (model, init)."""
    mpath = metrics_path(cfg)
    if not mpath.exists():
        return set()
    df = pd.read_parquet(mpath, columns=["init_time", "model", "lead_hours"])
    sel = (df.model == model_name) & (df.init_time == pd.Timestamp(init_time))
    return {int(x) for x in df.lead_hours[sel].unique()}


def fully_scored(model_name: str, init_time: datetime, cfg: dict) -> bool:
    """True once the final lead is scored. Real-time inits are scored
    incrementally as truth arrives, so mere row presence (scored_rows > 0)
    doesn't mean verification is complete."""
    return cfg["forecast"]["nsteps"] * 6 in scored_lead_hours(
        model_name, init_time, cfg
    )


# ---------------------------------------------------------------------------
# City point sampling (PLAN_EXPLORER.md §4a, EXPLORER_STEPS.md E2)


def sampled_coverage(model_name: str, init_time: datetime,
                     cfg: dict) -> tuple[set, set]:
    """(lead hours, city ids) already in points.parquet for (model, init)."""
    ppath = points_path(cfg)
    if not ppath.exists():
        return set(), set()
    df = pd.read_parquet(ppath, columns=["init_time", "model", "lead_hours", "city"])
    sel = (df.model == model_name) & (df.init_time == pd.Timestamp(init_time))
    return ({int(x) for x in df.lead_hours[sel].unique()},
            set(df.city[sel].unique()))


def fully_sampled(model_name: str, init_time: datetime, cfg: dict) -> bool:
    """True once every configured city has been sampled out to the final lead.

    The city set matters as much as the lead set: adding a city to
    config.yaml's `display.cities` leaves every existing init short one column,
    and the export would then emit that city as a hole rather than as data.
    """
    leads, cities = sampled_coverage(model_name, init_time, cfg)
    want = {c["id"] for c in cities_payload(cfg)}
    return cfg["forecast"]["nsteps"] * 6 in leads and want <= cities


def sample_points(model_name: str, init_time: datetime, cfg: dict,
                  ds: xr.Dataset | None = None, resample: bool = False) -> int:
    """Sample `display.cities` out of one forecast zarr into points.parquet.

    Unlike the metrics, this needs no truth and no availability window — the
    forecast covers every lead the moment it is written — so it is captured in
    one pass and never revisited. Truth for those same points is fetched by
    export.py from ERA5/GFS, which stay available indefinitely; only the
    forecast side is perishable, and this is the function that makes it durable.

    Values are stored exactly as the zarr holds them, precipitation included
    (metres, not mm): this table is a record of the field, and unit
    presentation belongs to the exporter that reads it back.

    Returns the number of rows written.
    """
    if not resample and fully_sampled(model_name, init_time, cfg):
        return 0

    cities = cities_payload(cfg)
    close = ds is None
    if ds is None:
        zpath = forecast_path(Path(cfg["paths"]["data"]), model_name, init_time)
        if not zpath.exists():
            raise FileNotFoundError(f"No forecast at {zpath}")
        ds = xr.open_zarr(zpath)
    try:
        lead_td = ds["lead_time"].values
        lead_td = lead_td[lead_td > np.timedelta64(0, "h")]
        lead_hours = (lead_td / np.timedelta64(1, "h")).astype(int)
        declared = cfg["models"][model_name]["scored_variables"]
        variables = [v for v in declared if v in ds]
        if len(variables) != len(declared):
            print(f"[points] {model_name} {init_time:%Y-%m-%dT%H}: "
                  f"{sorted(set(declared) - set(variables))} declared in config "
                  "but absent from the zarr")

        plat = np.array([c["lat"] for c in cities])
        plon = np.array([c["lon"] for c in cities])
        ids = [c["id"] for c in cities]
        n_l, n_c = len(lead_hours), len(cities)
        frames = []
        for var in variables:
            field = ds[var].isel(time=0).sel(lead_time=lead_td).values
            sampled = bilinear_sample(field, ds["lat"].values, ds["lon"].values,
                                      plat, plon)          # (lead, city)
            frames.append(pd.DataFrame({
                "init_time": pd.Timestamp(init_time),
                "model": model_name,
                "lead_hours": np.repeat(lead_hours, n_c),
                "variable": var,
                "city": np.tile(ids, n_l),
                "lat": np.tile(plat, n_l),
                "lon": np.tile(plon, n_l),
                "value": np.asarray(sampled, dtype="float64").reshape(-1),
            }))
    finally:
        if close:
            ds.close()
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)

    ppath = points_path(cfg)
    ppath.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    with open(ppath.parent / ".points.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        existing = pd.read_parquet(ppath) if ppath.exists() else None
        if existing is not None:
            df = (pd.concat([existing, df], ignore_index=True)
                  .drop_duplicates(subset=POINT_KEY_COLS, keep="last"))
        df.to_parquet(ppath, index=False)
    n = n_l * n_c * len(variables)
    print(f"[points] sampled {len(cities)} cities x {n_l} leads x "
          f"{len(variables)} vars for {model_name} {init_time:%Y-%m-%dT%H} "
          f"-> {ppath}")
    return n


def _lat_weights(lat: np.ndarray) -> np.ndarray:
    w = np.cos(np.deg2rad(lat))
    return np.clip(w, 0.0, None)


def _region_masks(lat: np.ndarray, regions: dict) -> dict:
    return {
        name: (lat >= lo) & (lat <= hi) for name, (lo, hi) in regions.items()
    }


def _weighted_stats(err: np.ndarray, w2d: np.ndarray, mask_rows: np.ndarray):
    """(rmse, bias) of err over masked rows with 2-D lat weights."""
    e = err[mask_rows]
    w = w2d[mask_rows]
    wsum = w.sum()
    rmse = float(np.sqrt((w * e**2).sum() / wsum))
    bias = float((w * e).sum() / wsum)
    return rmse, bias


def _acc(f: np.ndarray, o: np.ndarray, c: np.ndarray, w2d: np.ndarray,
         mask_rows: np.ndarray) -> float:
    fa = (f - c)[mask_rows]
    oa = (o - c)[mask_rows]
    w = w2d[mask_rows]
    num = (w * fa * oa).sum()
    den = np.sqrt((w * fa**2).sum() * (w * oa**2).sum())
    return float(num / den) if den > 0 else np.nan


def _csi(f_mm: np.ndarray, o_mm: np.ndarray, thr: float,
         mask_rows: np.ndarray) -> float:
    fe = f_mm[mask_rows] >= thr
    oe = o_mm[mask_rows] >= thr
    hits = np.sum(fe & oe)
    denom = hits + np.sum(fe & ~oe) + np.sum(~fe & oe)
    return float(hits / denom) if denom > 0 else np.nan


def _fss(f_mm: np.ndarray, o_mm: np.ndarray, thr: float, window: int,
         mask_rows: np.ndarray) -> float:
    # Fractions over the full grid, then scored over the region rows
    pf = uniform_filter((f_mm >= thr).astype(np.float32), size=window, mode="nearest")
    po = uniform_filter((o_mm >= thr).astype(np.float32), size=window, mode="nearest")
    pf, po = pf[mask_rows], po[mask_rows]
    denom = np.sum(pf**2) + np.sum(po**2)
    return float(1.0 - np.sum((pf - po) ** 2) / denom) if denom > 0 else np.nan


def verify_forecast(model_name: str, init_time: datetime, cfg: dict,
                    rescore: bool = False) -> int:
    """Score one (model, init) forecast. Returns number of metric rows written."""
    data_dir = Path(cfg["paths"]["data"])
    zpath = forecast_path(data_dir, model_name, init_time)
    if not zpath.exists():
        raise FileNotFoundError(f"No forecast at {zpath}")

    mpath = metrics_path(cfg)
    already = set() if rescore else scored_lead_hours(model_name, init_time, cfg)

    vcfg = cfg["verification"]
    mcfg = cfg["models"][model_name]
    precip_var = mcfg.get("precip_variable")  # None -> model has no precip
    state_vars = [v for v in vcfg["state_variables"]
                  if v in mcfg["scored_variables"]]

    ds = xr.open_zarr(zpath)
    init_src_file = zpath.parent / (zpath.name + ".init_source")
    init_source = init_src_file.read_text().strip() if init_src_file.exists() else "unknown"

    # Before anything can return: point values live only in this zarr, and the
    # metrics written below are exactly what licenses sweep.py to delete it. A
    # failure here is deliberately fatal — a re-run recovers the metrics, but
    # nothing recovers the points once the forecast is gone.
    sample_points(model_name, init_time, cfg, ds=ds, resample=rescore)

    lead_td = ds["lead_time"].values
    leads = lead_td[lead_td > np.timedelta64(0, "h")]
    lead_hours = (leads / np.timedelta64(1, "h")).astype(int)
    valid_times = [
        pd.Timestamp(np.datetime64(init_time) + lt).to_pydatetime() for lt in leads
    ]

    realtime = sources.regime(init_time, cfg["historic_cutoff_days"]) == "realtime"
    tier = "provisional" if realtime else "final"
    avail_cutoff = None
    if realtime:
        # GFS analysis lands ~4 h after cycle time; leads past the cutoff are
        # left unscored and picked up by later runs as their truth arrives.
        avail_cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
                        - timedelta(hours=6))
        if precip_var:
            print("[verify] real-time precip truth (IMERG Late) not "
                  "implemented — skipping precip metrics for this init")
            precip_var = None

    todo = [
        (lt, lh, vt) for lt, lh, vt in zip(leads, lead_hours, valid_times)
        if int(lh) not in already
        and (avail_cutoff is None or vt <= avail_cutoff)
    ]
    if not todo:
        held = "truth not yet available for the rest" if realtime else "done"
        print(f"[verify] nothing new to score for {model_name} "
              f"{init_time:%Y-%m-%dT%H} ({len(already)} leads scored, {held})")
        return 0
    leads = np.array([lt for lt, _, _ in todo])
    lead_hours = [lh for _, lh, _ in todo]
    valid_times = [vt for _, _, vt in todo]

    truth, truth_label = sources.truth_source(
        init_time, valid_times[-1], cfg["historic_cutoff_days"]
    )
    fc_vars = state_vars + ([precip_var] if precip_var else [])
    truth_vars = [sources.truth_variable(v) for v in fc_vars]
    print(f"[verify] fetching truth ({truth_label}) for {len(valid_times)} valid "
          f"times x {len(truth_vars)} vars ...")
    tda = truth(valid_times, truth_vars)  # (time, variable, lat, lon)

    clim = None
    if vcfg.get("compute_acc", False):
        try:
            csrc, _ = sources.climatology_source()
            clim_vars = [sources.truth_variable(v) for v in state_vars]
            clim = csrc(valid_times, clim_vars)
        except Exception as e:  # noqa: BLE001 — ACC is optional, never fatal
            print(f"[verify] climatology unavailable, skipping ACC: {e}")

    lat = ds["lat"].values
    lon = ds["lon"].values
    # Align truth grid to forecast grid orientation
    tda = tda.reindex(lat=lat, lon=lon, method="nearest", tolerance=1e-5)
    if clim is not None:
        clim = clim.reindex(lat=lat, lon=lon, method="nearest", tolerance=1e-5)

    w2d = np.broadcast_to(_lat_weights(lat)[:, None], (lat.size, lon.size))
    masks = _region_masks(lat, vcfg["regions"])

    rows = []

    def add(lead_h, var, region, metric, value):
        rows.append(dict(
            init_time=pd.Timestamp(init_time), model=model_name,
            lead_hours=int(lead_h), variable=var, region=region, metric=metric,
            value=value, init_source=init_source, truth_source=truth_label,
            tier=tier,
        ))

    for i, (lt, lh, vt) in enumerate(zip(leads, lead_hours, valid_times)):
        for var in state_vars:
            f = ds[var].isel(time=0).sel(lead_time=lt).values.squeeze()
            o = tda.isel(time=i).sel(variable=sources.truth_variable(var)).values
            for region, mask in masks.items():
                rmse, bias = _weighted_stats(f - o, w2d, mask)
                add(lh, var, region, "rmse", rmse)
                add(lh, var, region, "bias", bias)
                if clim is not None:
                    c = clim.isel(time=i).sel(
                        variable=sources.truth_variable(var)).values
                    add(lh, var, region, "acc", _acc(f, o, c, w2d, mask))

        if precip_var is None:
            continue
        # Precipitation (m -> mm per 6 h)
        f_mm = ds[precip_var].isel(time=0).sel(lead_time=lt).values.squeeze() * 1000.0
        o_mm = tda.isel(time=i).sel(
            variable=sources.truth_variable(precip_var)).values * 1000.0
        f_mm = np.clip(np.nan_to_num(f_mm), 0.0, None)
        o_mm = np.clip(np.nan_to_num(o_mm), 0.0, None)
        for region, mask in masks.items():
            rmse, bias = _weighted_stats(f_mm - o_mm, w2d, mask)
            add(lh, precip_var, region, "rmse_mm", rmse)
            add(lh, precip_var, region, "bias_mm", bias)
            for thr in vcfg["precip_thresholds_mm"]:
                add(lh, precip_var, region, f"csi_{thr:g}mm",
                    _csi(f_mm, o_mm, thr, mask))
                add(lh, precip_var, region, f"fss_{thr:g}mm",
                    _fss(f_mm, o_mm, thr, vcfg["fss_window_cells"], mask))

    df = pd.DataFrame(rows)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive lock: parallel per-GPU runs share this file
    import fcntl

    with open(mpath.parent / ".metrics.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        existing = pd.read_parquet(mpath) if mpath.exists() else None
        if existing is not None:
            df = (
                pd.concat([existing, df], ignore_index=True)
                .drop_duplicates(subset=KEY_COLS, keep="last")
            )
        df.to_parquet(mpath, index=False)
    print(f"[verify] wrote {len(rows)} rows -> {mpath}")
    return len(rows)


def main():
    """`python -m scoreboard.verify --backfill-points [--resample]`.

    Point sampling was added to the pipeline after forecasts had already been
    run and swept, so the store starts empty while some zarrs are still on
    disk. This walks whatever survives and captures it — the only chance those
    inits get, since sweep.py has already taken the rest.
    """
    import argparse

    import yaml

    p = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--backfill-points", action="store_true", required=True,
                   help="sample display.cities from every forecast zarr on disk")
    p.add_argument("--resample", action="store_true",
                   help="re-sample inits already present in points.parquet")
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())

    root = Path(cfg["paths"]["data"]) / "forecasts"
    total = 0
    for zpath in sorted(root.glob("*/*.zarr")):
        try:
            init = datetime.strptime(zpath.stem, "%Y-%m-%dT%H")
        except ValueError:
            print(f"[points] unrecognized name, skipping {zpath}")
            continue
        total += sample_points(zpath.parent.name, init, cfg, resample=a.resample)
    print(f"[points] backfill wrote {total} row(s) -> {points_path(cfg)}")


if __name__ == "__main__":
    main()
