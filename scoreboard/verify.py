"""Score a stored forecast against truth; append rows to metrics.parquet.

Metrics table schema (one row per score):
  init_time | model | lead_hours | variable | region | metric | value
  | init_source | truth_source | tier
"""

from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from scipy.ndimage import uniform_filter

from . import sources
from .forecast import forecast_path

KEY_COLS = ["init_time", "model", "lead_hours", "variable", "region", "metric"]


def metrics_path(cfg: dict) -> Path:
    return Path(cfg["paths"]["data"]) / "metrics.parquet"


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
    if not rescore:
        done = scored_rows(model_name, init_time, cfg)
        if done:
            print(f"[verify] already scored ({done} rows), skipping "
                  f"{model_name} {init_time:%Y-%m-%dT%H}")
            return 0

    vcfg = cfg["verification"]
    mcfg = cfg["models"][model_name]
    precip_var = mcfg.get("precip_variable")  # None -> model has no precip
    state_vars = [v for v in vcfg["state_variables"]
                  if v in mcfg["scored_variables"]]

    ds = xr.open_zarr(zpath)
    init_src_file = zpath.parent / (zpath.name + ".init_source")
    init_source = init_src_file.read_text().strip() if init_src_file.exists() else "unknown"

    lead_td = ds["lead_time"].values
    leads = lead_td[lead_td > np.timedelta64(0, "h")]
    lead_hours = (leads / np.timedelta64(1, "h")).astype(int)
    valid_times = [
        pd.Timestamp(np.datetime64(init_time) + lt).to_pydatetime() for lt in leads
    ]

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
            tier="final",
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
