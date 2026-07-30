#!/usr/bin/env python
"""Vendor Natural Earth geography into docs/lib/vendor/.

The map draws no basemap tiles (see docs/lib/js/map.js), so its entire sense of
place comes from these files. They are committed rather than fetched at runtime
for the same reason: the page makes no external requests.

Raw Natural Earth 50 m is 2.3 MB across the two layers, which is too much to
pull on every page load. Simplification here is not a quality compromise, it is
matching the data to the display: the map's maxZoom is 6 on EPSG:4326, where one
pixel is about 0.02 deg, so coordinates rounded to 2 decimal places are already
sub-pixel. Everything past that is bytes the reader pays for and cannot see.

Run after changing the map's maxZoom, and re-check the assumption above if it
goes up.

    conda run -n earth2 python scripts/vendor_geography.py
"""

from __future__ import annotations

import gzip
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"
VENDOR = Path(__file__).resolve().parent.parent / "docs" / "lib" / "vendor"

# (remote name, local name, minimum bounding-box span in degrees to keep)
#
# The span filter drops specks. Natural Earth 50 m carries islands a few hundred
# metres across; against a 1 deg field they are smaller than one data cell, so
# they cost bytes and add nothing a reader could use to orient. Borders get no
# filter — a short border segment is usually a real one between two countries,
# not a rounding artefact.
LAYERS = [
    ("ne_50m_coastline", "coastlines-50m.json", 0.15),
    ("ne_50m_admin_0_boundary_lines_land", "borders-50m.json", 0.0),
]

DECIMALS = 2

# Douglas-Peucker tolerance in degrees. maxZoom 6 on EPSG:4326 is ~0.02 deg per
# pixel, so a vertex displaced by less than this cannot move the drawn line by a
# whole pixel at the deepest zoom the map allows.
TOLERANCE = 0.02


def _dp(pts: list, tol: float) -> list:
    """Douglas-Peucker, iterative so a 20k-vertex coastline cannot blow the
    recursion limit."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]
        bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        worst, at = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if norm == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > worst:
                worst, at = d, k
        if worst > tol:
            keep[at] = True
            stack.append((i, at))
            stack.append((at, j))
    return [p for p, k in zip(pts, keep) if k]


def _span(coords: list) -> float:
    """Largest bounding-box side of a list of points, in degrees."""
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _round_ring(coords: list) -> list:
    """Round to DECIMALS and drop points that collapse onto their predecessor.

    Rounding alone barely helps: a dense coastline has many vertices inside one
    0.01 deg cell, and they all survive as duplicates. Dropping the repeats is
    where most of the saving comes from.
    """
    out = []
    for pt in coords:
        p = [round(pt[0], DECIMALS), round(pt[1], DECIMALS)]
        if not out or p != out[-1]:
            out.append(p)
    return out


def _line(coords: list, min_span: float) -> list | None:
    """Simplify, round, and reject one line. Order matters: simplify first so
    Douglas-Peucker sees the true geometry, then round to the display grid."""
    if len(coords) < 2:
        return None
    if min_span and _span(coords) < min_span:
        return None
    c = _round_ring(_dp(coords, TOLERANCE))
    return c if len(c) >= 2 else None


def _simplify_geometry(geom: dict, min_span: float) -> dict | None:
    t = geom.get("type")
    if t == "LineString":
        c = _line(geom["coordinates"], min_span)
        return {"type": t, "coordinates": c} if c else None
    if t == "MultiLineString":
        parts = [p for p in (_line(q, min_span) for q in geom["coordinates"]) if p]
        return {"type": t, "coordinates": parts} if parts else None
    return None                      # the layers we vendor are lines only


def fetch(name: str) -> dict:
    url = f"{BASE}/{name}.geojson"
    print(f"[vendor] fetching {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    VENDOR.mkdir(parents=True, exist_ok=True)
    for remote, local, min_span in LAYERS:
        raw = fetch(remote)
        feats = []
        for f in raw.get("features", []):
            g = _simplify_geometry(f.get("geometry") or {}, min_span)
            if g is None:
                continue
            # Properties are dropped wholesale. Nothing on the page styles or
            # labels by attribute, and they are a large share of the bytes.
            feats.append({"type": "Feature", "properties": {}, "geometry": g})
        out = VENDOR / local
        payload = {"type": "FeatureCollection", "features": feats}
        body = json.dumps(payload, separators=(",", ":"))
        out.write_text(body)
        kb = out.stat().st_size / 1024
        # Report the transferred size too. GitHub Pages gzips, and geojson is
        # mostly repeated digits and punctuation, so it compresses about 4x —
        # judging the cost by the on-disk number overstates it badly.
        gz = len(gzip.compress(body.encode("utf-8"), 9)) / 1024
        print(f"[vendor] {local}: {len(feats)} features, {kb:.0f} KB on disk, "
              f"{gz:.0f} KB gzipped")
        if gz > 250:
            print(f"[vendor] WARNING {local} transfers {gz:.0f} KB — every "
                  f"visitor pays this on first paint", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
