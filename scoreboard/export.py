"""Emit the explorer pages' data payloads under docs/data/.

`docs/index.html` stays self-contained-ish (metrics inlined by publish.py), but
compare.html and map.html fetch what they need from docs/data/ instead — a map
payload is far too large to inline, and a fetched file is browser-cached.

This module owns `models.json`, `manifest.json` and `points/<init>/<city>.json`
(EXPLORER_STEPS.md E2). The gridded field export lands in `fields.py` (E4) and
fills in the manifest's reserved `fields` section.

`docs/data/schema/` carries a JSON Schema for each emitted file type, and
`scripts/check_export.py` validates every file against it. That pairing is the
whole point of freezing the contract here: E3's page and E4's exporter both code
against the schema rather than against whatever this module happened to emit.

Run standalone:

    conda run -n earth2 python -m scoreboard.export [--init 2026-07-28T00] [--force]
"""

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from . import sources


def models_payload(cfg: dict) -> list[dict]:
    """The `display.models` registry, in legend/draw order."""
    try:
        return cfg["display"]["models"]
    except KeyError as e:
        raise RuntimeError(
            "config.yaml has no `display.models` block — every page reads model "
            "labels and colours from it, so it is required."
        ) from e


def write_models_json(cfg: dict, site: Path) -> Path:
    """Write docs/data/models.json from config.yaml's display block."""
    out = site / "data" / "models.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(models_payload(cfg), indent=2, ensure_ascii=False) + "\n")
    return out


def model_colors_css(cfg: dict) -> str:
    """Generate the `--s-*` custom properties for all three theme scopes.

    Hand-maintaining these drifted once already: before E1 the
    `[data-theme="dark"]` scope was missing --s-graphcast and --s-aifs, so a
    manual dark-theme toggle drew those two models in their light-theme colours.
    Generating all three scopes from one source makes that class of bug
    impossible rather than merely fixed.
    """
    models = models_payload(cfg)
    light = "".join(f"  {m['css_var']}: {m['color']};\n" for m in models)
    dark = "".join(f"    {m['css_var']}: {m['color_dark']};\n" for m in models)
    dark_flat = dark.replace("    ", "  ")
    return (
        "/* generated from config.yaml `display.models` by scoreboard/publish.py */\n"
        f":root {{\n{light}}}\n"
        "@media (prefers-color-scheme: dark) {\n"
        f'  :root:where(:not([data-theme="light"])) {{\n{dark}  }}\n'
        "}\n"
        f':root[data-theme="dark"] {{\n{dark_flat}}}\n'
    )


def models_js_array(cfg: dict) -> str:
    """Render config's display.models as index.html's `const MODELS = [...]`."""
    rows = []
    for m in models_payload(cfg):
        parts = [f'id: "{m["id"]}"', f'name: "{m["label"]}"']
        if m.get("short"):
            parts.append(f'short: "{m["short"]}"')
        parts.append(f'cvar: "{m["css_var"]}"')
        parts.append(
            f'hex: {{ light: "{m["color"]}", dark: "{m["color_dark"]}" }}'
        )
        parts.append(f'w: {m["width"]}')
        if m.get("baseline"):
            parts.append("baseline: true")
        rows.append("  { " + ", ".join(parts) + " },")
    return "const MODELS = [\n" + "\n".join(rows) + "\n];"


# ---------------------------------------------------------------------------
# Provenance labels. These live here rather than in publish.py because both the
# designed page's chips and manifest.json's per-init regime labels have to say
# the same thing about the same init — publish.py imports them back.

UNITS = {"t2m": "K", "u10m": "m/s", "v10m": "m/s", "z500": "m2/s2",
         "t850": "K", "msl": "Pa", "tp": "mm/6h", "tp06": "mm/6h"}

SOURCE_LABELS = {
    "era5_arco": "ERA5 (ARCO)",
    "gfs": "GFS",
    "gfs_analysis": "GFS analysis",
    "imerg_late": "IMERG Late",
}


def uniq_label(series) -> str:
    """'era5_arco' from a column that is all one value, 'a+b' when it is mixed."""
    vals = sorted(series.unique())
    return vals[0] if len(vals) == 1 else "+".join(vals)


def pretty_label(raw: str) -> str:
    """Display form of a provenance label: 'era5_arco+gfs' -> 'ERA5 (ARCO) +
    GFS', 'final+provisional' -> 'FINAL + PROVISIONAL'."""
    return " + ".join(SOURCE_LABELS.get(p, p.replace("_", " ").upper())
                      for p in raw.split("+"))


# ---------------------------------------------------------------------------
# The points contract (EXPLORER_STEPS.md E2)

SCHEMA_VERSION = 1
INIT_FMT = "%Y-%m-%dT%H"        # directory name, same as the forecast zarr stem
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Every series in a points file carries one of these. Absence is deliberately
# NOT expressed as a bare null series, because the two reasons a precipitation
# series can be missing are different facts and a page that drew nothing for
# both would imply a model failed when it simply has no precip head:
#
#   no_variable    the model has no `precip_variable` in config.yaml at all
#                  (fengwu, sfno, fcn3) — there is no forecast to show, ever.
#   truth_pending  the init is real-time, so verify.py:146-149 dropped precip
#                  truth (IMERG Late is not implemented). The *forecast* is
#                  fine; only the reference line is missing, and it will appear
#                  for this variable once IMERG lands.
#
# `ok` means `values` is present. A null AT a lead inside an `ok` series means
# "no value at that lead" — normal for real-time truth, whose later valid times
# are still in the future (see `truth_valid_through`).
STATUS_OK = "ok"
STATUS_NO_VARIABLE = "no_variable"
STATUS_TRUTH_PENDING = "truth_pending"
STATUS_UNAVAILABLE = "unavailable"   # declared in config but absent from the
                                     # forecast store: a pipeline anomaly, kept
                                     # representable so one bad zarr cannot
                                     # take the whole daily export down
SERIES_STATUSES = (STATUS_OK, STATUS_NO_VARIABLE, STATUS_TRUTH_PENDING,
                   STATUS_UNAVAILABLE)

# Precipitation is canonicalized to tp06 in the emitted JSON. atlas and
# persistence call their 6 h accumulation `tp` and everyone else `tp06`, but
# they are the same quantity — `sources.truth_variable` already collapses them
# for verification, and a comparison page with two tabs meaning the same thing
# is a bug generator. The native name is recorded on the series.
CANONICAL_PRECIP = "tp06"

# Rounding per variable, chosen so the JSON keeps the precision a reader can
# act on and spends no bytes past it: 0.01 K on temperature, 0.1 Pa / 0.1
# m2/s2, 0.001 mm on precip (the 1 mm CSI threshold needs the drizzle band).
DECIMALS = {"t2m": 2, "t850": 2, "u10m": 2, "v10m": 2,
            "z500": 1, "msl": 1, "tp06": 3}

VARIABLE_LABELS = {
    "t2m": "2 m temperature",
    "t850": "850 hPa temperature",
    "u10m": "10 m zonal wind",
    "v10m": "10 m meridional wind",
    "z500": "500 hPa geopotential",
    "msl": "Mean sea level pressure",
    "tp06": "Precipitation (6 h accumulation)",
}

# GFS analysis lands ~4 h after its cycle time; verify.py holds back leads whose
# valid time is newer than now-6h and picks them up on a later run. Truth
# sampling has to use the same cutoff or it would request analyses that do not
# exist yet.
TRUTH_LAG_HOURS = 6


def _utcnow() -> datetime:
    """Naive UTC now — init times and valid times are naive UTC throughout."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def truth_available_through(init_time: datetime, leads: list[int], regime: str,
                            now: datetime | None = None) -> datetime | None:
    """Latest valid time whose truth should be obtainable right now, or None.

    The one definition of "how much truth can exist yet", shared by the initial
    export, the incremental top-up, and scripts/check_export.py. Keeping it in
    one place is what lets the gate demand a complete truth series without
    demanding analyses nobody could have: a real-time init's GFS analysis lands
    ~4 h after its cycle, so verify.py:144-145 scores only leads valid at or
    before now-6h, and both sides of this contract must use that same boundary.

    Historic inits verify against ERA5, which is complete for any init old
    enough to be historic at all — so every lead's truth exists immediately.
    """
    valid = [init_time + timedelta(hours=h) for h in leads]
    if regime != "realtime":
        return valid[-1]
    cutoff = (now or _utcnow()) - timedelta(hours=TRUTH_LAG_HOURS)
    avail = [vt for vt in valid if vt <= cutoff]
    return avail[-1] if avail else None


def city_id(name: str) -> str:
    """URL- and filename-safe id for a city: 'São Paulo' -> 'sao-paulo'.

    Deliberately ASCII: these ids become path segments in a fetch() URL and
    keys in the manifest, and a percent-encoded path is a needless source of
    404s across web servers.
    """
    ascii_name = (unicodedata.normalize("NFKD", name)
                  .encode("ascii", "ignore").decode("ascii"))
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", ascii_name.lower())).strip("-")


def cities_payload(cfg: dict) -> list[dict]:
    """`display.cities` with a stable id added, ids checked unique."""
    try:
        raw = cfg["display"]["cities"]
    except KeyError as e:
        raise RuntimeError(
            "config.yaml has no `display.cities` block — the points exporter "
            "samples at exactly those locations, so it is required."
        ) from e
    out = [{"id": city_id(c["name"]), "name": c["name"],
            "lat": float(c["lat"]), "lon": float(c["lon"])} for c in raw]
    dupes = {c["id"] for c in out if sum(o["id"] == c["id"] for o in out) > 1}
    if dupes:
        raise RuntimeError(
            f"config.yaml `display.cities`: ids {sorted(dupes)} collide — two "
            "city names slugify to the same filename."
        )
    return out


# ---------------------------------------------------------------------------
# Bilinear point sampling


def _bilinear_weights(lat: np.ndarray, lon: np.ndarray,
                      plat: np.ndarray, plon: np.ndarray):
    """Corner indices and weights for bilinear sampling at (plat, plon).

    Two things a hand-rolled sampler usually gets wrong, both handled here
    rather than assumed away, because either one produces a plausible-looking
    wrong answer instead of an error:

    - **Latitude orientation.** e2s stores lat descending (90 -> -90). Index
      position is derived from the coordinate values, so an ascending grid
      (which a regridded truth field may well be) samples identically instead
      of being mirrored about the equator.
    - **The longitude seam.** Grids run 0 -> 359.75, so a western city must be
      wrapped (Lima's -77.04 is 282.96 E) and a point past the last column
      interpolates between it and column 0 rather than clamping.

    Latitude clamps at the poles, which is correct — there is no cell beyond
    90 N to interpolate towards.
    """
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    n_lat, n_lon = lat.size, lon.size
    if not (np.all(np.diff(lon) > 0)):
        raise ValueError("longitude coordinate must be strictly ascending")

    idx = np.arange(n_lat, dtype=float)
    if lat[0] > lat[-1]:            # descending (the e2s convention)
        f_lat = np.interp(plat, lat[::-1], idx[::-1])
    elif lat[0] < lat[-1]:
        f_lat = np.interp(plat, lat, idx)
    else:
        raise ValueError("latitude coordinate is not monotonic")
    i0 = np.clip(np.floor(f_lat).astype(int), 0, n_lat - 2)
    w_lat = f_lat - i0

    span = 360.0
    p = np.mod(np.asarray(plon, dtype=float) - lon[0], span) + lon[0]
    f_lon = np.interp(p, np.append(lon, lon[0] + span),
                      np.arange(n_lon + 1, dtype=float))
    j0 = np.floor(f_lon).astype(int)
    w_lon = f_lon - j0
    j0 %= n_lon
    return i0, i0 + 1, w_lat, j0, (j0 + 1) % n_lon, w_lon


def bilinear_sample(field: np.ndarray, lat, lon, plat, plon) -> np.ndarray:
    """Sample `field[..., lat, lon]` at point arrays, returning (..., n_points)."""
    i0, i1, wy, j0, j1, wx = _bilinear_weights(lat, lon, plat, plon)
    f = np.asarray(field)
    return (
        (1 - wy) * (1 - wx) * f[..., i0, j0]
        + (1 - wy) * wx * f[..., i0, j1]
        + wy * (1 - wx) * f[..., i1, j0]
        + wy * wx * f[..., i1, j1]
    )


# ---------------------------------------------------------------------------
# Points export


def _round_series(values, decimals: int) -> list:
    """Round to `decimals`, mapping non-finite to null (NaN is not valid JSON)."""
    return [None if v is None or not np.isfinite(v) else round(float(v), decimals)
            for v in np.asarray(values, dtype=float).tolist()]


def _forecast_inits(cfg: dict) -> dict[datetime, list[str]]:
    """{init_time: [model, ...]} for every forecast zarr still on disk.

    Point data is derived from the zarrs, but once written it stands alone — so
    the exporter is driven by what is present now and never re-derives an init
    it has already emitted. That matters because sweep.py deletes zarrs as soon
    as their scores land; keying the manifest off the zarrs instead would make
    committed point data vanish on the next sweep.
    """
    root = Path(cfg["paths"]["data"]) / "forecasts"
    order = [m["id"] for m in models_payload(cfg)]
    found: dict[datetime, list[str]] = {}
    for zpath in sorted(root.glob("*/*.zarr")):
        try:
            init = datetime.strptime(zpath.stem, INIT_FMT)
        except ValueError:
            print(f"[export] unrecognized zarr name, skipping {zpath}")
            continue
        found.setdefault(init, []).append(zpath.parent.name)
    return {
        init: sorted(models, key=lambda m: (order.index(m) if m in order else 99, m))
        for init, models in sorted(found.items())
    }


def canonical_variable(var: str) -> str:
    """Forecast-variable name as it appears in the emitted JSON (tp -> tp06)."""
    return sources.truth_variable(var)


def _model_series(cfg: dict, model: str, init_time: datetime,
                  union_leads: list[int], cities: list[dict]) -> dict:
    """Sample one model's forecast at every city. {canon_var: (status, array)}.

    `array` is (n_union_leads, n_cities) with NaN where the model has no such
    lead; a status other than `ok` comes with None.
    """
    from .forecast import forecast_path

    zpath = forecast_path(Path(cfg["paths"]["data"]), model, init_time)
    mcfg = cfg["models"][model]
    precip_var = mcfg.get("precip_variable")
    plat = np.array([c["lat"] for c in cities])
    plon = np.array([c["lon"] for c in cities])

    ds = xr.open_zarr(zpath, consolidated=False)
    lead_td = ds["lead_time"].values
    lead_td = lead_td[lead_td > np.timedelta64(0, "h")]
    rows = [union_leads.index(int(lt / np.timedelta64(1, "h"))) for lt in lead_td]

    out: dict[str, tuple] = {}
    for var in mcfg["scored_variables"]:
        canon = canonical_variable(var)
        if var not in ds:
            print(f"[export] {model} {init_time:{INIT_FMT}}: {var} declared in "
                  f"config but absent from {zpath.name}")
            out[canon] = (STATUS_UNAVAILABLE, None, var)
            continue
        field = ds[var].isel(time=0).sel(lead_time=lead_td).values
        if var == precip_var:
            field = np.clip(np.nan_to_num(field), 0.0, None) * 1000.0  # m -> mm
        sampled = bilinear_sample(field, ds["lat"].values, ds["lon"].values,
                                  plat, plon)
        full = np.full((len(union_leads), len(cities)), np.nan)
        full[rows] = sampled
        out[canon] = (STATUS_OK, full, var)
    if precip_var is None:
        # Not "no data for this init" but "this model has no precipitation head".
        out[CANONICAL_PRECIP] = (STATUS_NO_VARIABLE, None, None)
    ds.close()
    return out


def _truth_series(cfg: dict, init_time: datetime, union_leads: list[int],
                  state_vars: list[str], want_precip: bool,
                  cities: list[dict]) -> tuple[dict, str, str | None]:
    """Sample truth at every city. ({canon_var: (status, array)}, label, through).

    Mirrors verify.py exactly on both truth questions: which source serves this
    init's regime, and which valid times are available yet. Real-time precip is
    dropped for the reason verify.py:146-149 drops it — GFS would happily serve
    tp06 from its own +6 h background forecast, which is a model field, not
    truth.
    """
    cutoff_days = cfg["historic_cutoff_days"]
    regime = sources.regime(init_time, cutoff_days)
    realtime = regime == "realtime"
    valid_times = [init_time + timedelta(hours=h) for h in union_leads]

    through_dt = truth_available_through(init_time, union_leads, regime)
    avail = [vt for vt in valid_times if through_dt and vt <= through_dt]
    through = f"{through_dt:{TIME_FMT}}" if through_dt else None

    truth, label = sources.truth_source(init_time, valid_times[-1], cutoff_days)
    out: dict[str, tuple] = {}
    if want_precip and realtime:
        print("[export] real-time precip truth (IMERG Late) not implemented — "
              f"tp06 truth marked {STATUS_TRUTH_PENDING} for "
              f"{init_time:{INIT_FMT}}")
        out[CANONICAL_PRECIP] = (STATUS_TRUTH_PENDING, None)
        want_precip = False
    wanted = list(state_vars) + ([CANONICAL_PRECIP] if want_precip else [])
    if not wanted:
        return out, label, through

    for var in wanted:
        out[var] = (STATUS_OK, np.full((len(union_leads), len(cities)), np.nan))
    if not avail:
        print(f"[export] {init_time:{INIT_FMT}}: no truth valid times available "
              f"yet ({label}) — truth series emitted empty")
        return out, label, through

    print(f"[export] fetching truth ({label}) for {len(avail)} valid times x "
          f"{len(wanted)} vars ...")
    tda = truth(avail, wanted)
    plat = np.array([c["lat"] for c in cities])
    plon = np.array([c["lon"] for c in cities])
    rows = [union_leads.index(int((vt - init_time).total_seconds() // 3600))
            for vt in avail]
    for var in wanted:
        field = tda.sel(variable=var).values          # (time, lat, lon)
        if var == CANONICAL_PRECIP:
            field = np.clip(np.nan_to_num(field), 0.0, None) * 1000.0
        sampled = bilinear_sample(field, tda["lat"].values, tda["lon"].values,
                                  plat, plon)
        out[var][1][rows] = sampled
    return out, label, through


def _provenance(cfg: dict, init_time: datetime, models: list[str]) -> dict:
    """init_source / truth_source / tier for one init, from metrics.parquet.

    The same columns publish.py reads for the page's provenance chips, reduced
    the same way, so the explorer cannot label an init differently from the
    leaderboard. An init whose forecast exists but is not verified yet has no
    rows — fall back to what sources.py would pick for its regime.
    """
    from .verify import metrics_path

    mpath = metrics_path(cfg)
    if mpath.exists():
        df = pd.read_parquet(
            mpath,
            columns=["init_time", "model", "init_source", "truth_source", "tier"])
        sel = df[(df.init_time == pd.Timestamp(init_time)) & df.model.isin(models)]
        if not sel.empty:
            return {"init_source": uniq_label(sel.init_source),
                    "truth_source": uniq_label(sel.truth_source),
                    "tier": uniq_label(sel.tier), "from_metrics": True}
    cutoff = cfg["historic_cutoff_days"]
    realtime = sources.regime(init_time, cutoff) == "realtime"
    return {"init_source": "gfs" if realtime else "era5_arco",
            "truth_source": "gfs_analysis" if realtime else "era5_arco",
            "tier": "provisional" if realtime else "final",
            "from_metrics": False}


def scored_models(cfg: dict, init_time: datetime) -> list[str]:
    """Models with metrics rows for this init — what the leaderboard scored."""
    from .verify import metrics_path

    mpath = metrics_path(cfg)
    if not mpath.exists():
        return []
    df = pd.read_parquet(mpath, columns=["init_time", "model"])
    return sorted(set(df.model[df.init_time == pd.Timestamp(init_time)]))


def expected_models(cfg: dict, init_time: datetime, on_disk: list[str]) -> list[str]:
    """Every model this init ought to have point data for, in display order.

    The union of "scored in metrics.parquet" and "forecast zarr still on disk",
    because either alone understates it: sweep.py deletes a zarr as soon as its
    scores land, and a forecast that has run but not been verified yet has no
    metrics rows. Point values can only come from the zarr — the metrics table
    holds aggregate skill, not the field — so an init whose zarrs are already
    partly purged can never be exported whole, and export_explorer_data refuses
    to publish it rather than emitting a silently partial comparison.
    """
    order = [m["id"] for m in models_payload(cfg)]
    both = set(on_disk) | set(scored_models(cfg, init_time))
    return sorted(both, key=lambda m: (order.index(m) if m in order else 99, m))


def export_points_for_init(cfg: dict, site: Path, init_time: datetime,
                           models: list[str],
                           expected: list[str] | None = None) -> Path:
    """Write docs/data/points/<init>/<city>.json for every configured city."""
    from .forecast import forecast_path

    cities = cities_payload(cfg)
    expected = expected if expected is not None else list(models)
    # Only fetch truth for variables some model here actually forecasts —
    # a truth series nothing can be compared against is a wasted download.
    scored = {v for m in models for v in cfg["models"][m]["scored_variables"]}
    state_vars = [v for v in cfg["verification"]["state_variables"] if v in scored]

    union_leads: set[int] = set()
    for model in models:
        ds = xr.open_zarr(
            forecast_path(Path(cfg["paths"]["data"]), model, init_time),
            consolidated=False)
        lt = ds["lead_time"].values
        union_leads |= {int(x / np.timedelta64(1, "h")) for x in lt
                        if x > np.timedelta64(0, "h")}
        ds.close()
    leads = sorted(union_leads)

    per_model = {}
    for model in models:
        print(f"[export] sampling {model} {init_time:{INIT_FMT}} at "
              f"{len(cities)} cities ...")
        per_model[model] = _model_series(cfg, model, init_time, leads, cities)

    want_precip = any(cfg["models"][m].get("precip_variable") for m in models)
    truth, truth_label, truth_through = _truth_series(
        cfg, init_time, leads, state_vars, want_precip, cities)

    prov = _provenance(cfg, init_time, models)
    if not prov["from_metrics"]:
        prov["truth_source"] = truth_label   # unverified init: label what we read
    outdir = site / "data" / "points" / f"{init_time:{INIT_FMT}}"
    outdir.mkdir(parents=True, exist_ok=True)

    def series(status, arr, ci, var, native=None):
        s: dict = {"status": status}
        if native and native != var:
            s["native_variable"] = native
        s["values"] = (_round_series(arr[:, ci], DECIMALS.get(var, 3))
                       if status == STATUS_OK else None)
        return s

    for ci, city in enumerate(cities):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "init_time": f"{init_time:{TIME_FMT}}",
            "regime": sources.regime(init_time, cfg["historic_cutoff_days"]),
            "tier": prov["tier"],
            "init_source": prov["init_source"],
            "truth_source": prov["truth_source"],
            "truth_valid_through": truth_through,
            "city": city,
            "leads": leads,
            "valid_times": [f"{init_time + timedelta(hours=h):{TIME_FMT}}"
                            for h in leads],
            "truth": {v: series(st, arr, ci, v) for v, (st, arr) in truth.items()},
            # Every model this init should carry, so a consumer (and
            # scripts/check_export.py) can tell a complete comparison from one
            # that lost models to a purged zarr. `models` below must match it.
            "models_expected": expected,
            "models": {
                m: {v: series(st, arr, ci, v, native)
                    for v, (st, arr, native) in vars_.items()}
                for m, vars_ in per_model.items()
            },
        }
        (outdir / f"{city['id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    total = sum(p.stat().st_size for p in outdir.glob("*.json"))
    print(f"[export] points -> {outdir} ({len(cities)} cities, "
          f"{total / 1024:.0f} KiB)")
    return outdir


# ---------------------------------------------------------------------------
# Incremental truth completion


def _city_docs(outdir: Path) -> tuple[list[Path], list[dict]]:
    """Every city file of one init, checked to agree on the shared header.

    Each city file repeats the init's regime, provenance and truth window. A
    half-written export — one city updated, the rest not — is the exact shape a
    crash mid-loop leaves behind, and it renders as thirty cities that disagree
    about which init they describe. Refusing to build a manifest over that is
    cheaper than discovering it in the browser.
    """
    files = sorted(outdir.glob("*.json"))
    docs = [json.loads(p.read_text()) for p in files]
    shared = ("init_time", "regime", "tier", "init_source", "truth_source",
              "truth_valid_through", "leads", "models_expected")

    def sig(d: dict):
        return ({k: d.get(k) for k in shared}, sorted(d["models"]),
                {v: s["status"] for v, s in d["truth"].items()})

    for p, d in zip(files[1:], docs[1:]):
        if sig(d) != sig(docs[0]):
            raise RuntimeError(
                f"{p} disagrees with {files[0].name} on the init header "
                "(regime/provenance/truth window/model set) — that export is "
                "half-written. Re-run `python -m scoreboard.export "
                f"--init {outdir.name} --force`.")
    return files, docs


def topup_truth_for_init(cfg: dict, outdir: Path,
                         now: datetime | None = None) -> bool:
    """Fill in truth that has become available since this init was exported.

    A real-time init is exported the day it runs, when GFS analysis covers only
    its first few leads; the rest arrives over the following five days.
    verify.py:141-155 deliberately re-scores those leads incrementally as their
    truth lands, and the point export has to do the same — otherwise every init
    the live site shows stays truthless past lead ~36 h forever, and once
    sweep.py deletes the zarr the file can never be repaired at all.

    Only the truth series change, so this deliberately does not touch (or need)
    the forecast zarrs: the last leads' truth arrives days after retention may
    have purged them. Returns True if anything was written.
    """
    files, docs = _city_docs(outdir)
    if not files:
        return False
    first = docs[0]
    init_time = datetime.strptime(first["init_time"], TIME_FMT)
    leads = first["leads"]
    through = truth_available_through(init_time, leads, first["regime"], now)
    if through is None:
        return False

    ok_vars = [v for v, s in first["truth"].items() if s["status"] == STATUS_OK]
    in_window = [i for i, h in enumerate(leads)
                 if init_time + timedelta(hours=h) <= through]
    # Retry a hole anywhere inside the window, not just past the recorded
    # boundary: a fetch that failed for one valid time must heal on a later run
    # the way verify.py's does, rather than being frozen in as a permanent gap.
    todo = sorted({i for i in in_window for d in docs for v in ok_vars
                   if d["truth"][v]["values"][i] is None})
    if not todo or not ok_vars:
        return False

    valid = [init_time + timedelta(hours=leads[i]) for i in todo]
    truth, label = sources.truth_source(init_time, valid[-1],
                                        cfg["historic_cutoff_days"])
    if label != first["truth_source"]:
        # The init has crossed historic_cutoff_days since it was exported.
        # Splicing ERA5 into a series whose earlier leads came from GFS
        # analysis would make one line two different measurements, so this is a
        # full re-export (and re-verify), not a top-up.
        raise RuntimeError(
            f"{outdir.name}: truth source is now {label!r} but the export was "
            f"written against {first['truth_source']!r} — the init changed "
            "regime. Re-run `python -m scoreboard.export --init "
            f"{outdir.name} --force`.")

    print(f"[export] {outdir.name}: truth now available through "
          f"{through:{TIME_FMT}} — filling {len(todo)} lead(s) x "
          f"{len(ok_vars)} var(s) from {label}")
    tda = truth(valid, ok_vars)
    plat = np.array([d["city"]["lat"] for d in docs])
    plon = np.array([d["city"]["lon"] for d in docs])
    for var in ok_vars:
        field = tda.sel(variable=var).values          # (time, lat, lon)
        if var == CANONICAL_PRECIP:
            field = np.clip(np.nan_to_num(field), 0.0, None) * 1000.0
        sampled = bilinear_sample(field, tda["lat"].values, tda["lon"].values,
                                  plat, plon)        # (len(todo), n_cities)
        for ci, d in enumerate(docs):
            filled = _round_series(sampled[:, ci], DECIMALS.get(var, 3))
            values = d["truth"][var]["values"]
            for k, i in enumerate(todo):
                values[i] = filled[k]

    stamp = f"{through:{TIME_FMT}}"
    for p, d in zip(files, docs):
        d["truth_valid_through"] = stamp
        p.write_text(json.dumps(d, ensure_ascii=False,
                                separators=(",", ":")) + "\n")
    remaining = sum(1 for d in docs for v in ok_vars for i in in_window
                    if d["truth"][v]["values"][i] is None)
    if remaining:
        print(f"[export] WARNING {outdir.name}: {remaining} truth value(s) "
              "inside the available window are still null after fetching — "
              "check_export.py will flag this")
    return True


# ---------------------------------------------------------------------------
# manifest.json


def _read_init_dir(outdir: Path) -> dict | None:
    """Manifest entry for one already-exported init, read back from its files.

    Reading the emitted JSON rather than the zarrs is what lets the manifest
    keep listing an init after sweep.py has deleted the forecast it came from.
    """
    files, docs = _city_docs(outdir)
    if not files:
        return None
    first = docs[0]
    variables = sorted(
        {v for m in first["models"].values() for v in m} | set(first["truth"]))
    return {
        "init_time": first["init_time"],
        "regime": first["regime"],
        "tier": first["tier"],
        "init_source": first["init_source"],
        "truth_source": first["truth_source"],
        "truth_valid_through": first["truth_valid_through"],
        "models": sorted(first["models"]),
        "models_expected": first["models_expected"],
        "leads": first["leads"],
        "variables": variables,
        "points": {
            "dir": f"points/{outdir.name}",
            "cities": [p.stem for p in files],
            "bytes": sum(p.stat().st_size for p in files),
        },
    }


def write_manifest(cfg: dict, site: Path) -> Path:
    """Write docs/data/manifest.json from whatever is present under points/.

    This is the metadata endpoint of the static design (PLAN_EXPLORER.md §10):
    it says what exists and how to scale it, and the pages fetch the payloads
    themselves. `fields` is reserved for E4 and empty until then.
    """
    root = site / "data"
    order = [m["id"] for m in models_payload(cfg)]
    inits = [e for e in (_read_init_dir(d)
                         for d in sorted((root / "points").glob("*"))
                         if d.is_dir())
             if e is not None]
    for e in inits:
        e["models"] = sorted(e["models"],
                             key=lambda m: (order.index(m) if m in order else 99, m))

    variables = sorted({v for e in inits for v in e["variables"]},
                       key=lambda v: (v == CANONICAL_PRECIP, v))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated": datetime.now(timezone.utc).strftime(TIME_FMT),
        "series_statuses": list(SERIES_STATUSES),
        "leads": sorted({lh for e in inits for lh in e["leads"]}),
        "variables": {
            v: {
                "label": VARIABLE_LABELS.get(v, v),
                "units": UNITS.get(v, ""),
                "kind": "precip" if v == CANONICAL_PRECIP else "state",
                "decimals": DECIMALS.get(v, 3),
                **({"accumulation_hours": 6} if v == CANONICAL_PRECIP else {}),
            }
            for v in variables
        },
        "cities": cities_payload(cfg),
        "inits": inits,
        # Reserved for E4: {init: {model: {var: {lead: {scale, missing}}}}}.
        "fields": {},
    }
    out = root / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    print(f"[export] manifest -> {out} ({len(inits)} inits, "
          f"{len(payload['variables'])} variables)")
    return out


def prune_points(cfg: dict, site: Path, dry_run: bool = False) -> int:
    """Drop point directories older than `retention_days`.

    Age is the directory's mtime, not its init time — sweep.py's convention, and
    for the same reason: a historic backfill exported today must survive the
    next prune even though its init is from 2023.
    """
    import shutil

    days = cfg.get("retention_days", 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for d in sorted((site / "data" / "points").glob("*")):
        if not d.is_dir():
            continue
        if datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc) >= cutoff:
            continue
        print(f"[export] {'would prune' if dry_run else 'pruning'} {d}")
        if not dry_run:
            shutil.rmtree(d)
        n += 1
    return n


def export_explorer_data(cfg: dict, inits: list[datetime] | None = None,
                         force: bool = False, prune: bool = True) -> Path:
    """models.json + points/** + manifest.json. Returns the manifest path."""
    site = Path(cfg["paths"]["site"])
    print(f"[export] models.json -> {write_models_json(cfg, site)}")

    points_root = site / "data" / "points"
    exported = {d.name for d in points_root.glob("*") if d.is_dir()}
    available = _forecast_inits(cfg)
    if inits is None:
        todo = available
        topup = sorted(exported)
    else:
        # An init with no zarr left is still a legitimate target: its truth can
        # be completed from the emitted files alone.
        unknown = [i for i in inits
                   if i not in available and f"{i:{INIT_FMT}}" not in exported]
        if unknown:
            raise SystemExit(
                "[export] no forecast zarr and no exported points for "
                + ", ".join(f"{i:{INIT_FMT}}" for i in unknown)
                + " — available: "
                + (", ".join(f"{i:{INIT_FMT}}" for i in available) or "none")
            )
        todo = {i: available[i] for i in inits if i in available}
        topup = [f"{i:{INIT_FMT}}" for i in inits]
    for init, models in todo.items():
        outdir = points_root / f"{init:{INIT_FMT}}"
        expected = expected_models(cfg, init, models)
        gone = [m for m in expected if m not in models]
        if gone:
            # Never write point data for an init that cannot be made whole —
            # not even under --force, which would otherwise overwrite a
            # complete export with a degraded one after a sweep.
            print(
                f"[export] NOT exporting {init:{INIT_FMT}} from the zarrs: "
                f"metrics.parquet scored {len(expected)} models for it but only "
                f"{len(models)} still have a forecast zarr — "
                f"{', '.join(gone)} were purged by sweep.py, and point values "
                "live in the zarr, not the metrics table. A partial init would "
                "publish a comparison that silently drops models. To include "
                "it, re-run those forecasts (`python -m scoreboard.forecast "
                f"--model <m> --init {init:{INIT_FMT}}`) and export again.")
            continue
        _, docs = _city_docs(outdir) if outdir.is_dir() else ([], [])
        if docs and not force:
            # An already-exported init is not re-derived — its point data stands
            # alone once written, and the zarrs it came from may be gone. The
            # one exception is a model that finished after the export ran: all
            # zarrs are still present, so folding it in loses nothing.
            new = sorted(set(models) - set(docs[0]["models"]))
            if not new:
                continue
            print(f"[export] {init:{INIT_FMT}}: {', '.join(new)} appeared since "
                  "the last export — re-deriving this init from the zarrs")
        export_points_for_init(cfg, site, init, models, expected)

    # Truth arrives after the forecast does, so completing it is a separate pass
    # over everything already on disk — including inits whose zarrs are long
    # gone, which the loop above cannot touch at all.
    failed = []
    for name in sorted(set(topup) | {f"{i:{INIT_FMT}}" for i in todo}):
        outdir = points_root / name
        if not outdir.is_dir():
            continue
        try:
            topup_truth_for_init(cfg, outdir)
        except Exception as e:  # noqa: BLE001 — one bad init must not block the rest
            print(f"[export] FAILED to complete truth for {outdir.name}: {e}")
            failed.append(outdir.name)

    if prune:
        prune_points(cfg, site)
    manifest = write_manifest(cfg, site)
    if failed:
        # The manifest is written first so it still describes what is on disk;
        # then the run fails, because the emitted data is knowably incomplete
        # and check_export.py is about to say so.
        raise SystemExit(
            "[export] truth completion failed for " + ", ".join(failed))
    return manifest


def main():
    """`python -m scoreboard.export` — regenerate docs/data/ from the zarrs."""
    import argparse

    import yaml

    p = argparse.ArgumentParser(
        description="Write docs/data/{models,manifest}.json and points/**")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--init", nargs="*", default=None, metavar="YYYY-MM-DDTHH",
                   help="only these inits (default: every forecast zarr on disk)")
    p.add_argument("--force", action="store_true",
                   help="re-export inits whose points directory already exists")
    p.add_argument("--no-prune", action="store_true",
                   help="keep point directories past retention_days")
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    inits = ([datetime.strptime(s, INIT_FMT) for s in a.init]
             if a.init else None)
    export_explorer_data(cfg, inits, force=a.force, prune=not a.no_prune)


if __name__ == "__main__":
    main()
