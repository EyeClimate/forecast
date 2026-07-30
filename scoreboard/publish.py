"""Generate the static scoreboard site from metrics.parquet."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr

from . import sources
from .verify import metrics_path

UNITS = {"t2m": "K", "u10m": "m/s", "v10m": "m/s", "z500": "m2/s2",
         "t850": "K", "msl": "Pa", "tp": "mm/6h", "tp06": "mm/6h"}
SCORECARD_LEADS = [24, 72, 120]


def _save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def _lead_curves(df, metric, variable, ylabel, title, out_png):
    sub = df[(df.metric == metric) & (df.variable == variable)
             & (df.region == "global")]
    if sub.empty:
        return False
    fig, ax = plt.subplots(figsize=(7, 4))
    for model, g in sub.groupby("model"):
        curve = g.groupby("lead_hours").value.mean()
        ax.plot(curve.index, curve.values, marker="o", ms=3, label=model)
    ax.set_xlabel("lead time (h)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend()
    _save(fig, out_png)
    return True


def _precip_panels(df, precip_vars, thresholds, assets: Path):
    made = []
    sub = df[df.variable.isin(precip_vars) & (df.region == "global")]
    if sub.empty:
        return made
    for kind in ("csi", "fss"):
        fig, ax = plt.subplots(figsize=(7, 4))
        plotted = False
        for model, gm in sub.groupby("model"):
            for thr in thresholds:
                g = gm[gm.metric == f"{kind}_{thr:g}mm"]
                if g.empty:
                    continue
                curve = g.groupby("lead_hours").value.mean()
                ax.plot(curve.index, curve.values, marker="o", ms=3,
                        label=f"{model} ≥{thr:g} mm")
                plotted = True
        if not plotted:
            plt.close(fig)
            continue
        ax.set_xlabel("lead time (h)")
        ax.set_ylabel(kind.upper())
        ax.set_ylim(0, 1)
        ax.set_title(f"Precipitation {kind.upper()} vs lead time (global)")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8)
        png = assets / f"precip_{kind}.png"
        _save(fig, png)
        made.append(png.name)
    return made


def _precip_map(cfg, df, site: Path):
    """Forecast-vs-truth precip maps for the latest scored (model, init)."""
    from .forecast import forecast_path

    sub = df[df.metric.str.startswith("csi_")]
    if sub.empty:
        return None
    latest = sub.sort_values("init_time").iloc[-1]
    model, init_time = latest.model, latest.init_time.to_pydatetime()
    precip_var = cfg["models"][model]["precip_variable"]
    zpath = forecast_path(Path(cfg["paths"]["data"]), model, init_time)
    if not zpath.exists():
        return None
    try:
        ds = xr.open_zarr(zpath)
        avail = ds["lead_time"].values
        avail = avail[avail > np.timedelta64(0, "h")]
        lt = avail[np.argmin(np.abs(avail - np.timedelta64(24, "h")))]
        lead_h = int(lt / np.timedelta64(1, "h"))
        f_mm = ds[precip_var].isel(time=0).sel(lead_time=lt).values.squeeze() * 1000
        valid = init_time + pd.Timedelta(hours=lead_h)
        truth, _ = sources.truth_source(
            init_time, valid, cfg["historic_cutoff_days"]
        )
        o_mm = truth([valid], [sources.truth_variable(precip_var)]) \
            .isel(time=0).sel(variable=sources.truth_variable(precip_var)) \
            .reindex(lat=ds.lat.values, lon=ds.lon.values, method="nearest") \
            .values * 1000
    except Exception as e:  # noqa: BLE001 — maps are cosmetic
        print(f"[publish] skipping precip map: {e}")
        return None

    fig, axes = plt.subplots(2, 1, figsize=(10, 8))
    vmax = max(np.nanpercentile(o_mm, 99.9), 1.0)
    for ax, field, name in zip(
        axes, [f_mm, o_mm], [f"{model} forecast", "ERA5 truth"]
    ):
        im = ax.imshow(np.clip(field, 0, None), extent=[0, 360, -90, 90],
                       origin="upper", cmap="turbo", vmin=0, vmax=vmax)
        ax.set_title(f"{name} — {precip_var} mm/6h, init "
                     f"{init_time:%Y-%m-%d %Hz} +{lead_h}h")
        fig.colorbar(im, ax=ax, shrink=0.8)
    png = site / "assets" / "precip_map.png"
    _save(fig, png)
    return png.name


def _scorecard(df) -> str:
    sub = df[(df.region == "global") & (df.lead_hours.isin(SCORECARD_LEADS))]
    if sub.empty:
        return "<p>No data.</p>"
    pt = (
        sub.groupby(["model", "variable", "metric", "lead_hours"]).value.mean()
        .reset_index()
        .pivot_table(index=["model", "variable", "metric"],
                     columns="lead_hours", values="value")
        .round(4)
    )
    return pt.to_html(border=0, na_rep="—")


# ---------------------------------------------------------------------------
# Designed page refresh. This is the site's index.html — the page visitors
# land on, and what GitHub Pages serves from docs/. The plain matplotlib
# page generated below is secondary and lands at charts.html.
#
# The page is a self-contained file whose data lives in one inline JSON blob
# (`const DATA = {...};`). Every publish rewrites, in place, exactly three
# things: that blob, the header provenance "inits ..." span, and the footer
# "scores generated ..." timestamp. All other bytes are left untouched —
# markup, styles, scripts, and the MODELS color registry are hand-maintained.

SOURCE_LABELS = {
    "era5_arco": "ERA5 (ARCO)",
    "gfs": "GFS",
    "gfs_analysis": "GFS analysis",
    "imerg_late": "IMERG Late",
}


def _uniq_label(series) -> str:
    vals = sorted(series.unique())
    return vals[0] if len(vals) == 1 else "+".join(vals)


def _pretty_label(raw: str) -> str:
    """Display form of a DATA metadata label: 'era5_arco+gfs' -> 'ERA5 (ARCO)
    + GFS', 'final+provisional' -> 'FINAL + PROVISIONAL'."""
    return " + ".join(SOURCE_LABELS.get(p, p.replace("_", " ").upper())
                      for p in raw.split("+"))


def _init_range_label(inits: list) -> str:
    """'Jan 1–31 + Jul 1–7 2023' from a sorted list of init timestamps."""
    days = sorted({pd.Timestamp(t).normalize() for t in inits})
    runs: list[list] = []
    for d in days:
        if runs and (d - runs[-1][1]).days == 1:
            runs[-1][1] = d
        else:
            runs.append([d, d])
    years = {d.year for d in days}
    parts = []
    for a, b in runs:
        if a == b:
            p = f"{a:%b} {a.day}"
        elif a.month == b.month and a.year == b.year:
            p = f"{a:%b} {a.day}–{b.day}"
        else:
            p = f"{a:%b} {a.day} – {b:%b} {b.day}"
        if len(years) > 1:
            p += f" {a.year}" if a.year == b.year else f" {a.year}–{b.year}"
        parts.append(p)
    label = " + ".join(parts)
    if len(years) == 1:
        label += f" {days[0].year}"
    return label


def _scoreboard_data(df: pd.DataFrame) -> dict:
    """metrics.parquet -> the page's DATA shape: means across init times,
    arrays aligned to `leads`, None where a (model, region, var, metric,
    lead) has no rows."""
    leads = sorted(int(x) for x in df.lead_hours.unique())
    lead_pos = {lh: i for i, lh in enumerate(leads)}
    means = df.groupby(
        ["model", "region", "variable", "metric", "lead_hours"]
    ).value.mean()
    data: dict = {}
    for (model, region, var, metric, lh), v in means.items():
        arr = (data.setdefault(model, {}).setdefault(region, {})
               .setdefault(var, {}).setdefault(metric, [None] * len(leads)))
        arr[lead_pos[int(lh)]] = round(float(v), 5)
    inits = sorted(df.init_time.unique())
    return {
        "leads": leads,
        "init_times": [f"{pd.Timestamp(t):%Y-%m-%d %H:%M:%S}" for t in inits],
        "n_scores": int(len(df)),
        "truth": _uniq_label(df.truth_source),
        "init_source": _uniq_label(df.init_source),
        "tier": _uniq_label(df.tier),
        "data": data,
    }


def refresh_scoreboard(df: pd.DataFrame, mpath: Path, site: Path) -> Path | None:
    """Rewrite DATA, provenance, and timestamp in the designed index.html."""
    page = site / "index.html"
    if not page.exists():
        print(f"[publish] {page} not found, skipping designed page")
        return None
    html = page.read_text()

    # Models present in the parquet but absent from the page's hand-curated
    # MODELS array are embedded in DATA but never drawn — warn, don't invent
    # a color (next free categorical slot: yellow #eda100 / #c98500).
    m = re.search(r"const MODELS = \[(.*?)\];", html, re.S)
    page_models = set(re.findall(r'id: "([^"]+)"', m.group(1))) if m else set()
    missing = sorted(set(df.model.unique()) - page_models)
    if missing:
        print("!" * 74)
        print(f"[publish] WARNING: {missing} in metrics.parquet but NOT in "
              f"{page.name}'s MODELS array — embedded in DATA yet invisible on "
              f"the page. Assign colors manually (next free slot: yellow "
              f"#eda100/#c98500).")
        print("!" * 74)

    payload = _scoreboard_data(df)
    blob = json.dumps(payload, separators=(",", ": "))
    html, n = re.subn(r"const DATA = \{.*?\};",
                      lambda _: f"const DATA = {blob};", html,
                      count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{page.name}: `const DATA = {{...}};` not found")

    n_inits = df.init_time.nunique()
    hours = sorted({pd.Timestamp(t).hour for t in df.init_time.unique()})
    hour_label = "/".join(f"{h:02d}z" for h in hours)
    prov = (f"<span>inits <b>{n_inits} × {hour_label}</b> · "
            f"{_init_range_label(df.init_time.unique())}</span>")
    html, n = re.subn(r"<span>inits <b>.*?</b> · .*?</span>",
                      lambda _: prov, html, count=1)
    if n != 1:
        raise RuntimeError(f"{page.name}: provenance 'inits' span not found")

    # The source/tier chips must track the table too. Once a range mixes
    # regimes (ERA5 historic + GFS real-time) a hardcoded chip would keep
    # claiming all-ERA5 / all-FINAL, which is simply false.
    for field, key in (("init source", "init_source"), ("truth", "truth"),
                       ("tier", "tier")):
        chip = f"<span>{field} <b>{_pretty_label(payload[key])}</b></span>"
        html, n = re.subn(rf"<span>{field} <b>.*?</b></span>",
                          lambda _, c=chip: c, html, count=1)
        if n != 1:
            raise RuntimeError(f"{page.name}: '{field}' chip span not found")

    stamp = datetime.fromtimestamp(
        mpath.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html, n = re.subn(r"scores generated .*? UTC",
                      f"scores generated {stamp}", html, count=1)
    if n != 1:
        raise RuntimeError(f"{page.name}: 'scores generated' stamp not found")

    page.write_text(html)
    print(f"[publish] designed page -> {page} ({n_inits} inits, {stamp})")
    return page


def publish(cfg: dict, models: list[str] | None = None) -> Path:
    """Generate the site. `models` limits which models appear (default: all)."""
    mpath = metrics_path(cfg)
    if not mpath.exists():
        print("[publish] no metrics yet, skipping site generation")
        return None
    site = Path(cfg["paths"]["site"])
    assets = site / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    df_full = pd.read_parquet(mpath)
    # `models` narrows the plain index.html only; the designed page always
    # reflects the whole metrics table.
    df = df_full[df_full.model.isin(models)] if models else df_full

    images = []
    for var in cfg["verification"]["state_variables"]:
        unit = UNITS.get(var, "")
        if _lead_curves(df, "rmse", var, f"RMSE ({unit})",
                        f"{var} RMSE vs lead time (global, lat-weighted)",
                        assets / f"rmse_{var}.png"):
            images.append(f"rmse_{var}.png")
    for var in ("z500", "t2m"):
        if _lead_curves(df, "acc", var, "ACC",
                        f"{var} anomaly correlation vs lead time (global)",
                        assets / f"acc_{var}.png"):
            images.append(f"acc_{var}.png")

    precip_vars = {m.get("precip_variable") for m in cfg["models"].values()}
    precip_vars.discard(None)
    images += _precip_panels(df, precip_vars,
                             cfg["verification"]["precip_thresholds_mm"], assets)
    map_png = _precip_map(cfg, df, site)

    n_inits = df.init_time.nunique()
    models = ", ".join(sorted(df.model.unique()))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    imgs_html = "\n".join(
        f'<img src="assets/{p}" alt="{p}">' for p in images
    )
    map_html = (f'<h2>Latest precipitation map</h2><img src="assets/{map_png}" '
                f'alt="precip map">' if map_png else "")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Forecast Scoreboard</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 980px; margin: 2rem auto;
        padding: 0 1rem; color: #222; }}
 img {{ max-width: 100%; margin: .5rem 0; border: 1px solid #ddd; }}
 table {{ border-collapse: collapse; font-size: .85rem; }}
 th, td {{ padding: .3rem .6rem; border-bottom: 1px solid #ddd; text-align: right; }}
 .meta {{ color: #666; font-size: .85rem; }}
</style></head><body>
<h1>Forecast Scoreboard</h1>
<p class="meta">Models: {models} · init times scored: {n_inits} ·
generated {stamp} · truth: ERA5 (WeatherBench2) · tier: final</p>
<h2>Scorecard (global, mean over init times)</h2>
{_scorecard(df)}
<h2>Skill vs lead time</h2>
{imgs_html}
{map_html}
</body></html>
"""
    # Secondary page: the plain matplotlib charts. index.html belongs to the
    # designed page (refreshed below) — never write it from here.
    out = site / "charts.html"
    out.write_text(html)
    print(f"[publish] charts page -> {out}")

    refresh_scoreboard(df_full, mpath, site)
    return out
