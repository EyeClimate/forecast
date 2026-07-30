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

from . import export, sources
from .verify import metrics_path

# Units and the provenance-label helpers live in export.py — manifest.json has
# to describe an init the same way this page's chips do, so there is one copy.
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
# The page's data lives in one inline JSON blob (`const DATA = {...};`). Every
# publish rewrites, in place, only these spots: that blob, the `const MODELS`
# array and the `<style id="model-colors">` block (both generated from
# config.yaml's `display.models`), the header provenance "inits ..." span, the
# source/tier chips, and the footer "scores generated ..." timestamp. All other
# bytes — markup, styles, scripts — are hand-maintained and left untouched.

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


def _round(v: float):
    """Magnitude-aware rounding: keeps ACC's 5 decimals without spending 5 on
    z500 RMSE in the hundreds. Non-finite -> None (NaN is not valid JSON)."""
    if v is None or not np.isfinite(v):
        return None
    a = abs(v)
    return round(v, 2) if a >= 100 else round(v, 3) if a >= 1 else round(v, 5)


def _scoreboard_data(df: pd.DataFrame) -> dict:
    """metrics.parquet -> the page's DATA shape.

    Values are stored PER INIT TIME, not pre-averaged, so the page can
    re-average over any user-selected init window client-side:

        data[model][region][var][metric][init_idx] = [value per lead]

    `init_idx` indexes `init_times`; a null in place of the inner array means
    that init has no rows for that series, and a null inside it means that
    lead is unscored (real-time inits fill in as truth arrives).
    """
    leads = sorted(int(x) for x in df.lead_hours.unique())
    lead_pos = {lh: i for i, lh in enumerate(leads)}
    inits = sorted(pd.Timestamp(t) for t in df.init_time.unique())
    init_pos = {t: i for i, t in enumerate(inits)}
    n_inits = len(inits)

    data: dict = {}
    for r in df.itertuples(index=False):
        per_init = (data.setdefault(r.model, {}).setdefault(r.region, {})
                    .setdefault(r.variable, {})
                    .setdefault(r.metric, [None] * n_inits))
        i = init_pos[r.init_time]
        if per_init[i] is None:
            per_init[i] = [None] * len(leads)
        per_init[i][lead_pos[int(r.lead_hours)]] = _round(float(r.value))
    return {
        "leads": leads,
        "init_times": [f"{pd.Timestamp(t):%Y-%m-%d %H:%M:%S}" for t in inits],
        "n_scores": int(len(df)),
        "truth": export.uniq_label(df.truth_source),
        "init_source": export.uniq_label(df.init_source),
        "tier": export.uniq_label(df.tier),
        "data": data,
    }


def refresh_scoreboard(df: pd.DataFrame, mpath: Path, site: Path,
                       cfg: dict | None = None) -> Path | None:
    """Rewrite DATA, the model registry, provenance, and timestamp in index.html."""
    page = site / "index.html"
    if not page.exists():
        print(f"[publish] {page} not found, skipping designed page")
        return None
    html = page.read_text()

    # The MODELS array and the model colour custom properties are generated from
    # config.yaml's `display.models`, so the page cannot drift from the registry
    # the way it used to (this replaced a hand-curated array plus a warning).
    # A model scored but absent from `display.models` is still worth shouting
    # about: it lands in DATA yet has no colour or label to draw with.
    if cfg is not None:
        html, n = re.subn(r"const MODELS = \[.*?\];",
                          lambda _: export.models_js_array(cfg), html,
                          count=1, flags=re.S)
        if n != 1:
            raise RuntimeError(f"{page.name}: `const MODELS = [...];` not found")
        html, n = re.subn(r'<style id="model-colors">.*?</style>',
                          lambda _: ('<style id="model-colors">\n'
                                     + export.model_colors_css(cfg)
                                     + "</style>"),
                          html, count=1, flags=re.S)
        if n != 1:
            raise RuntimeError(
                f'{page.name}: `<style id="model-colors">` block not found')

        configured = {m["id"] for m in export.models_payload(cfg)}
        missing = sorted(set(df.model.unique()) - configured)
        if missing:
            print("!" * 74)
            print(f"[publish] WARNING: {missing} in metrics.parquet but NOT in "
                  f"config.yaml's `display.models` — embedded in DATA yet "
                  f"invisible on the page. Add them there with a colour (next "
                  f"free categorical slot: teal #0f9b9b / #2bb8b8).")
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
        chip = f"<span>{field} <b>{export.pretty_label(payload[key])}</b></span>"
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
        unit = export.UNITS.get(var, "")
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

    refresh_scoreboard(df_full, mpath, site, cfg)
    print(f"[publish] models.json -> {export.write_models_json(cfg, site)}")
    return out


def main():
    """`python -m scoreboard.publish` — regenerate the site from the parquet.

    Publishing has only ever run as a tail of `run_range`, which makes verifying
    a page edit awkward. This entry point exists so it can be run on its own.
    """
    import argparse

    import yaml

    p = argparse.ArgumentParser(
        description="Regenerate the scoreboard site from data/metrics.parquet")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--models", nargs="*", default=None,
                   help="limit the plain charts page to these models")
    a = p.parse_args()
    publish(yaml.safe_load(Path(a.config).read_text()), a.models)


if __name__ == "__main__":
    main()
