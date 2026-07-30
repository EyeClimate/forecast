"""Per-init-time data source resolution: historic vs real-time regime.

The rest of the pipeline never hard-codes a data source — it asks this module,
which picks based on where the init time falls relative to truth availability.
"""

from datetime import datetime, timedelta, timezone

# Forecast-variable -> truth-variable naming (truth serves 6h-accumulated
# precip as tp06; Atlas calls its 6h accumulation tp).
TRUTH_VAR_MAP = {"tp": "tp06"}


def truth_variable(forecast_variable: str) -> str:
    return TRUTH_VAR_MAP.get(forecast_variable, forecast_variable)


def regime(init_time: datetime, cutoff_days: int = 6) -> str:
    t = init_time if init_time.tzinfo else init_time.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - t
    return "historic" if age > timedelta(days=cutoff_days) else "realtime"


class ARCOInit:
    """ERA5 via ARCO with derived variables; used for both init and truth.

    ARCO's lexicon lacks two things models/verification need:
    - r{level} (relative humidity, a FuXi input): synthesized from ARCO
      q{level}/t{level} via the Magnus saturation formula (over water),
      matching ERA5's r to within a few percent.
    - tp06 (6 h precip accumulation, FuXi input + verification truth):
      synthesized by summing the six hourly ERA5 tp accumulations in (t-6h, t].
    - tcw/swvl1/swvl2/stl1/stl2 (AIFS inputs): present in the ARCO store but
      absent from e2s's lexicon — registered via EXTRA_VOCAB below.
    All other variables pass through to ARCO unchanged. (WB2 would serve both
    pre-computed but its zarr ends 2023-01-10; ARCO covers 1940->recent.)
    """

    # Arrays the ARCO store carries but e2s's lexicon doesn't map — AIFS
    # inputs (total column water, soil moisture/temperature layers 1-2).
    # Registered into the lexicon at runtime so ARCO fetches them natively.
    EXTRA_VOCAB = {
        "tcw": "total_column_water::",
        "swvl1": "volumetric_soil_water_layer_1::",
        "swvl2": "volumetric_soil_water_layer_2::",
        "stl1": "soil_temperature_level_1::",
        "stl2": "soil_temperature_level_2::",
    }

    def __init__(self):
        from earth2studio.data import ARCO
        from earth2studio.lexicon import ARCOLexicon

        for k, v in self.EXTRA_VOCAB.items():
            ARCOLexicon.VOCAB.setdefault(k, v)
        self.arco = ARCO()

    @staticmethod
    def _rh_from_qt(q, t, level_hpa: float):
        import numpy as np

        p = level_hpa * 100.0  # Pa
        e = q * p / (0.622 + 0.378 * q)  # vapor pressure
        es = 611.2 * np.exp(17.67 * (t - 273.15) / (t - 29.65))  # saturation
        return np.clip(100.0 * e / es, 0.0, 110.0)

    def __call__(self, time, variable):
        import numpy as np
        import pandas as pd
        import xarray as xr

        times = list(time) if isinstance(time, (list, tuple, np.ndarray)) else [time]
        # Callers pass datetime, np.datetime64, or raw ints — normalize once so
        # the timedelta arithmetic below always works.
        times = [pd.Timestamp(t).to_pydatetime() for t in times]
        variables = (
            list(variable)
            if isinstance(variable, (list, tuple, np.ndarray))
            else [variable]
        )
        r_vars = [v for v in variables if v[0] == "r" and v[1:].isdigit()]
        want_tp06 = "tp06" in variables
        other = [v for v in variables if v not in r_vars and v != "tp06"]

        # One ARCO fetch for passthrough vars plus q/t needed for derived r
        need = list(other)
        for rv in r_vars:
            lev = rv[1:]
            for base in (f"q{lev}", f"t{lev}"):
                if base not in need:
                    need.append(base)
        da = self.arco(times, need) if need else None

        pieces = []
        if other:
            pieces.append(da.sel(variable=other))
        for rv in r_vars:
            lev = rv[1:]
            q = da.sel(variable=f"q{lev}")
            t = da.sel(variable=f"t{lev}")
            r = self._rh_from_qt(q, t, float(lev))
            r = r.drop_vars("variable", errors="ignore").expand_dims(variable=[rv]).transpose(
                "time", "variable", "lat", "lon"
            )
            pieces.append(r)
        if want_tp06:
            # 6 h accumulation ending at t = sum of hourly ERA5 tp in (t-6h, t]
            hourly = sorted({t - timedelta(hours=h) for t in times for h in range(6)})
            tp = self.arco(hourly, ["tp"]).sel(variable="tp")
            slabs = []
            for t in times:
                window = [t - timedelta(hours=h) for h in range(5, -1, -1)]
                slabs.append(tp.sel(time=window).sum("time"))
            tp06 = xr.concat(slabs, dim="time").assign_coords(time=times)
            tp06 = tp06.expand_dims(variable=["tp06"]).transpose(
                "time", "variable", "lat", "lon"
            )
            pieces.append(tp06)
        out = xr.concat(pieces, dim="variable") if len(pieces) > 1 else pieces[0]
        return out.sel(variable=variables)


class GFSInit:
    """GFS analysis (0.25°, 6-hourly cycles) for real-time initial conditions.

    Mirrors ARCOInit's derived-variable handling, but GFS needs less of it:
    - r{level} (FuXi input): GFS maps relative humidity natively — passthrough,
      no Magnus synthesis.
    - tp06 (FuXi input): GFS analysis files (f000) carry no accumulated
      precip, so the 6 h accumulation ending at t comes from the previous
      cycle's own short forecast — GFS_FX init t-6h at lead +6 h, APCP
      accumulated over (t-6h, t], the same window the historic branch sums
      from hourly ERA5 tp. (Model-background precip, not analysis, but it is
      only a model input here, never verification truth.)
    Variables with neither a lexicon entry nor a synthesis (Atlas's sst,
    AIFS's tcw/swvl/stl soil fields) raise immediately — GFS would otherwise
    return them silently NaN-filled.
    """

    def __init__(self):
        from earth2studio.data import GFS, GFS_FX

        self.gfs = GFS()
        self.gfs_fx = GFS_FX()

    def __call__(self, time, variable):
        import numpy as np
        import pandas as pd
        import xarray as xr
        from earth2studio.lexicon import GFSLexicon

        times = list(time) if isinstance(time, (list, tuple, np.ndarray)) else [time]
        times = [pd.Timestamp(t).to_pydatetime() for t in times]
        variables = (
            list(variable)
            if isinstance(variable, (list, tuple, np.ndarray))
            else [variable]
        )
        unsupported = [
            v for v in variables if v != "tp06" and v not in GFSLexicon.VOCAB
        ]
        if unsupported:
            raise ValueError(
                f"Variables {unsupported} have no GFS lexicon entry and no "
                "real-time synthesis — this model cannot initialize from GFS yet."
            )

        other = [v for v in variables if v != "tp06"]
        pieces = []
        if other:
            pieces.append(self.gfs(times, other))
        if "tp06" in variables:
            cycles = [t - timedelta(hours=6) for t in times]
            fx = self.gfs_fx(cycles, [timedelta(hours=6)], ["tp"])
            tp06 = fx.isel(lead_time=0, drop=True).assign_coords(
                time=times, variable=["tp06"]
            )
            pieces.append(tp06)
        out = xr.concat(pieces, dim="variable") if len(pieces) > 1 else pieces[0]
        return out.sel(variable=variables)


def init_source(init_time: datetime, cutoff_days: int = 6):
    """Returns (DataSource, label) providing initial conditions for a model."""
    if regime(init_time, cutoff_days) == "historic":
        return ARCOInit(), "era5_arco"
    return GFSInit(), "gfs"


def truth_source(init_time: datetime, last_valid_time: datetime, cutoff_days: int = 6):
    """Returns (DataSource, label) providing verification truth."""
    if regime(init_time, cutoff_days) == "historic":
        return ARCOInit(), "era5_arco"
    raise NotImplementedError(
        "Real-time truth (IMERG Late / GFS analysis at valid time) is not "
        "implemented yet; this init will be scored once it is and truth arrives."
    )


def climatology_source():
    """Returns (DataSource, label) for ACC anomaly climatology."""
    from earth2studio.data import WB2Climatology

    return WB2Climatology(), "wb2_climatology"
