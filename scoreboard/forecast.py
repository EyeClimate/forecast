"""Run one model for one init time, storing scored variables to zarr."""

import shutil
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np
import torch

from . import sources


class _ConcatOutputCoords:
    """Always concatenate prognostic + diagnostic variables (the default
    silently drops prognostic vars when dim orders differ)."""

    def __call__(self, px_coords, dx_coords):
        out = px_coords.copy()
        out["variable"] = np.concatenate(
            [px_coords["variable"]] + [c["variable"] for c in dx_coords]
        )
        return out


class _ConcatOutputTensor(torch.nn.Module):
    def forward(self, px_x, px_coords, dx_x, dx_coords):
        import torch as _torch

        keys = list(px_coords.keys())
        var_dim = keys.index("variable")
        lat_dim = keys.index("lat")
        import os

        if os.environ.get("SCOREBOARD_DEBUG"):
            print(f"[concat-debug] keys={keys} px={tuple(px_x.shape)} "
                  f"dx={[tuple(d.shape) for d in dx_x]}", flush=True)
        aligned = []
        for d in dx_x:
            d = d.to(px_x.device)
            # Prognostics/diagnostics disagree on pole rows (Aurora runs 720
            # lats, AFNO diagnostics 720, derived diagnostics 721). Crop or
            # replicate-pad the diagnostic to the prognostic's lat grid.
            if d.shape[lat_dim] > px_x.shape[lat_dim]:
                d = d.narrow(lat_dim, 0, px_x.shape[lat_dim])
            while d.shape[lat_dim] < px_x.shape[lat_dim]:
                d = _torch.cat(
                    [d, d.narrow(lat_dim, d.shape[lat_dim] - 1, 1)], dim=lat_dim
                )
            aligned.append(d)
        x = _torch.cat([px_x] + aligned, dim=var_dim)
        coords = px_coords.copy()
        coords["variable"] = np.concatenate(
            [px_coords["variable"]] + [c["variable"] for c in dx_coords]
        )
        return x, coords


class _LeadCoordFix(torch.nn.Module):
    """Normalize iterator yields where coords['lead_time'] disagrees with the
    tensor (e2s Aurora yields its IC with 2 lead coords but a 1-deep tensor,
    which breaks downstream diagnostics that loop over coords)."""

    def __init__(self, px):
        super().__init__()
        self.px = px

    def input_coords(self):
        return self.px.input_coords()

    def output_coords(self, input_coords):
        return self.px.output_coords(input_coords)

    def __call__(self, x, coords):
        return self.px(x, coords)

    def create_iterator(self, x, coords):
        for xx, cc in self.px.create_iterator(x, coords):
            keys = list(cc.keys())
            if "lead_time" in cc:
                ld = keys.index("lead_time")
                lead = np.atleast_1d(cc["lead_time"])
                if len(lead) != xx.shape[ld]:
                    lead = lead[-xx.shape[ld]:]
                cc = cc.copy()
                cc["lead_time"] = lead
            yield xx, cc


def build_precip_chain(px_model):
    """Chain derived sp -> tcwv -> PrecipitationAFNOv2 onto a prognostic whose
    outputs include z/t/q pressure levels (e.g. Aurora). Adds a tp06 output."""
    from datetime import datetime

    from earth2studio.data import SurfaceGeoPotential
    from earth2studio.models.dx import (
        DerivedSurfacePressure,
        DerivedTCWV,
        PrecipitationAFNOv2,
    )
    from earth2studio.models.px import DiagnosticWrapper

    zs = SurfaceGeoPotential()([datetime(2000, 1, 1)], ["zsl"])
    z_surf = torch.from_numpy(zs.isel(time=0).sel(variable="z").values)
    z_coords = OrderedDict({"lat": zs.lat.values, "lon": zs.lon.values})
    sp_dx = DerivedSurfacePressure(
        p_levels=[1000, 925, 850, 700, 600, 500],
        surface_geopotential=z_surf,
        surface_geopotential_coords=z_coords,
    )
    tcwv_dx = DerivedTCWV(
        levels=[1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100, 50]
    )
    precip_dx = PrecipitationAFNOv2.load_model(
        PrecipitationAFNOv2.load_default_package()
    )
    model = _LeadCoordFix(px_model)
    for dx in (sp_dx, tcwv_dx, precip_dx):
        model = DiagnosticWrapper(
            model,
            dx,
            prepare_output_coords=_ConcatOutputCoords(),
            prepare_output_tensor=_ConcatOutputTensor(),
        )
    return model


def _patch_disco_chunked_einsum():
    """FCN3 is badged for 80 GB cards: its decoder DISCO conv materializes the
    kernel-basis contraction (B,C,K,721,1440 fp32, ~20 GiB) and then einsums it
    against the weights, which peaks past 48 GB. Splitting that einsum over the
    kernel dim K keeps the math identical (the K-sum is just accumulated
    sequentially) while shrinking the transient buffers by a factor of K."""
    import torch_harmonics.disco.convolution as _dc

    if getattr(_dc.DiscreteContinuousConvS2.forward, "_chunked", False):
        return

    def forward(self, x):
        if self.optimized_kernel:
            x = _dc._disco_s2_contraction_optimized(
                x, self.psi_roff_idx, self.psi_ker_idx, self.psi_row_idx,
                self.psi_col_idx, self.psi_vals, self.kernel_size,
                self.nlat_out, self.nlon_out,
            )
        else:
            x = _dc._disco_s2_contraction_torch(
                x, self.psi.to(x.device), self.nlon_out
            )
        B, C, K, H, W = x.shape
        x = x.reshape(B, self.groups, self.groupsize, K, H, W)
        w = self.weight.reshape(
            self.groups, -1, self.weight.shape[1], self.weight.shape[2]
        )
        out = None
        for k in range(K):
            t = torch.einsum("bgcxy,goc->bgoxy", x[:, :, :, k], w[:, :, :, k])
            out = t if out is None else out.add_(t)
        del x
        out = out.reshape(B, -1, H, W)
        if self.bias is not None:
            out = out + self.bias.reshape(1, -1, 1, 1)
        return out

    forward._chunked = True
    _dc.DiscreteContinuousConvS2.forward = forward


def load_model(name: str):
    if name == "atlas":
        from earth2studio.models.px import Atlas

        return Atlas.load_model(Atlas.load_default_package())
    if name == "fuxi":
        from earth2studio.models.px import FuXi

        return FuXi.load_model(FuXi.load_default_package())
    if name == "aurora":
        # Aurora has no precip/sp/tcwv outputs. Chain derived diagnostics to
        # bridge to PrecipitationAFNOv2: sp from z-levels+t, tcwv from
        # q-levels+sp, then tp06 from the AFNO diagnostic. Each wrapper is
        # itself a prognostic, so nesting resolves the input dependencies.
        from earth2studio.models.px import Aurora

        model = Aurora.load_model(Aurora.load_default_package())
        return build_precip_chain(model)
    if name == "fengwu":
        from earth2studio.models.px import FengWu

        return FengWu.load_model(FengWu.load_default_package())
    if name == "pangu6":
        # Pangu6's output set is identical to Aurora's (z/q/t/u/v at 13
        # levels + msl/u10m/v10m/t2m, no sp/tcwv/precip), so the same
        # derived-diagnostics chain bridges to PrecipitationAFNOv2.
        from earth2studio.models.px import Pangu6

        model = Pangu6.load_model(Pangu6.load_default_package())
        # Pangu configures its ONNX io-binding in a custom .to(); inside the
        # DiagnosticWrapper chain only plain nn.Module.to recursion runs, so
        # move it here with a concrete device index while it is still bare.
        if torch.cuda.is_available():
            model = model.to(torch.device("cuda", torch.cuda.current_device()))
        return build_precip_chain(model)
    if name == "sfno":
        from earth2studio.models.px import SFNO

        return SFNO.load_model(SFNO.load_default_package())
    if name == "fcn3":
        from earth2studio.models.px import FCN3

        _patch_disco_chunked_einsum()
        return FCN3.load_model(FCN3.load_default_package())
    if name == "graphcast_oper":
        # JAX-based; lives in its own conda env (config: conda_env) — the
        # orchestrator shells out to `python -m scoreboard.forecast` there.
        from earth2studio.models.px import GraphCastOperational

        return GraphCastOperational.load_model(
            GraphCastOperational.load_default_package()
        )
    if name == "aifs":
        # anemoi + flash-attn; lives in its own conda env (config: conda_env).
        from earth2studio.models.px import AIFS

        return AIFS.load_model(AIFS.load_default_package())
    if name == "persistence":
        from earth2studio.models.px import Persistence

        variables = ["tp", "t2m", "u10m", "v10m", "z500", "t850", "msl"]
        domain = OrderedDict(
            {"lat": np.linspace(90, -90, 721), "lon": np.arange(0, 360, 0.25)}
        )
        return Persistence(variables, domain)
    raise KeyError(f"Unknown model '{name}'")


def forecast_path(data_dir: Path, model_name: str, init_time: datetime) -> Path:
    return data_dir / "forecasts" / model_name / f"{init_time:%Y-%m-%dT%H}.zarr"


def run_forecast(
    model_name: str,
    init_time: datetime,
    cfg: dict,
    model=None,
    device=None,
) -> Path:
    """Run (or skip, if already present) one forecast. Returns the zarr path."""
    from earth2studio.io import ZarrBackend
    from earth2studio.run import deterministic

    data_dir = Path(cfg["paths"]["data"])
    out = forecast_path(data_dir, model_name, init_time)
    if out.exists():
        print(f"[forecast] exists, skipping: {out}")
        return out
    out.parent.mkdir(parents=True, exist_ok=True)

    scored = cfg["models"][model_name]["scored_variables"]
    nsteps = cfg["forecast"]["nsteps"]
    data, init_label = sources.init_source(init_time, cfg["historic_cutoff_days"])

    if model is None:
        model = load_model(model_name)

    print(f"[forecast] {model_name} init={init_time:%Y-%m-%dT%H} "
          f"nsteps={nsteps} init_source={init_label}")
    io = ZarrBackend(str(out))
    try:
        deterministic(
            time=[init_time],
            nsteps=nsteps,
            prognostic=model,
            data=data,
            io=io,
            output_coords={"variable": np.array(scored)},
            device=device,
        )
    except Exception:
        shutil.rmtree(out, ignore_errors=True)
        raise

    # Record init source for the metrics table (sidecar, not inside the store)
    out.parent.joinpath(out.name + ".init_source").write_text(init_label)
    return out


def main():
    """CLI used by run_range to run the forecast step of models whose
    `conda_env` differs from the orchestrator's env:

      conda run -n <env> python -m scoreboard.forecast \\
          --model graphcast_oper --init 2023-01-15T00
    """
    import argparse

    import yaml

    p = argparse.ArgumentParser(description="Run one model's forecast step")
    p.add_argument("--model", required=True, help="model name from config")
    p.add_argument("--init", required=True, nargs="+",
                   help="init time(s) (ISO, e.g. 2023-01-15T00)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--nsteps", type=int, default=None, help="override forecast steps")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    if args.nsteps:
        cfg["forecast"]["nsteps"] = args.nsteps
    if args.model not in cfg["models"]:
        raise SystemExit(f"Unknown model '{args.model}' — add it to {args.config}")

    inits = [datetime.fromisoformat(t) for t in args.init]
    data_dir = Path(cfg["paths"]["data"])
    model = None
    for init in inits:
        if model is None and not forecast_path(data_dir, args.model, init).exists():
            print(f"[forecast] loading model {args.model} (once)...")
            model = load_model(args.model)
        run_forecast(args.model, init, cfg, model=model)


if __name__ == "__main__":
    main()
