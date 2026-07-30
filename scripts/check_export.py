#!/usr/bin/env python
"""Validate everything scoreboard/export.py writes under docs/data/.

    conda run -n earth2 python scripts/check_export.py

Five layers, because a schema alone would not make the export trustworthy:

1. **Schema.** Every emitted file is validated against
   `docs/data/schema/*.schema.json`. That is the contract E3's page and E4's
   exporter code against.
2. **Semantics the schema cannot express.** Series lengths matching `leads`,
   valid times matching init+lead, *every* city file agreeing with the manifest,
   model coverage matching `models_expected`, the truth series being as complete
   as the clock allows, and — the point of E2 — that the two
   precipitation-absence cases are present where they belong and are never
   collapsed to a null series.
3. **The sampler.** An analytic bilinear check on synthetic latitude/longitude
   fields. A lat flip or a longitude-seam tear yields plausible numbers rather
   than an error, so it has to be tested against a known answer instead of
   eyeballed. London at 0.13 W sits on the 0/360 seam and is the case that
   catches a naive clamp.
4. **The validator itself.** A self-test feeds deliberately broken documents
   through and requires each to be rejected. Without it a check that silently
   accepted everything would look exactly like a passing gate.
5. **The semantic layer itself.** Layer 4 only proves the *schema* rejects
   malformed documents, and the failures that actually happen are schema-valid:
   an export that dies halfway leaves most cities correct and a few wrong, and a
   manifest rebuilt afterwards agrees with whatever is on disk. So layer 2 gets
   the same treatment — a synthetic two-city tree with its own metrics.parquet,
   corrupted one way at a time, each corruption required to be caught.

Nothing here is conditional on what happens to be present. In particular the
provenance cross-check **fails** when `data/metrics.parquet` is missing rather
than skipping itself: the tier and the source labels are precisely what the
exporter guesses when it has no metrics rows, so a gate that waived them on a
fresh clone would bless a manifest that lies about the regime.

`jsonschema` is not in the earth2 env, so layer 1 runs on the small validator
below (draft 2020-12 subset) unless the real library is importable, in which
case that is preferred. Either way layer 4 exercises whichever one is active.
Any schema keyword the local validator does not implement is a hard error, so
it can never quietly skip a constraint the schema asks for.
"""

import contextlib
import copy
import io
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scoreboard import export  # noqa: E402

# ---------------------------------------------------------------------------
# Layer 1: a small draft 2020-12 validator

_ANNOTATIONS = {"$schema", "$id", "title", "description", "$defs", "$comment",
                "examples", "default"}
_KNOWN = _ANNOTATIONS | {
    "$ref", "type", "const", "enum", "pattern", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems",
    "maxItems", "uniqueItems", "minProperties", "maxProperties", "required",
    "properties", "additionalProperties", "propertyNames", "items", "oneOf",
    "anyOf", "allOf",
}


def assert_supported(schema, where="$"):
    """Refuse a schema using a keyword the validator would ignore."""
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k not in _KNOWN:
                raise RuntimeError(
                    f"{where}: schema keyword {k!r} is not implemented by this "
                    "validator — implement it or the constraint is not checked")
            if k in ("properties", "$defs"):
                for name, sub in v.items():
                    assert_supported(sub, f"{where}.{k}.{name}")
            elif k in ("oneOf", "anyOf", "allOf"):
                for i, sub in enumerate(v):
                    assert_supported(sub, f"{where}.{k}[{i}]")
            elif k in ("items", "propertyNames", "additionalProperties"):
                if isinstance(v, dict):
                    assert_supported(v, f"{where}.{k}")


def _json_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "integer" if float(v).is_integer() else "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _type_ok(v, want):
    actual = _json_type(v)
    if want == "number":
        return actual in ("number", "integer")
    return actual == want


def _validate(inst, schema, root, path, errs):
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise RuntimeError(f"only local $refs are supported, got {ref!r}")
        target = root
        for part in ref[2:].split("/"):
            target = target[part]
        _validate(inst, target, root, path, errs)
        return

    if "type" in schema:
        want = schema["type"]
        want = want if isinstance(want, list) else [want]
        if not any(_type_ok(inst, w) for w in want):
            errs.append(f"{path}: expected type {'|'.join(want)}, got "
                        f"{_json_type(inst)}")
            return
    if "const" in schema and inst != schema["const"]:
        errs.append(f"{path}: expected const {schema['const']!r}, got {inst!r}")
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in enum {schema['enum']}")

    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            errs.append(f"{path}: {inst!r} does not match /{schema['pattern']}/")
        if "minLength" in schema and len(inst) < schema["minLength"]:
            errs.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            errs.append(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        for key, op, word in (("minimum", inst.__lt__, "below"),
                              ("maximum", inst.__gt__, "above")):
            if key in schema and op(schema[key]):
                errs.append(f"{path}: {inst} {word} {key} {schema[key]}")
        if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
            errs.append(f"{path}: {inst} not > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and inst >= schema["exclusiveMaximum"]:
            errs.append(f"{path}: {inst} not < {schema['exclusiveMaximum']}")

    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errs.append(f"{path}: {len(inst)} items < minItems {schema['minItems']}")
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            errs.append(f"{path}: {len(inst)} items > maxItems {schema['maxItems']}")
        if schema.get("uniqueItems") and len(
                {json.dumps(x, sort_keys=True) for x in inst}) != len(inst):
            errs.append(f"{path}: items are not unique")
        if "items" in schema:
            for i, item in enumerate(inst):
                _validate(item, schema["items"], root, f"{path}[{i}]", errs)

    if isinstance(inst, dict):
        for key in schema.get("required", []):
            if key not in inst:
                errs.append(f"{path}: missing required property {key!r}")
        if "minProperties" in schema and len(inst) < schema["minProperties"]:
            errs.append(f"{path}: {len(inst)} properties < minProperties "
                        f"{schema['minProperties']}")
        if "maxProperties" in schema and len(inst) > schema["maxProperties"]:
            errs.append(f"{path}: {len(inst)} properties > maxProperties "
                        f"{schema['maxProperties']}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in inst:
                _validate(inst[key], sub, root, f"{path}.{key}", errs)
        if "propertyNames" in schema:
            for key in inst:
                _validate(key, schema["propertyNames"], root,
                          f"{path} property name {key!r}", errs)
        extra = [k for k in inst if k not in props]
        ap = schema.get("additionalProperties", True)
        if ap is False and extra:
            word = "property" if len(extra) == 1 else "properties"
            errs.append(f"{path}: unexpected {word} {sorted(extra)}")
        elif isinstance(ap, dict):
            for key in extra:
                _validate(inst[key], ap, root, f"{path}.{key}", errs)

    for key, need in (("allOf", "all"), ("anyOf", "any"), ("oneOf", "one")):
        if key not in schema:
            continue
        results = []
        for sub in schema[key]:
            sub_errs: list[str] = []
            _validate(inst, sub, root, path, sub_errs)
            results.append(sub_errs)
        n_ok = sum(1 for r in results if not r)
        if (need == "all" and n_ok != len(results)) \
                or (need == "any" and n_ok == 0) \
                or (need == "one" and n_ok != 1):
            detail = "; ".join(r[0] for r in results if r) or "all branches matched"
            errs.append(f"{path}: failed {key} "
                        f"({n_ok}/{len(results)} matched) — {detail}")


try:
    import jsonschema

    VALIDATOR = f"jsonschema {jsonschema.__version__}"
except ImportError:
    jsonschema = None
    VALIDATOR = "built-in (jsonschema not installed in this env)"


def schema_errors(instance, schema) -> list[str]:
    """[] when `instance` satisfies `schema`, else a list of messages."""
    if jsonschema is None:
        errs: list[str] = []
        _validate(instance, schema, schema, "$", errs)
        return errs
    v = jsonschema.Draft202012Validator(schema)
    return [f"${e.json_path[1:]}: {e.message}" for e in v.iter_errors(instance)]


# ---------------------------------------------------------------------------
# Layer 4: prove the validator rejects broken documents
#
# The fixtures are hand-written rather than taken from the emitted tree so the
# mutations below can address exact paths and so the self-test still runs on a
# checkout with no data. Each fixture doubles as a positive control: a schema
# strict enough to reject every mutation but also the legitimate document would
# otherwise sail through.

FIXTURE_POINTS = {
    "schema_version": 1,
    "init_time": "2026-07-28T00:00:00Z",
    "regime": "realtime",
    "tier": "provisional",
    "init_source": "gfs",
    "truth_source": "gfs_analysis",
    "truth_valid_through": "2026-07-28T12:00:00Z",
    "city": {"id": "sao-paulo", "name": "São Paulo", "lat": -23.55, "lon": -46.63},
    "leads": [6, 12, 18],
    "valid_times": ["2026-07-28T06:00:00Z", "2026-07-28T12:00:00Z",
                    "2026-07-28T18:00:00Z"],
    "truth": {
        "t2m": {"status": "ok", "values": [291.2, 290.1, None]},
        "tp06": {"status": "truth_pending", "values": None},
    },
    "models_expected": ["aurora", "atlas", "fengwu"],
    "models": {
        "aurora": {"t2m": {"status": "ok", "values": [291.0, 290.4, 289.9]},
                   "tp06": {"status": "ok", "values": [0.0, 0.125, 1.5]}},
        "atlas": {"t2m": {"status": "ok", "values": [291.3, 290.0, 289.5]},
                  "tp06": {"status": "ok", "native_variable": "tp",
                           "values": [0.0, 0.0, 0.25]}},
        "fengwu": {"t2m": {"status": "ok", "values": [291.1, 290.2, 289.8]},
                   "tp06": {"status": "no_variable", "values": None}},
    },
}

FIXTURE_MANIFEST = {
    "schema_version": 1,
    "generated": "2026-07-30T05:00:00Z",
    "series_statuses": ["ok", "no_variable", "truth_pending", "unavailable"],
    "leads": [6, 12, 18],
    "variables": {
        "t2m": {"label": "2 m temperature", "units": "K", "kind": "state",
                "decimals": 2, "palette": "temperature"},
        "tp06": {"label": "Precipitation (6 h accumulation)", "units": "mm/6h",
                 "kind": "precip", "decimals": 3, "palette": "precip",
                 "accumulation_hours": 6},
    },
    "cities": [{"id": "sao-paulo", "name": "São Paulo", "lat": -23.55,
                "lon": -46.63}],
    "map": {
        "max_zoom": 10, "basemap_zoom": 6, "field_opacity": 0.72,
        "basemaps": [{"id": "labels", "label": "Roads",
                      "url": "https://example.invalid/wms",
                      "layers": "OSM-Overlay-WMS", "over": True,
                      "attribution": "© OpenStreetMap contributors"}],
    },
    "inits": [{
        "init_time": "2026-07-28T00:00:00Z",
        "regime": "realtime",
        "tier": "provisional",
        "init_source": "gfs",
        "truth_source": "gfs_analysis",
        "truth_valid_through": "2026-07-28T12:00:00Z",
        "models": ["aurora", "atlas", "fengwu"],
        "models_expected": ["aurora", "atlas", "fengwu"],
        "leads": [6, 12, 18],
        "variables": ["t2m", "tp06"],
        "points": {"dir": "points/2026-07-28T00", "cities": ["sao-paulo"],
                   "bytes": 4096},
    }],
    "fields": {},
}

# (description, mutation). Each must be REJECTED by points.schema.json.
POINTS_MUTATIONS = [
    ("missing required `leads`", lambda d: d.pop("leads")),
    ("bumped schema_version", lambda d: d.update(schema_version=2)),
    ("unknown top-level key", lambda d: d.update(surprise=1)),
    ("bad init_time format", lambda d: d.update(init_time="2026-07-28 00Z")),
    ("unknown regime", lambda d: d.update(regime="sometimes")),
    ("provenance label with a space", lambda d: d.update(tier="not final")),
    ("city latitude out of range", lambda d: d["city"].update(lat=200.0)),
    ("city id not a slug", lambda d: d["city"].update(id="Sao Paulo")),
    ("city missing its longitude", lambda d: d["city"].pop("lon")),
    ("no models at all", lambda d: d.update(models={})),
    ("model id with a capital letter",
     lambda d: d["models"].update(Aurora=d["models"]["aurora"])),
    ("lead hour as a string", lambda d: d["leads"].__setitem__(0, "6")),
    ("truth object replaced by a list", lambda d: d.update(truth=[])),
    ("valid_times holding a bare date",
     lambda d: d["valid_times"].__setitem__(0, "2026-07-28")),
    ("truth_valid_through not a timestamp",
     lambda d: d.update(truth_valid_through="pending")),
    ("unknown series status",
     lambda d: d["models"]["aurora"]["t2m"].update(status="probably_fine")),
    ("ok series with a null value array",
     lambda d: d["models"]["aurora"]["t2m"].update(values=None)),
    ("ok series holding a string value",
     lambda d: d["models"]["aurora"]["t2m"]["values"].__setitem__(0, "12.3")),
    ("ok series missing `values` entirely",
     lambda d: d["models"]["aurora"]["t2m"].pop("values")),
    ("series with an extra key",
     lambda d: d["models"]["aurora"]["t2m"].update(reason="because")),
    # The two that matter most for E2: an absent precip series must stay
    # distinguishable, and must not be re-dressed as data.
    ("no_variable series smuggling values back in",
     lambda d: d["models"]["fengwu"]["tp06"].update(values=[])),
    ("no_variable series relabelled ok",
     lambda d: d["models"]["fengwu"]["tp06"].update(status="ok")),
    ("truth_pending collapsed to a bare null",
     lambda d: d["truth"].update(tp06=None)),
    ("models_expected emptied", lambda d: d.update(models_expected=[])),
    ("models_expected dropped entirely", lambda d: d.pop("models_expected")),
    ("models_expected holding a capitalised id",
     lambda d: d["models_expected"].append("Aurora")),
]

MANIFEST_MUTATIONS = [
    ("missing required `fields`", lambda d: d.pop("fields")),
    ("unknown top-level key", lambda d: d.update(extra=True)),
    ("inits not a list", lambda d: d.update(inits={})),
    ("points dir outside points/",
     lambda d: d["inits"][0]["points"].update(dir="../secrets")),
    ("points dir not an init stamp",
     lambda d: d["inits"][0]["points"].update(dir="points/latest")),
    ("init with no models", lambda d: d["inits"][0].update(models=[])),
    ("init missing models_expected",
     lambda d: d["inits"][0].pop("models_expected")),
    ("init missing its regime", lambda d: d["inits"][0].pop("regime")),
    ("duplicate lead hours", lambda d: d["inits"][0].update(leads=[6, 6])),
    ("negative byte count", lambda d: d["inits"][0]["points"].update(bytes=-1)),
    ("variable meta missing units",
     lambda d: d["variables"]["t2m"].pop("units")),
    ("unknown variable kind",
     lambda d: d["variables"]["t2m"].update(kind="vibes")),
    ("status vocabulary gained a member",
     lambda d: d["series_statuses"].append("probably_fine")),
    ("city list emptied", lambda d: d.update(cities=[])),
    ("fields section replaced by a list", lambda d: d.update(fields=[])),
    ("basemap with no attribution",
     lambda d: d["map"]["basemaps"][0].pop("attribution")),
    # http, not https. The site is served over https, so a mixed-content tile
    # is blocked outright — the basemap would simply never appear, with nothing
    # in the page to say why.
    ("basemap over plain http",
     lambda d: d["map"]["basemaps"][0].update(url="http://example.invalid/wms")),
    ("map block missing basemaps", lambda d: d["map"].pop("basemaps")),
    ("field opacity of zero", lambda d: d["map"].update(field_opacity=0)),
]


def self_test(doc, schema, mutations, label) -> list[str]:
    """Every mutation of a valid document must be rejected. Returns failures."""
    fails = []
    base = schema_errors(doc, schema)
    if base:
        fails.append(f"{label}: the document handed to the self-test is itself "
                     f"invalid ({base[0]})")
        return fails
    for desc, mutate in mutations:
        broken = copy.deepcopy(doc)
        try:
            mutate(broken)
        except (KeyError, IndexError) as e:
            fails.append(f"{label} self-test {desc!r}: mutation failed to apply ({e})")
            continue
        if not schema_errors(broken, schema):
            fails.append(f"{label} self-test: {desc!r} was ACCEPTED — the "
                         "validator is not enforcing this")
    return fails


# ---------------------------------------------------------------------------
# Layer 3: the sampler

def sampler_errors() -> list[str]:
    """Bilinear sampling must return the coordinates it was asked for.

    Latitude is linear in the grid index, so bilinear interpolation of a
    latitude field is exact — a mirrored grid shows up immediately as a sign
    flip. Longitude is tested through sin/cos so the field stays continuous
    across the 0/360 seam; interpolating the raw angle there would jump 360
    degrees and hide a seam bug behind an apparently reasonable number.

    Run on both grid orientations: e2s stores lat 90 -> -90, but nothing in the
    sampler should depend on that.
    """
    errs = []
    lat_desc = np.linspace(90.0, -90.0, 721)
    lon = np.arange(0.0, 360.0, 0.25)
    pts = [("Tokyo", 35.68, 139.69), ("Sao Paulo", -23.55, -46.63),
           ("London", 51.51, -0.13),        # 359.87 E — straddles the seam
           ("Lima", -12.05, -77.04), ("Sydney", -33.87, 151.21),
           ("seam east", 0.0, 359.99), ("seam west", 0.0, 0.01),
           ("equator", 0.0, 180.0)]
    plat = np.array([p[1] for p in pts])
    plon = np.array([p[2] for p in pts])

    for name, lat in (("descending", lat_desc), ("ascending", lat_desc[::-1])):
        lat_field = np.broadcast_to(lat[:, None], (lat.size, lon.size))
        sin_f = np.broadcast_to(np.sin(np.deg2rad(lon))[None, :],
                               (lat.size, lon.size))
        cos_f = np.broadcast_to(np.cos(np.deg2rad(lon))[None, :],
                               (lat.size, lon.size))
        got_lat = export.bilinear_sample(lat_field, lat, lon, plat, plon)
        got_lon = np.rad2deg(np.arctan2(
            export.bilinear_sample(sin_f, lat, lon, plat, plon),
            export.bilinear_sample(cos_f, lat, lon, plat, plon)))
        for (label, want_lat, want_lon), glat, glon in zip(pts, got_lat, got_lon):
            if abs(glat - want_lat) > 1e-9:
                errs.append(f"sampler ({name} lat): {label} latitude "
                            f"{glat:.6f} != {want_lat} — grid is mirrored")
            dlon = (glon - want_lon + 180.0) % 360.0 - 180.0
            if abs(dlon) > 0.01:
                errs.append(f"sampler ({name} lat): {label} longitude "
                            f"{glon:.6f} != {want_lon} (off by {dlon:.4f} deg)")
    # A single grid cell, sampled at its own corners and centre.
    cell_lat = np.array([1.0, 0.0])
    cell_lon = np.array([0.0, 1.0])
    f = np.array([[10.0, 20.0], [30.0, 40.0]])
    for (la, lo), want in (((1.0, 0.0), 10.0), ((1.0, 1.0), 20.0),
                           ((0.0, 0.0), 30.0), ((0.0, 1.0), 40.0),
                           ((0.5, 0.5), 25.0)):
        got = float(export.bilinear_sample(f, cell_lat, cell_lon,
                                           np.array([la]), np.array([lo]))[0])
        if abs(got - want) > 1e-12:
            errs.append(f"sampler: 2x2 cell at ({la}, {lo}) gave {got}, want {want}")
    return errs


# ---------------------------------------------------------------------------
# Layers 1 + 2 over the emitted tree

POINTS_BUDGET_BYTES = 1_048_576      # E2 acceptance (d): ~1 MB per init

# This script runs after the export, so the truth-availability boundary can have
# advanced by at most one lead step (6 h) in between. Anything further behind is
# not a race but a stale export: truth that exists was never sampled, and the
# page would draw fewer reference points than it could. That is the failure the
# grace deliberately does not cover — a real-time init exported once and never
# completed sits 1-5 days behind, not 6 h.
TRUTH_STALENESS_GRACE_HOURS = 6


def truth_window_errors(p: str, doc: dict, now: datetime) -> list[str]:
    """Truth must be as complete as the clock allows, and say how complete it is.

    A real-time init is exported on the day it runs, when GFS analysis covers
    only its first few leads; the rest lands over the following five days. Unless
    something insists that it arrives, nothing ever fetches it — and after
    sweep.py deletes the zarr (retention_days) the init can never be repaired.
    So `truth_valid_through` is checked against what the clock permits *now*, and
    every lead inside that window is required to carry a value. This is the
    counterpart of verify.py:141-155, which re-scores real-time leads
    incrementally for exactly the same reason.
    """
    errs = []
    fmt = export.TIME_FMT
    init = datetime.strptime(doc["init_time"], fmt)
    leads = doc["leads"]
    want = export.truth_available_through(init, leads, doc["regime"], now)
    raw = doc["truth_valid_through"]
    have = datetime.strptime(raw, fmt) if raw else None

    if want is None:
        if have is not None:
            errs.append(f"{p}: truth_valid_through is {raw} but no lead's truth "
                        f"can exist yet — the first valid time is "
                        f"{init + timedelta(hours=leads[0]):{fmt}}")
    elif have is None:
        errs.append(f"{p}: truth_valid_through is null but truth is available "
                    f"through {want:{fmt}} — stale export, re-run "
                    "`python -m scoreboard.export`")
    elif have > want:
        errs.append(f"{p}: claims truth through {raw}, but {want:{fmt}} is the "
                    "newest valid time whose truth can exist "
                    f"(now {now:{fmt}} - {export.TRUTH_LAG_HOURS} h)")
    elif want - have > timedelta(hours=TRUTH_STALENESS_GRACE_HOURS):
        n = sum(1 for h in leads if have < init + timedelta(hours=h) <= want)
        errs.append(f"{p}: truth stops at {raw} while {want:{fmt}} is available "
                    f"— {(want - have).total_seconds() / 3600:.0f} h behind, "
                    f"{n} lead(s) of truth never sampled. Re-run "
                    "`python -m scoreboard.export` to complete it.")

    if have is None:
        return errs
    for var, s in doc["truth"].items():
        if s["status"] != export.STATUS_OK:
            continue
        for lh, v in zip(leads, s["values"]):
            vt = init + timedelta(hours=lh)
            if vt <= have and v is None:
                errs.append(f"{p}: truth {var} is null at lead {lh} h "
                            f"({vt:{fmt}}), inside the window "
                            f"truth_valid_through={raw} claims to cover")
            elif vt > have and v is not None:
                errs.append(f"{p}: truth {var} has a value at lead {lh} h "
                            f"({vt:{fmt}}), past truth_valid_through={raw}")
    return errs


def check_points_file(path: Path, doc: dict, cfg: dict, city_by_id: dict,
                      model_ids: set, now: datetime) -> list[str]:
    errs = []
    p = path.name
    n_leads = len(doc["leads"])

    if doc["city"]["id"] != path.stem:
        errs.append(f"{p}: city id {doc['city']['id']!r} != filename stem")
    want_city = city_by_id.get(doc["city"]["id"])
    if want_city is None:
        errs.append(f"{p}: city {doc['city']['id']!r} is not in "
                    "config.yaml display.cities")
    elif doc["city"] != want_city:
        errs.append(f"{p}: city record {doc['city']} != config's {want_city} — "
                    "stale export after a config edit")

    if doc["leads"] != sorted(doc["leads"]):
        errs.append(f"{p}: leads are not ascending")
    if len(doc["valid_times"]) != n_leads:
        errs.append(f"{p}: {len(doc['valid_times'])} valid_times for "
                    f"{n_leads} leads")
    else:
        init = datetime.strptime(doc["init_time"], export.TIME_FMT)
        for lh, vt in zip(doc["leads"], doc["valid_times"]):
            want = f"{init + timedelta(hours=lh):{export.TIME_FMT}}"
            if vt != want:
                errs.append(f"{p}: lead {lh} h has valid_time {vt}, want {want}")

    def check_series(owner: str, var: str, s: dict, is_truth: bool = False):
        if s["status"] != export.STATUS_OK:
            if s["values"] is not None:
                errs.append(f"{p}: {owner}.{var} is {s['status']} but carries "
                            "values")
            return
        if len(s["values"]) != n_leads:
            errs.append(f"{p}: {owner}.{var} has {len(s['values'])} values for "
                        f"{n_leads} leads")
            return
        if is_truth:
            return          # truth nulls are the availability window, checked below
        # A forecast, unlike truth, covers every lead the moment it is written.
        # A hole therefore means the sample was never taken — a city added to
        # config.yaml after this init was sampled, or an interrupted re-sample —
        # and it is only repairable while the forecast zarr survives.
        holes = [lh for lh, v in zip(doc["leads"], s["values"]) if v is None]
        if holes:
            errs.append(f"{p}: {owner}.{var} is {export.STATUS_OK!r} but has no "
                        f"value at lead(s) {holes} h. Forecasts have no "
                        "availability lag, so this sample was never taken. "
                        "Re-run `python -m scoreboard.verify "
                        "--backfill-points`, which can only help while the "
                        "forecast zarr still exists.")

    for var, s in doc["truth"].items():
        check_series("truth", var, s, is_truth=True)
    for model, series in doc["models"].items():
        for var, s in series.items():
            check_series(model, var, s)

    errs += truth_window_errors(p, doc, now)

    # Model coverage. A points file that quietly carries a subset of the models
    # this init ran turns E3's multi-model chart into a chart of whatever
    # survived, with nothing on the page to say so.
    expected = doc["models_expected"]
    for model in expected:
        if model not in cfg["models"]:
            errs.append(f"{p}: models_expected lists {model!r}, which is not in "
                        "config.yaml's registry")
    lost = sorted(set(expected) - set(doc["models"]))
    extra = sorted(set(doc["models"]) - set(expected))
    if lost:
        errs.append(f"{p}: {len(lost)} of {len(expected)} expected models have no "
                    f"series — {lost}. Point values come from data/points.parquet, "
                    "which verify.py fills only while the forecast zarr is open; "
                    "metrics.parquet holds region aggregates and cannot supply "
                    "them. Re-export, and if the samples were never taken the "
                    "init is unrecoverable and belongs out of the manifest.")
    if extra:
        errs.append(f"{p}: carries models {extra} that models_expected omits")

    # Models must be drawable: config registry for the variable list, models.json
    # for a colour and a label.
    for model in doc["models"]:
        if model not in cfg["models"]:
            errs.append(f"{p}: model {model!r} is not in config.yaml's registry")
            continue
        if model not in model_ids:
            errs.append(f"{p}: model {model!r} has no models.json entry — it "
                        "would render with no colour or label")
        mcfg = cfg["models"][model]
        want = {export.canonical_variable(v) for v in mcfg["scored_variables"]}
        want.add(export.CANONICAL_PRECIP)
        got = set(doc["models"][model])
        if got != want:
            errs.append(f"{p}: {model} carries variables {sorted(got)}, "
                        f"expected {sorted(want)}")

        # E2's core requirement: the two precipitation-absence cases, present
        # where they belong and never collapsed to null.
        precip = doc["models"][model].get(export.CANONICAL_PRECIP)
        if precip is None:
            continue
        if mcfg.get("precip_variable") is None:
            if precip["status"] != export.STATUS_NO_VARIABLE:
                errs.append(
                    f"{p}: {model} has no precip_variable in config, so its "
                    f"{export.CANONICAL_PRECIP} series must be "
                    f"{export.STATUS_NO_VARIABLE!r}, got {precip['status']!r}")
        elif precip["status"] == export.STATUS_NO_VARIABLE:
            errs.append(f"{p}: {model} DOES have precip_variable "
                        f"{mcfg['precip_variable']!r} but its series claims "
                        f"{export.STATUS_NO_VARIABLE}")
        elif precip["status"] == export.STATUS_OK:
            native = precip.get("native_variable", export.CANONICAL_PRECIP)
            if native != mcfg["precip_variable"]:
                errs.append(f"{p}: {model} precip native_variable {native!r} != "
                            f"config's {mcfg['precip_variable']!r}")

    truth_precip = doc["truth"].get(export.CANONICAL_PRECIP)
    any_precip = any(cfg["models"][m].get("precip_variable")
                     for m in doc["models"] if m in cfg["models"])
    if doc["regime"] == "realtime" and any_precip:
        if truth_precip is None:
            errs.append(f"{p}: real-time init has no {export.CANONICAL_PRECIP} "
                        "truth entry at all — a consumer cannot tell that truth "
                        "is pending rather than that precip does not exist")
        elif truth_precip["status"] != export.STATUS_TRUTH_PENDING:
            errs.append(f"{p}: real-time init must mark "
                        f"{export.CANONICAL_PRECIP} truth "
                        f"{export.STATUS_TRUTH_PENDING!r} (verify.py:146-149 has "
                        f"no IMERG truth), got {truth_precip['status']!r}")
    if doc["regime"] == "historic" and any_precip:
        if truth_precip is None or truth_precip["status"] != export.STATUS_OK:
            errs.append(f"{p}: historic init should have ERA5 "
                        f"{export.CANONICAL_PRECIP} truth, got "
                        f"{truth_precip and truth_precip['status']}")

    # Truth must exist for every state variable some model forecasts.
    for var in cfg["verification"]["state_variables"]:
        if any(var in s for s in doc["models"].values()) and var not in doc["truth"]:
            errs.append(f"{p}: no truth series for {var}")
    return errs


def check_manifest(doc: dict, root: Path, cfg: dict) -> list[str]:
    errs = []
    dirs = sorted(d.name for d in (root / "points").glob("*") if d.is_dir())
    listed = [e["points"]["dir"].split("/", 1)[1] for e in doc["inits"]]
    if listed != dirs:
        errs.append(f"manifest.json: lists inits {listed} but points/ holds {dirs}")

    want_cities = [c["id"] for c in export.cities_payload(cfg)]
    if [c["id"] for c in doc["cities"]] != want_cities:
        errs.append("manifest.json: cities do not match config.yaml display.cities")
    if not isinstance(doc["fields"], dict):
        errs.append("manifest.json: `fields` must be an object")

    union_leads = set()
    for e in doc["inits"]:
        name = e["points"]["dir"].split("/", 1)[1]
        union_leads |= set(e["leads"])
        d = root / e["points"]["dir"]
        files = sorted(p.stem for p in d.glob("*.json"))
        if files != sorted(e["points"]["cities"]):
            errs.append(f"manifest.json {name}: cities {e['points']['cities']} "
                        f"!= files on disk {files}")
        nbytes = sum(p.stat().st_size for p in d.glob("*.json"))
        if nbytes != e["points"]["bytes"]:
            errs.append(f"manifest.json {name}: bytes {e['points']['bytes']} "
                        f"!= actual {nbytes}")
        if nbytes > POINTS_BUDGET_BYTES:
            errs.append(f"{name}: points total {nbytes / 1024:.0f} KiB exceeds "
                        f"the {POINTS_BUDGET_BYTES / 1024:.0f} KiB per-init budget")
        for var in e["variables"]:
            if var not in doc["variables"]:
                errs.append(f"manifest.json {name}: variable {var!r} has no "
                            "entry in `variables`")
        absent = sorted(set(want_cities) - set(e["points"]["cities"]))
        if absent:
            errs.append(f"manifest.json {name}: no points file for {absent} — "
                        f"config.yaml lists {len(want_cities)} cities. Re-export "
                        "this init, or drop its directory if its forecasts are "
                        "gone.")

        # EVERY city file, not a sample of one. A partially failed export leaves
        # most cities right and a handful wrong, and the manifest's byte count
        # agrees either way once it has been recomputed — so checking one city
        # and trusting the byte total is exactly the hole such a failure fits
        # through. E3 iterates all of them.
        for cid in e["points"]["cities"]:
            fp = d / f"{cid}.json"
            try:
                doc_c = json.loads(fp.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                errs.append(f"manifest.json {name}: cannot read {cid}.json to "
                            f"cross-check against ({exc})")
                continue
            for key in ("init_time", "regime", "tier", "init_source",
                        "truth_source", "truth_valid_through", "leads"):
                if doc_c.get(key) != e[key]:
                    errs.append(f"manifest.json {name}: {key} {e[key]!r} != "
                                f"{cid}.json's {doc_c.get(key)!r}")
            for key in ("models", "models_expected"):
                if sorted(doc_c.get(key) or []) != sorted(e[key]):
                    errs.append(f"manifest.json {name}: {key} {sorted(e[key])} "
                                f"!= {cid}.json's "
                                f"{sorted(doc_c.get(key) or [])}")
            got_vars = sorted({v for m in doc_c["models"].values() for v in m}
                              | set(doc_c["truth"]))
            if got_vars != sorted(e["variables"]):
                errs.append(f"manifest.json {name}: variables "
                            f"{sorted(e['variables'])} != {cid}.json's "
                            f"{got_vars}")
    if set(doc["leads"]) != union_leads:
        errs.append(f"manifest.json: leads {doc['leads']} != union over inits "
                    f"{sorted(union_leads)}")
    return errs


def cross_check_metrics(doc: dict, cfg: dict) -> list[str]:
    """Provenance in the manifest must match what the leaderboard reports.

    A missing metrics table is a **failure, not a skip.** Everything this
    function checks — the tier, the init and truth sources, and which models an
    init is supposed to carry — is unverifiable without it, and those are
    exactly the fields the exporter falls back to guessing when no metrics rows
    exist (`export._provenance`). A gate that skipped itself on a fresh clone
    would pass a manifest that mislabels a provisional init as final, or one
    that quietly forgot half its models, and report success.
    """
    import pandas as pd

    mpath = Path(cfg["paths"]["data"]) / "metrics.parquet"
    if not mpath.exists():
        return [f"{mpath} does not exist, so the manifest's tier / init_source "
                "/ truth_source and its models_expected cannot be checked "
                "against the leaderboard's own record. This gate does not run "
                "without it — the emitted JSON is exactly what a mislabelled "
                "init would look like."]
    df = pd.read_parquet(mpath, columns=["init_time", "model", "init_source",
                                         "truth_source", "tier"])
    errs = []
    for e in doc["inits"]:
        init = pd.Timestamp(datetime.strptime(e["init_time"], export.TIME_FMT))
        at_init = df[df.init_time == init]
        # The leaderboard is the authority on which models ran for an init, so it
        # is also the authority on what a complete export looks like. This is the
        # check that would have caught a historic init exported after sweep.py
        # had already purged nine of its ten forecasts.
        unscored = sorted(set(at_init.model) - set(e["models_expected"]))
        if unscored:
            errs.append(f"manifest.json {e['init_time']}: metrics.parquet scored "
                        f"{unscored} for this init but models_expected omits "
                        "them")
        sel = at_init[at_init.model.isin(e["models"])]
        if sel.empty:
            continue
        for key, col in (("init_source", sel.init_source),
                         ("truth_source", sel.truth_source), ("tier", sel.tier)):
            want = export.uniq_label(col)
            if e[key] != want:
                errs.append(f"manifest.json {e['init_time']}: {key} {e[key]!r} "
                            f"but metrics.parquet says {want!r}")
    return errs


def check_tree(root: Path, cfg: dict, schemas: dict,
               now: datetime) -> tuple[list[str], dict]:
    """Layers 1 + 2 over one docs/data tree. Returns (failures, stats).

    Parameterized by root, cfg and `now` so layer 5 can run the identical checks
    against a synthetic tree at a fixed clock. A gate that only ever ran against
    the real tree could not be shown to reject anything.
    """
    failures: list[str] = []
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return [f"{manifest_path} does not exist — run "
                "`python -m scoreboard.export` first"], {}
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return [f"manifest.json is not valid JSON ({e})"], {}
    try:
        model_ids = {m["id"] for m in json.loads((root / "models.json").read_text())}
    except (OSError, json.JSONDecodeError) as e:
        return [f"models.json is unreadable ({e})"], {}
    city_by_id = {c["id"]: c for c in export.cities_payload(cfg)}

    # Layer 2 assumes a schema-valid document — it indexes into series and
    # coordinates freely. A file that failed layer 1 is reported and skipped
    # rather than crashing the run, so the gate always produces a diagnosis.
    n_files = 0
    statuses_seen: set[str] = set()
    point_files = sorted((root / "points").glob("*/*.json"))
    if not point_files:
        failures.append("no points files under points/")
    for path in point_files:
        rel = path.relative_to(root)
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            failures.append(f"{rel}: not valid JSON ({e})")
            continue
        n_files += 1
        bad = schema_errors(doc, schemas["points"])
        failures += [f"{rel} {m}" for m in bad]
        if bad:
            continue
        failures += check_points_file(path, doc, cfg, city_by_id, model_ids, now)
        statuses_seen |= {s["status"] for s in doc["truth"].values()}
        statuses_seen |= {s["status"] for m in doc["models"].values()
                          for s in m.values()}

    bad = schema_errors(manifest, schemas["manifest"])
    failures += [f"manifest.json {m}" for m in bad]
    if not bad:
        failures += check_manifest(manifest, root, cfg)
        failures += cross_check_metrics(manifest, cfg)
        unknown = statuses_seen - set(manifest["series_statuses"])
        if unknown:
            failures.append(f"points use statuses {sorted(unknown)} that "
                            "manifest.json's series_statuses does not publish")
    return failures, {"n_files": n_files, "statuses": sorted(statuses_seen),
                      "manifest": manifest, "manifest_valid": not bad}


# ---------------------------------------------------------------------------
# Layer 5: prove check_tree rejects a partially corrupted export
#
# A fixed clock and a fixed init, so the truth window is exact and the test does
# not rot: 2026-07-29T00:00 minus the 6 h analysis lag puts the boundary at
# 2026-07-28T18:00, which covers leads 6/12/18 of a 2026-07-28T00 init but not 24.

SYNTH_NOW = datetime(2026, 7, 29, 0)
SYNTH_INIT = datetime(2026, 7, 28, 0)
SYNTH_LEADS = [6, 12, 18, 24]
SYNTH_THROUGH = "2026-07-28T18:00:00Z"
SYNTH_DIR = f"{SYNTH_INIT:{export.INIT_FMT}}"

# The tree also carries a historic init, whose rules are the opposite ones: ERA5
# truth is complete the moment the init is old enough to be historic, so every
# lead must carry a value and precipitation truth must be present rather than
# pending. Whether the real docs/data tree happens to hold a historic init
# depends on which forecast zarrs have survived retention, so the gate cannot
# rely on it to exercise those branches.
SYNTH_HIST_INIT = datetime(2023, 1, 15, 0)
SYNTH_HIST_DIR = f"{SYNTH_HIST_INIT:{export.INIT_FMT}}"
SYNTH_HIST_THROUGH = "2023-01-16T00:00:00Z"


def _synth_cfg(site: Path) -> dict:
    """A three-model, two-city config describing the synthetic tree.

    `paths.data` points at a store this test materializes, so layer 5 exercises
    the provenance cross-check too rather than the version of it that skips.
    """
    ids = ("aurora", "atlas", "fengwu")
    return {
        "paths": {"data": str(site / "store"), "site": str(site)},
        "historic_cutoff_days": 120,
        "retention_days": 30,
        "verification": {"state_variables": ["t2m"]},
        "models": {
            "aurora": {"scored_variables": ["t2m", "tp06"],
                       "precip_variable": "tp06"},
            "atlas": {"scored_variables": ["t2m", "tp"], "precip_variable": "tp"},
            "fengwu": {"scored_variables": ["t2m"]},
        },
        "display": {
            "models": [{"id": m, "label": m.title(), "css_var": f"--s-{m}",
                        "color": "#123456", "color_dark": "#654321", "width": 2}
                       for m in ids],
            "cities": [{"name": "Alphaville", "lat": 10.0, "lon": 20.0},
                       {"name": "Betaville", "lat": -30.0, "lon": -40.0}],
        },
    }


def _ok(values, **extra):
    return {"status": "ok", **extra, "values": values}


def _synth_doc(city: dict, historic: bool = False) -> dict:
    init = SYNTH_HIST_INIT if historic else SYNTH_INIT
    return {
        "schema_version": 1,
        "init_time": f"{init:{export.TIME_FMT}}",
        "regime": "historic" if historic else "realtime",
        "tier": "final" if historic else "provisional",
        "init_source": "era5_arco" if historic else "gfs",
        "truth_source": "era5_arco" if historic else "gfs_analysis",
        "truth_valid_through": SYNTH_HIST_THROUGH if historic else SYNTH_THROUGH,
        "city": city,
        "leads": list(SYNTH_LEADS),
        "valid_times": [f"{init + timedelta(hours=h):{export.TIME_FMT}}"
                        for h in SYNTH_LEADS],
        "truth": {
            "t2m": _ok([300.0, 301.0, 302.0, 303.0] if historic
                       else [300.0, 301.0, 302.0, None]),
            "tp06": (_ok([0.0, 0.5, 1.0, 0.0]) if historic
                     else {"status": "truth_pending", "values": None}),
        },
        "models_expected": ["aurora", "atlas", "fengwu"],
        "models": {
            "aurora": {"t2m": _ok([300.1, 301.2, 302.0, 302.5]),
                       "tp06": _ok([0.0, 0.5, 1.25, 0.0])},
            "atlas": {"t2m": _ok([299.8, 300.9, 301.7, 302.2]),
                      "tp06": _ok([0.0, 0.25, 0.5, 0.0], native_variable="tp")},
            "fengwu": {"t2m": _ok([300.4, 301.1, 301.9, 302.6]),
                       "tp06": {"status": "no_variable", "values": None}},
        },
    }


def _write_points(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False,
                               separators=(",", ":")) + "\n")


def _write_synth_metrics(site: Path, extra: dict | None = None) -> None:
    """The metrics.parquet the synthetic export claims to have come from.

    Only the provenance columns matter here — cross_check_metrics reads the
    tier, the two source labels and the model set, never a score.
    """
    import pandas as pd

    rows = []
    for init, src, tier in ((SYNTH_INIT, "gfs", "provisional"),
                            (SYNTH_HIST_INIT, "era5_arco", "final")):
        truth = "gfs_analysis" if src == "gfs" else "era5_arco"
        for model in ("aurora", "atlas", "fengwu"):
            rows.append(dict(init_time=pd.Timestamp(init), model=model,
                             lead_hours=6, variable="t2m", region="global",
                             metric="rmse", value=1.0, init_source=src,
                             truth_source=truth, tier=tier))
    if extra:
        rows.append({**rows[0], **extra})
    out = site / "store" / "metrics.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(out, index=False)


def _synth_site(site: Path) -> dict:
    """Materialize a pristine, passing tree under `site`. Returns its cfg."""
    cfg = _synth_cfg(site)
    _write_synth_metrics(site)
    for name, historic in ((SYNTH_DIR, False), (SYNTH_HIST_DIR, True)):
        outdir = site / "data" / "points" / name
        outdir.mkdir(parents=True, exist_ok=True)
        for city in export.cities_payload(cfg):
            _write_points(outdir / f"{city['id']}.json",
                          _synth_doc(city, historic))
    export.write_models_json(cfg, site)
    export.write_manifest(cfg, site)
    return cfg


def _rebuild_manifest(cfg: dict, site: Path) -> None:
    """What the exporter itself would write for the tree as it now stands."""
    export.write_manifest(cfg, site)


def _refresh_bytes(site: Path) -> None:
    """Recompute only `points.bytes`, leaving the rest of the manifest stale.

    This is the move that made a corrupted city invisible: the byte total is the
    one thing a hand-edited tree obviously breaks, so a check that reconciles it
    and stops there passes everything else.
    """
    root = site / "data"
    m = json.loads((root / "manifest.json").read_text())
    for e in m["inits"]:
        d = root / e["points"]["dir"]
        e["points"]["bytes"] = sum(f.stat().st_size for f in d.glob("*.json"))
    (root / "manifest.json").write_text(
        json.dumps(m, indent=1, ensure_ascii=False) + "\n")


def _corrupt_one_city(site, cfg, mutate, init_dir=SYNTH_DIR):
    """Apply `mutate` to betaville.json alone, then reconcile the byte count."""
    p = site / "data" / "points" / init_dir / "betaville.json"
    doc = json.loads(p.read_text())
    mutate(doc)
    _write_points(p, doc)
    _refresh_bytes(site)


def _corrupt_all_cities(site, cfg, mutate, init_dir=SYNTH_DIR):
    """Apply `mutate` to every city, then rebuild the manifest over the result.

    Rebuilding is what the exporter would do next, so these cases are internally
    consistent end to end — nothing about them is detectable by reconciliation.
    """
    for p in sorted((site / "data" / "points" / init_dir).glob("*.json")):
        doc = json.loads(p.read_text())
        mutate(doc)
        _write_points(p, doc)
    _rebuild_manifest(cfg, site)


def _rewind_truth(doc, through, keep):
    """Pretend the export sampled truth for only the first `keep` leads."""
    doc["truth_valid_through"] = through
    doc["truth"]["t2m"]["values"] = (
        doc["truth"]["t2m"]["values"][:keep] + [None] * (len(SYNTH_LEADS) - keep))


def _drop_model(doc, model, drop_expected=False):
    doc["models"].pop(model)
    if drop_expected:
        doc["models_expected"].remove(model)


def _set_truth(doc, through, values):
    doc["truth_valid_through"] = through
    doc["truth"]["t2m"]["values"] = values


# (description, corruption). Each must make check_tree report at least one
# failure. These are the shapes a partially-failed export actually produces, not
# typos — every one of them is schema-valid, and all but the last two leave the
# manifest's byte count in perfect agreement with the files.
TREE_CORRUPTIONS = [
    ("one city loses a model, self-consistently",
     lambda s, c: _corrupt_one_city(s, c, lambda d: _drop_model(d, "atlas", True))),
    ("one city loses a model but still expects it",
     lambda s, c: _corrupt_one_city(s, c, lambda d: _drop_model(d, "atlas"))),
    # Within the staleness grace, so only the per-city cross-check can catch it.
    ("one city disagrees on the truth window",
     lambda s, c: _corrupt_one_city(
         s, c, lambda d: _rewind_truth(d, "2026-07-28T12:00:00Z", 2))),
    ("one city disagrees on tier",
     lambda s, c: _corrupt_one_city(s, c, lambda d: d.update(tier="final"))),
    ("one city file deleted, manifest rebuilt over what is left",
     lambda s, c: (
         (s / "data" / "points" / SYNTH_DIR / "betaville.json").unlink(),
         _rebuild_manifest(c, s))),
    # The reviewed regression: an init written once, never completed, and
    # internally consistent about it.
    ("every city rewound to one lead of truth, manifest rebuilt",
     lambda s, c: _corrupt_all_cities(
         s, c, lambda d: _rewind_truth(d, "2026-07-28T06:00:00Z", 1))),
    ("every city claims truth for a lead whose analysis cannot exist yet",
     lambda s, c: _corrupt_all_cities(s, c, lambda d: _set_truth(
         d, "2026-07-29T00:00:00Z", [300.0, 301.0, 302.0, 303.0]))),
    ("a truth hole inside the window the files claim to cover",
     lambda s, c: _corrupt_all_cities(s, c, lambda d: _set_truth(
         d, SYNTH_THROUGH, [None, 301.0, 302.0, None]))),
    ("a truth value past truth_valid_through",
     lambda s, c: _corrupt_all_cities(s, c, lambda d: _set_truth(
         d, SYNTH_THROUGH, [300.0, 301.0, 302.0, 303.0]))),
    ("a model missing from every city but still expected",
     lambda s, c: _corrupt_all_cities(s, c, lambda d: _drop_model(d, "atlas"))),
    ("manifest lists a model no city carries",
     lambda s, c: _edit_manifest(
         s, lambda m: m["inits"][0]["models"].append("fuxi"))),
    ("manifest lists an init with no directory",
     lambda s, c: _edit_manifest(s, lambda m: m["inits"].append(
         copy.deepcopy(m["inits"][0])))),
    # Historic inits: ERA5 truth is complete, so an incomplete one is a bug
    # rather than a wait, and precipitation truth must be present, not pending.
    ("a historic init missing truth at its last lead",
     lambda s, c: _corrupt_all_cities(
         s, c, lambda d: _rewind_truth(d, "2023-01-15T18:00:00Z", 3),
         init_dir=SYNTH_HIST_DIR)),
    ("a historic init with precip truth marked pending",
     lambda s, c: _corrupt_all_cities(
         s, c, lambda d: d["truth"].update(
             tp06={"status": "truth_pending", "values": None}),
         init_dir=SYNTH_HIST_DIR)),
    # A forecast hole, which is what a city added to config.yaml after an init
    # was sampled looks like — schema-valid, and indistinguishable from truth's
    # legitimate nulls unless the two are checked by different rules.
    ("one city missing a forecast value at one lead",
     lambda s, c: _corrupt_one_city(
         s, c, lambda d: d["models"]["aurora"]["t2m"]["values"].__setitem__(
             2, None))),
    # The provenance cross-check, which is the only thing standing between a
    # mislabelled regime and a passing gate.
    ("metrics.parquet absent",
     lambda s, c: (s / "store" / "metrics.parquet").unlink()),
    ("every city relabelled final while metrics says provisional",
     lambda s, c: _corrupt_all_cities(s, c, lambda d: d.update(tier="final"))),
    ("metrics.parquet scored a model the export never mentions",
     lambda s, c: _write_synth_metrics(s, extra={"model": "fuxi"})),
]


def _edit_manifest(site: Path, mutate) -> None:
    root = site / "data"
    m = json.loads((root / "manifest.json").read_text())
    mutate(m)
    (root / "manifest.json").write_text(
        json.dumps(m, indent=1, ensure_ascii=False) + "\n")


def tree_self_test(schemas: dict) -> list[str]:
    """Every corruption of a valid tree must be caught. Returns failures."""
    fails = []
    tmp = Path(tempfile.mkdtemp(prefix="check_export_"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            cfg = _synth_site(tmp)
            base, _ = check_tree(tmp / "data", cfg, schemas, SYNTH_NOW)
        if base:
            return ["tree self-test: the pristine synthetic tree does not pass "
                    f"its own checks ({base[0]})"]
        for desc, corrupt in TREE_CORRUPTIONS:
            work = Path(tempfile.mkdtemp(prefix="check_export_case_"))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    shutil.copytree(tmp, work, dirs_exist_ok=True)
                    cfg_w = _synth_cfg(work)
                    corrupt(work, cfg_w)
                    errs, _ = check_tree(work / "data", cfg_w, schemas, SYNTH_NOW)
                if not errs:
                    fails.append(f"tree self-test: {desc!r} was ACCEPTED — the "
                                 "semantic layer is not enforcing this")
            except Exception as e:  # noqa: BLE001
                fails.append(f"tree self-test {desc!r}: raised instead of "
                             f"reporting ({type(e).__name__}: {e})")
            finally:
                shutil.rmtree(work, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    repo = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((repo / a.config).read_text())
    root = repo / cfg["paths"]["site"] / "data"

    schemas = {}
    for name in ("manifest", "points"):
        s = json.loads((root / "schema" / f"{name}.schema.json").read_text())
        assert_supported(s, name)
        schemas[name] = s
    print(f"[check] validator: {VALIDATOR}; schemas: "
          f"{', '.join(sorted(schemas))}")

    failures: list[str] = []
    failures += sampler_errors()
    print(f"[check] bilinear sampler: {'FAIL' if failures else 'ok'} "
          "(latitude exact on both orientations, longitude across the 0/360 seam)")

    now = export._utcnow()
    tree_failures, stats = check_tree(root, cfg, schemas, now)
    failures += tree_failures
    if not stats:
        print(f"\n[check] FAILED — {failures[0]}")
        return 1
    manifest, bad = stats["manifest"], not stats["manifest_valid"]
    print(f"[check] points: {stats['n_files']} file(s) across "
          f"{len(manifest['inits'])} init(s); statuses used: "
          f"{stats['statuses']}")

    failures += self_test(FIXTURE_MANIFEST, schemas["manifest"],
                          MANIFEST_MUTATIONS, "manifest")
    failures += self_test(FIXTURE_POINTS, schemas["points"], POINTS_MUTATIONS,
                          "points")
    print(f"[check] validator self-test: {len(POINTS_MUTATIONS)} points + "
          f"{len(MANIFEST_MUTATIONS)} manifest mutations, all rejected as "
          "required")

    tree_fails = tree_self_test(schemas)
    failures += tree_fails
    print(f"[check] semantic self-test: {len(TREE_CORRUPTIONS)} corrupted "
          f"tree(s){'' if not tree_fails else ' — FAILURES BELOW'}, all caught "
          "as required (pristine tree passes)")

    for e in (manifest["inits"] if not bad else []):
        n = e["points"]["bytes"]
        through = e["truth_valid_through"] or "none yet"
        first = datetime.strptime(e["init_time"], export.TIME_FMT)
        covered = sum(1 for h in e["leads"]
                      if e["truth_valid_through"]
                      and first + timedelta(hours=h)
                      <= datetime.strptime(e["truth_valid_through"],
                                           export.TIME_FMT))
        print(f"[check] {e['init_time']}  {e['regime']:<8} tier={e['tier']:<11} "
              f"{len(e['models'])}/{len(e['models_expected'])} model(s)  "
              f"{len(e['points']['cities'])} cities  "
              f"truth {covered}/{len(e['leads'])} leads (through {through})  "
              f"{n / 1024:.0f} KiB "
              f"({100 * n / POINTS_BUDGET_BYTES:.0f}% of budget)")

    if failures:
        print(f"\n[check] FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"\n[check] OK — {stats['n_files']} points file(s) + manifest.json "
          "valid against docs/data/schema/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
