"""Export gridded `t2m` fields for map.html as quantized single-channel PNGs.

EXPLORER_STEPS.md E4, PLAN_EXPLORER.md §4 and §5a. `t2m` only: all ten models
carry it, truth exists in both regimes, and uint8 quantization is benign — so
the export -> decode -> render chain gets proven before precipitation's
log-scale and categorical-error complications arrive.

For each init, model and lead in `display.map_leads` two PNGs are written under
`docs/data/fields/<init>/<model>/t2m/`:

    f<lead>.png   the forecast field
    e<lead>.png   model - truth

**The error field is differenced in float, at native resolution, before either
side is quantized.** This is the single decision the whole module exists to
enforce. uint8 across a 220-320 K `t2m` range is 0.39 K per level, so a browser
that subtracted two quantized forecast fields would carry ~0.55 K of pure
encoding noise — the same order as the 24 h error being displayed, which would
be buried in its own artifacts (PLAN_EXPLORER.md §4). Differencing here, where
the float data already exists, lets the error field spend all 254 levels on a
±few K range instead of ±50 K.

Three encoding choices, each with a consequence a renderer depends on:

- **One reserved byte for missing.** Value 0 means "no data" — a lead whose
  truth has not landed, or a cell where either side is non-finite. Data occupies
  1..255, so the quantization has 254 intervals rather than 255. The reserved
  value is published in the manifest so map.html can draw it transparent instead
  of painting it as the bottom of the colour ramp.
- **Error scales are exactly symmetric about zero**, [-m, +m]. A diverging ramp
  whose neutral point drifts off zero misrepresents the sign of a bias, and the
  cheapest way to make that impossible is to never emit an asymmetric error
  scale in the first place.
- **One scale per (variable, lead, kind), shared across models.** Per-model
  scales would quantize each field slightly more finely, and would render the
  same temperature as two different colours in E6's side-by-side comparison.
  For a comparison site that trade is the wrong way round: the precision given
  up is ~0.1 K, and what is bought is that two panes mean the same thing.

`docs/data/fields/<init>/index.json` is the durable per-init record of all of
this, and `manifest.json`'s reserved `fields` section is aggregated from those
sidecars by `export.write_manifest`. Keeping the sidecar authoritative is what
lets the two exporters run in either order — a points-only export cannot wipe
the field scales, and a pruned init disappears from the manifest by itself.

Unlike point values, fields are **not** captured to a durable store: a single
init at 0.25° is ~584 MB per model. So this reads the forecast zarrs directly
and can only ever export inits whose zarrs sweep.py has not yet purged.

Run standalone:

    conda run -n earth2 python -m scoreboard.fields [--init 2026-07-28T00] [--latest]
"""

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import xarray as xr
from scipy.ndimage import uniform_filter

from . import sources
from .export import (INIT_FMT, SCHEMA_VERSION, STATUS_OK, STATUS_TRUTH_PENDING,
                     TIME_FMT, UNITS, _provenance, bilinear_sample,
                     models_payload, scored_models, truth_available_through)
from .forecast import forecast_path

# t2m only, deliberately (PLAN_EXPLORER.md §5a). z500 and msl would work through
# this exact code path and cost almost nothing, but they are a later step's
# scope; precipitation cannot use it at all — linear uint8 across 0-100 mm/6 h
# erases the drizzle band the 1 mm CSI threshold depends on, and pointwise
# `model - truth` is the wrong error notion for it (§4).
SUPPORTED_VARIABLES = ("t2m",)

# uint8 layout. 0 is reserved so a renderer can distinguish "no data" from "the
# coldest value on the map"; the 254 intervals between MIN_BYTE and MAX_BYTE
# carry the field.
MISSING_BYTE = 0
MIN_BYTE = 1
MAX_BYTE = 255
LEVELS = MAX_BYTE - MIN_BYTE          # intervals, not levels-of-value

# Scales are rounded outward to this many decimals before being written, so the
# manifest stays readable and — more importantly — so the number the encoder
# used is bit-identical to the number the manifest publishes. Rounding *outward*
# keeps every sample inside the scale, which is what makes the half-step error
# bound in scripts/check_fields.py hold rather than nearly hold.
SCALE_DECIMALS = 3
MIN_SPAN = 10.0 ** -SCALE_DECIMALS    # a constant field still needs a range


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def map_settings(cfg: dict) -> tuple[list[str], list[int], float]:
    """(variables, leads, resolution) from config.yaml's display block."""
    try:
        d = cfg["display"]
        variables = list(d["map_variables"])
        leads = sorted(int(h) for h in d["map_leads"])
        res = float(d["map_resolution_deg"])
    except KeyError as e:
        raise RuntimeError(
            "config.yaml `display` needs map_variables, map_leads and "
            "map_resolution_deg — the field exporter is driven entirely by them."
        ) from e
    unsupported = [v for v in variables if v not in SUPPORTED_VARIABLES]
    if unsupported:
        raise RuntimeError(
            f"display.map_variables has {unsupported}, but this exporter "
            f"supports only {list(SUPPORTED_VARIABLES)}. Precipitation needs log "
            "quantization and a categorical error rendering, not this path "
            "(PLAN_EXPLORER.md §4); z500/msl are a later step.")
    if not variables or not leads:
        raise RuntimeError("display.map_variables and display.map_leads must "
                           "both be non-empty")
    return variables, leads, res


# ---------------------------------------------------------------------------
# Regridding


def target_grid(res: float) -> tuple[np.ndarray, np.ndarray]:
    """The display grid: lat 90 -> -90 inclusive, lon 0 -> 360 exclusive.

    Pole-inclusive latitude and seam-exclusive longitude is the convention the
    0.25° source zarrs already use (721 x 1440), and it is what makes
    PLAN_EXPLORER.md §4's cell counts come out (181 x 360 at 1.0°,
    361 x 720 at 0.5°). Latitude stays *descending* so nothing between here and
    the canvas has to flip it — an accidental flip renders a completely
    convincing picture of the wrong hemisphere.
    """
    n_lat_intervals = 180.0 / res
    n_lon = 360.0 / res
    if abs(n_lat_intervals - round(n_lat_intervals)) > 1e-9 or \
            abs(n_lon - round(n_lon)) > 1e-9:
        raise RuntimeError(
            f"map_resolution_deg {res} does not divide the globe evenly — the "
            "grid would not close at the poles or across the antimeridian.")
    lat = np.linspace(90.0, -90.0, int(round(n_lat_intervals)) + 1)
    lon = np.arange(int(round(n_lon)), dtype=float) * res
    return lat, lon


def _box_kernel(src_step: float, tgt_step: float) -> int:
    """Odd smoothing width covering one target cell, or 1 if not downsampling.

    Odd rather than the exact ratio: `uniform_filter` centres an even-width
    window half a cell off, which would shift the whole field by half a source
    cell in a way that looks like nothing at all on a map and is simply wrong.
    """
    f = tgt_step / src_step
    if f <= 1.0:
        return 1
    k = int(round(f))
    return k + 1 if k % 2 == 0 else k


def regrid(field: np.ndarray, lat: np.ndarray, lon: np.ndarray,
           tgt_lat: np.ndarray, tgt_lon: np.ndarray) -> np.ndarray:
    """Area-smooth then sample `field` onto (tgt_lat, tgt_lon).

    Plain subsampling from 0.25° to 1.0° would throw away fifteen cells of
    sixteen and alias whatever structure sat between them; the box filter is
    what makes the coarse field the *average* of the fine one rather than an
    arbitrary sample of it. Longitude wraps, latitude does not — there is no
    cell beyond the pole to average towards.

    Non-finite cells are excluded from the average rather than poisoning their
    whole neighbourhood, and stay non-finite only where nothing finite was in
    range. They become the reserved missing byte at quantization time.
    """
    f = np.asarray(field, dtype=np.float64)
    ky = _box_kernel(abs(float(lat[1] - lat[0])), abs(float(tgt_lat[1] - tgt_lat[0])))
    kx = _box_kernel(abs(float(lon[1] - lon[0])), abs(float(tgt_lon[1] - tgt_lon[0])))
    if ky > 1 or kx > 1:
        mask = np.isfinite(f)
        mode = ("nearest", "wrap")          # lat clamps at the pole, lon wraps
        num = uniform_filter(np.where(mask, f, 0.0), size=(ky, kx), mode=mode)
        if mask.all():
            f = num
        else:
            den = uniform_filter(mask.astype(np.float64), size=(ky, kx), mode=mode)
            with np.errstate(invalid="ignore", divide="ignore"):
                f = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)

    plat = np.repeat(tgt_lat, tgt_lon.size)
    plon = np.tile(tgt_lon, tgt_lat.size)
    return bilinear_sample(f, lat, lon, plat, plon).reshape(tgt_lat.size,
                                                            tgt_lon.size)


# ---------------------------------------------------------------------------
# Quantization


def _round_out(lo: float, hi: float) -> tuple[float, float]:
    """Widen [lo, hi] to SCALE_DECIMALS places so no sample falls outside it."""
    q = 10.0 ** SCALE_DECIMALS
    lo = math.floor(lo * q) / q
    hi = math.ceil(hi * q) / q
    if hi - lo < MIN_SPAN:              # constant field: a zero span has no step
        mid = 0.5 * (lo + hi)
        lo, hi = mid - 0.5 * MIN_SPAN, mid + 0.5 * MIN_SPAN
    return lo, hi


def forecast_scale(fields: list[np.ndarray]) -> list[float]:
    """[min, max] covering every finite value in `fields`."""
    finite = [f[np.isfinite(f)] for f in fields]
    finite = [a for a in finite if a.size]
    if not finite:
        return [0.0, MIN_SPAN]
    lo = min(float(a.min()) for a in finite)
    hi = max(float(a.max()) for a in finite)
    return list(_round_out(lo, hi))


def error_scale(fields: list[np.ndarray]) -> list[float]:
    """[-m, +m] covering every finite value — symmetric about zero by construction.

    Deriving the magnitude and negating it, rather than rounding min and max
    independently, is what guarantees the diverging ramp's neutral point lands
    exactly on zero. Floating point negation is exact, so `lo == -hi` holds as
    an equality the gate can assert rather than approximate.
    """
    finite = [np.abs(f[np.isfinite(f)]) for f in fields]
    finite = [a for a in finite if a.size]
    m = max((float(a.max()) for a in finite), default=0.0)
    q = 10.0 ** SCALE_DECIMALS
    m = max(math.ceil(m * q) / q, MIN_SPAN)
    return [-m, m]


def quantize(field: np.ndarray, scale) -> np.ndarray:
    """Float field -> uint8, MISSING_BYTE where the input is not finite."""
    lo, hi = float(scale[0]), float(scale[1])
    step = (hi - lo) / LEVELS
    out = np.full(field.shape, MISSING_BYTE, dtype=np.uint8)
    m = np.isfinite(field)
    if m.any():
        v = np.clip(np.asarray(field, dtype=np.float64)[m], lo, hi)
        out[m] = (np.rint((v - lo) / step) + MIN_BYTE).astype(np.uint8)
    return out


def dequantize(codes: np.ndarray, scale) -> np.ndarray:
    """uint8 -> float, NaN at MISSING_BYTE. The exact inverse map map.html uses."""
    lo, hi = float(scale[0]), float(scale[1])
    step = (hi - lo) / LEVELS
    out = np.full(codes.shape, np.nan, dtype=np.float64)
    m = codes != MISSING_BYTE
    out[m] = lo + (codes[m].astype(np.float64) - MIN_BYTE) * step
    return out


def quantization_step(scale) -> float:
    return (float(scale[1]) - float(scale[0])) / LEVELS


def write_png(path: Path, codes: np.ndarray) -> int:
    """Write a single-channel 8-bit PNG. Returns bytes written."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    # A 2-D uint8 array already implies mode "L"; passing it explicitly is
    # deprecated in Pillow 11.
    im = Image.fromarray(np.ascontiguousarray(codes, dtype=np.uint8))
    if im.mode != "L":
        raise RuntimeError(f"expected an 8-bit greyscale image, got {im.mode}")
    im.save(path, format="PNG", optimize=True)
    return path.stat().st_size


def read_png(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        if im.mode != "L":
            raise RuntimeError(f"{path}: expected 8-bit greyscale, got {im.mode}")
        return np.asarray(im, dtype=np.uint8)


# ---------------------------------------------------------------------------
# Source arrays
#
# Split out from the writer so scripts/check_fields.py can regenerate exactly
# what was encoded and compare against the decoded PNG. A gate that recomputed
# the source a second, subtly different way would be testing its own copy of the
# pipeline rather than the pipeline.


def available_models(cfg: dict, init_time: datetime) -> list[str]:
    """Models whose forecast zarr for this init is still on disk, in draw order.

    Fields, unlike point values, cannot be captured to a durable store — one
    init is ~584 MB per model — so this is a hard floor, not a repairable gap.
    """
    order = [m["id"] for m in models_payload(cfg)]
    data = Path(cfg["paths"]["data"])
    have = [m for m in cfg["models"]
            if forecast_path(data, m, init_time).exists()]
    return sorted(have, key=lambda m: (order.index(m) if m in order else 99, m))


def zarr_inits(cfg: dict) -> list[datetime]:
    """Every init time with at least one surviving forecast zarr, oldest first."""
    root = Path(cfg["paths"]["data"]) / "forecasts"
    out = set()
    for zpath in root.glob("*/*.zarr"):
        try:
            out.add(datetime.strptime(zpath.stem, INIT_FMT))
        except ValueError:
            print(f"[fields] unrecognized zarr name, skipping {zpath}")
    return sorted(out)


def fetch_truth(cfg: dict, init_time: datetime, leads: list[int],
                variables: list[str],
                now: datetime | None = None) -> tuple[object, list[int], str,
                                                      list[int]]:
    """Fetch truth once per init. (DataArray|None, ready leads, label, pending).

    Mirrors verify.py and export.py on both truth questions — which source
    serves this init's regime, and which valid times can exist yet — so the map
    and the leaderboard cannot disagree about what "error" means for an init.
    A real-time init exported the day it runs simply has no truth past its first
    couple of leads; those leads' error fields are marked pending rather than
    invented.

    Returned on its native grid rather than a model's, because the models do not
    agree on one: Aurora stores 720 latitudes (90 -> -89.75, no south pole row)
    where everything else stores 721. Aligning once against whichever model
    happened to be first would have silently broadcast-failed, or worse, matched.
    `truth_on_grid` does the alignment per model.
    """
    regime = sources.regime(init_time, cfg["historic_cutoff_days"])
    through = truth_available_through(init_time, leads, regime, now)
    ready = [h for h in leads
             if through is not None and init_time + timedelta(hours=h) <= through]
    pending = [h for h in leads if h not in ready]

    if not ready:
        _, label = sources.truth_source(init_time,
                                        init_time + timedelta(hours=leads[-1]),
                                        cfg["historic_cutoff_days"])
        print(f"[fields] {init_time:{INIT_FMT}}: no truth valid times available "
              f"yet ({label}) — every error field marked {STATUS_TRUTH_PENDING}")
        return None, ready, label, pending
    tda, label = fetch_truth_at(cfg, init_time, ready, variables)
    return tda, ready, label, pending


def fetch_truth_at(cfg: dict, init_time: datetime, leads: list[int],
                   variables: list[str]) -> tuple[object, str]:
    """Truth for exactly these leads, with no availability filtering.

    Split out for scripts/check_fields.py, which must reproduce the truth an
    export *used* — the leads its manifest marks `ok` — rather than whatever the
    clock permits by the time the gate runs, which may be more.
    """
    valid = [init_time + timedelta(hours=h) for h in leads]
    truth_vars = [sources.truth_variable(v) for v in variables]
    truth, label = sources.truth_source(init_time, valid[-1],
                                        cfg["historic_cutoff_days"])
    print(f"[fields] fetching truth ({label}) for {len(valid)} valid time(s) x "
          f"{len(truth_vars)} var(s) ...")
    return truth(valid, truth_vars), label


def truth_on_grid(tda, ready: list[int], variables: list[str],
                  lat: np.ndarray, lon: np.ndarray) -> dict:
    """{(var, lead): array} on one model's grid, or {} when there is no truth.

    Nearest-neighbour reindex with the same 1e-5 tolerance verify.py uses before
    it differences: tight enough that a genuinely different grid raises rather
    than being silently snapped onto this one.
    """
    if tda is None:
        return {}
    aligned = tda.reindex(lat=lat, lon=lon, method="nearest", tolerance=1e-5)
    out = {}
    for i, h in enumerate(ready):
        for var in variables:
            out[(var, h)] = np.asarray(
                aligned.isel(time=i).sel(variable=sources.truth_variable(var))
                .values, dtype=np.float64)
    return out


def source_arrays(cfg: dict, init_time: datetime, model: str,
                  variables: list[str], leads: list[int], res: float,
                  truth_da, ready: list[int]) -> dict:
    """{(var, kind, lead): regridded float array} for one model.

    `kind` is "forecast" or "error". The error array is `model - truth`
    differenced at native 0.25° resolution and regridded afterwards — see the
    module docstring. (Regridding is linear, so differencing after would give
    the same numbers; doing it before keeps the code's claim and the data's
    provenance the same statement.)
    """
    tgt_lat, tgt_lon = target_grid(res)
    zpath = forecast_path(Path(cfg["paths"]["data"]), model, init_time)
    if not zpath.exists():
        raise FileNotFoundError(f"No forecast at {zpath}")
    out: dict = {}
    ds = xr.open_zarr(zpath)
    try:
        lat, lon = ds["lat"].values, ds["lon"].values
        truth = truth_on_grid(truth_da, ready, variables, lat, lon)
        have = {int(td / np.timedelta64(1, "h")) for td in ds["lead_time"].values}
        missing = [h for h in leads if h not in have]
        if missing:
            raise RuntimeError(
                f"{zpath} has no lead_time for {missing} h — display.map_leads "
                "asks for leads this forecast does not carry.")
        for var in variables:
            if var not in ds:
                print(f"[fields] {model} {init_time:{INIT_FMT}}: {var} declared "
                      "in display.map_variables but absent from the zarr")
                continue
            for h in leads:
                native = np.asarray(
                    ds[var].isel(time=0)
                    .sel(lead_time=np.timedelta64(h, "h")).values.squeeze(),
                    dtype=np.float64)
                out[(var, "forecast", h)] = regrid(native, lat, lon,
                                                   tgt_lat, tgt_lon)
                t = truth.get((var, h))
                if t is not None:
                    out[(var, "error", h)] = regrid(native - t, lat, lon,
                                                    tgt_lat, tgt_lon)
    finally:
        ds.close()
    return out


# ---------------------------------------------------------------------------
# Export


def init_dir(site: Path, init_time: datetime) -> Path:
    return site / "data" / "fields" / f"{init_time:{INIT_FMT}}"


def export_fields_for_init(cfg: dict, site: Path, init_time: datetime,
                           now: datetime | None = None) -> dict | None:
    """Write every PNG plus the index.json sidecar for one init.

    Returns the sidecar payload, or None when no model's zarr survives.
    """
    variables, leads, res = map_settings(cfg)
    models = available_models(cfg, init_time)
    if not models:
        print(f"[fields] {init_time:{INIT_FMT}}: no forecast zarr survives — "
              "fields cannot be exported for this init")
        return None

    order = [m["id"] for m in models_payload(cfg)]
    expected = sorted(set(models) | set(scored_models(cfg, init_time)),
                      key=lambda m: (order.index(m) if m in order else 99, m))
    gone = [m for m in expected if m not in models]
    if gone:
        # Not fatal, and deliberately unlike export.py's points rule. A points
        # file claims to be a comparison of every model that ran, so one missing
        # model makes the whole file a lie; the map draws one model at a time
        # from a selector built out of `models`, so a smaller selector is an
        # accurate statement about what is on disk. The manifest carries both
        # lists so the page can say so.
        print(f"[fields] {init_time:{INIT_FMT}}: {', '.join(gone)} scored in "
              "metrics.parquet but their forecast zarrs are gone — the map will "
              f"offer {len(models)} of {len(expected)} models for this init")

    tgt_lat, tgt_lon = target_grid(res)
    # One truth fetch per init — it is the same field for every model — but
    # aligned separately per model, since they do not share a latitude grid.
    truth_da, ready, truth_label, pending = fetch_truth(
        cfg, init_time, leads, variables, now)

    arrays: dict = {}
    for model in models:
        print(f"[fields] regridding {model} {init_time:{INIT_FMT}} to {res}° ...")
        arrays[model] = source_arrays(cfg, init_time, model, variables, leads,
                                      res, truth_da, ready)

    # Scales: one per (variable, kind, lead), over every model at that lead.
    scales: dict = {}
    for var in variables:
        for h in leads:
            fc = [a[(var, "forecast", h)] for a in arrays.values()
                  if (var, "forecast", h) in a]
            if fc:
                scales[(var, "forecast", h)] = forecast_scale(fc)
            er = [a[(var, "error", h)] for a in arrays.values()
                  if (var, "error", h) in a]
            if er:
                scales[(var, "error", h)] = error_scale(er)

    outdir = init_dir(site, init_time)
    outdir.mkdir(parents=True, exist_ok=True)
    n_png = 0
    for model in models:
        for (var, kind, h), field in sorted(arrays[model].items()):
            path = outdir / model / var / f"{'f' if kind == 'forecast' else 'e'}{h}.png"
            write_png(path, quantize(field, scales[(var, kind, h)]))
            n_png += 1

    prov = _provenance(cfg, init_time, models)
    if not prov["from_metrics"]:
        prov["truth_source"] = truth_label
    payload = {
        "schema_version": SCHEMA_VERSION,
        "init_time": f"{init_time:{TIME_FMT}}",
        "generated": datetime.now(timezone.utc).strftime(TIME_FMT),
        "regime": sources.regime(init_time, cfg["historic_cutoff_days"]),
        "tier": prov["tier"],
        "init_source": prov["init_source"],
        "truth_source": prov["truth_source"],
        "dir": f"fields/{outdir.name}",
        "grid": {
            "resolution_deg": res,
            "width": int(tgt_lon.size),
            "height": int(tgt_lat.size),
            # Explicit origin and step, not just a bounding box: the one bug
            # that renders a completely convincing picture of the wrong thing is
            # a latitude flip, and a renderer that reads a negative lat_step
            # cannot guess the orientation wrong (EXPLORER_STEPS.md E5).
            "lat_start": float(tgt_lat[0]),
            "lat_step": float(tgt_lat[1] - tgt_lat[0]),
            "lon_start": float(tgt_lon[0]),
            "lon_step": float(tgt_lon[1] - tgt_lon[0]),
        },
        "encoding": {
            "dtype": "uint8",
            "missing": MISSING_BYTE,
            "min_byte": MIN_BYTE,
            "max_byte": MAX_BYTE,
            "levels": LEVELS,
            # value = min + (byte - min_byte) * (max - min) / levels, and
            # byte == missing means no data — draw it transparent.
            "decode": "value = scale[0] + (byte - min_byte) * "
                      "(scale[1] - scale[0]) / levels",
        },
        "leads": leads,
        "models": models,
        "models_expected": expected,
        "variables": {},
        # An error field for a lead whose truth has not landed would be a file
        # of nothing but the reserved missing byte, so it is not written at all
        # and the manifest marks that lead `truth_pending` instead. `png_count`
        # is therefore models x vars x (leads + leads_with_truth), and equals
        # EXPLORER_STEPS.md E4's models x leads x 2 exactly once truth is
        # complete — which for a historic init is immediately. Both numbers are
        # published so the difference is arithmetic rather than a discrepancy.
        "png_count": n_png,
        "png_count_when_complete": len(models) * len(variables) * len(leads) * 2,
        "bytes": 0,
    }
    for var in variables:
        entry = {
            "units": UNITS.get(var, ""),
            "forecast": {
                "file": "f{lead}.png",
                "scales": {str(h): scales[(var, "forecast", h)] for h in leads
                           if (var, "forecast", h) in scales},
            },
            "error": {
                "file": "e{lead}.png",
                "symmetric": True,
                "definition": "model - truth",
                "truth_source": prov["truth_source"],
                "scales": {str(h): scales[(var, "error", h)] for h in leads
                           if (var, "error", h) in scales},
                "status": {
                    str(h): (STATUS_TRUTH_PENDING if h in pending else STATUS_OK)
                    for h in leads
                },
            },
        }
        payload["variables"][var] = entry
    payload["bytes"] = sum(p.stat().st_size for p in outdir.rglob("*.png"))
    (outdir / "index.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False) + "\n")

    n_pending = len(pending) * len(variables) * len(models)
    print(f"[fields] {init_time:{INIT_FMT}}: {n_png} PNG(s) of "
          f"{payload['png_count_when_complete']} "
          f"({len(models)} model(s) x {len(leads)} lead(s) x {len(variables)} "
          f"var x 2 kinds, minus {n_pending} error field(s) still awaiting "
          f"truth) -> {outdir} ({payload['bytes'] / 1024:.0f} KiB, "
          f"{payload['bytes'] / len(models) / 1024:.0f} KiB per model)")
    return payload


def fields_section(cfg: dict, site: Path) -> dict:
    """manifest.json's `fields` object, aggregated from the on-disk sidecars.

    `export.write_manifest` calls this, so the manifest is always a description
    of what is actually in docs/data/fields/ — a points-only export cannot drop
    the field scales, and a pruned init leaves the manifest by itself.
    """
    root = site / "data" / "fields"
    out = {}
    for d in sorted(root.glob("*")):
        idx = d / "index.json"
        if not d.is_dir() or not idx.exists():
            continue
        out[d.name] = json.loads(idx.read_text())
    return out


def prune_fields(cfg: dict, site: Path, dry_run: bool = False) -> int:
    """Drop field directories older than `retention_days`.

    Age is the directory's mtime, matching sweep.py's and prune_points'
    convention: a historic backfill exported today must survive the next prune
    even though its init is from 2023.
    """
    import shutil

    days = cfg.get("retention_days", 30)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for d in sorted((site / "data" / "fields").glob("*")):
        if not d.is_dir():
            continue
        if datetime.fromtimestamp(d.stat().st_mtime, tz=timezone.utc) >= cutoff:
            continue
        print(f"[fields] {'would prune' if dry_run else 'pruning'} {d}")
        if not dry_run:
            shutil.rmtree(d)
        n += 1
    return n


def export_fields(cfg: dict, inits: list[datetime] | None = None,
                  latest: bool = False, prune: bool = True,
                  now: datetime | None = None) -> dict:
    """Export every requested init, prune, and rewrite manifest.json."""
    from .export import write_manifest

    site = Path(cfg["paths"]["site"])
    todo = inits if inits is not None else zarr_inits(cfg)
    if latest and todo:
        todo = [max(todo)]
    if not todo:
        print("[fields] no forecast zarrs on disk — nothing to export. Fields "
              "come from the zarrs directly (they are far too large to keep a "
              "durable copy of), so sweep.py's retention is a hard floor here.")
    written = {}
    for init in todo:
        payload = export_fields_for_init(cfg, site, init, now)
        if payload is not None:
            written[f"{init:{INIT_FMT}}"] = payload
    if prune:
        prune_fields(cfg, site)
    write_manifest(cfg, site)
    return written


def main():
    """`python -m scoreboard.fields` — regenerate docs/data/fields/ from zarrs."""
    import argparse

    import yaml

    p = argparse.ArgumentParser(
        description="Export quantized t2m field PNGs for docs/map.html")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--init", nargs="*", default=None, metavar="YYYY-MM-DDTHH",
                   help="only these inits (default: every init with a zarr)")
    p.add_argument("--latest", action="store_true",
                   help="only the newest init (PLAN_EXPLORER.md §4's "
                        "recommendation once the repo-growth choice is made)")
    p.add_argument("--no-prune", action="store_true",
                   help="keep field directories past retention_days")
    p.add_argument("--now", default=None, metavar="YYYY-MM-DDTHH",
                   help="pretend it is this UTC time when deciding which truth "
                        "valid times exist yet (testing/rehearsal)")
    a = p.parse_args()
    cfg = yaml.safe_load(Path(a.config).read_text())
    inits = ([datetime.strptime(s, INIT_FMT) for s in a.init]
             if a.init else None)
    now = datetime.strptime(a.now, INIT_FMT) if a.now else None
    export_fields(cfg, inits, latest=a.latest, prune=not a.no_prune, now=now)


if __name__ == "__main__":
    main()
