/* Colormaps for the field map.
 *
 * Two jobs, two rules (dataviz):
 *   magnitude  -> ONE hue, light to dark. Never a rainbow: a rainbow ramp has
 *                 no perceptual ordering, so readers invent boundaries where
 *                 the data has none.
 *   polarity   -> TWO hues with a NEUTRAL GREY midpoint pinned at zero. The
 *                 midpoint must sit exactly at zero — an error map whose
 *                 neutral colour drifts off zero misstates the sign of a bias,
 *                 which is the one thing the map exists to show.
 */

const lerp = (a, b, t) => a + (b - a) * t;

function ramp(stops) {
  return (t) => {
    const x = Math.max(0, Math.min(1, t)) * (stops.length - 1);
    const i = Math.min(Math.floor(x), stops.length - 2);
    const f = x - i;
    const a = stops[i], b = stops[i + 1];
    return [Math.round(lerp(a[0], b[0], f)),
            Math.round(lerp(a[1], b[1], f)),
            Math.round(lerp(a[2], b[2], f))];
  };
}

// Viridis, subsampled. Perceptually uniform and colourblind-safe — the reason
// it is the default scientific sequential ramp rather than a matter of taste.
export const SEQUENTIAL = ramp([
  [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142],
  [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37],
]);

// Cool -> neutral grey -> warm. Grey, not white, so the midpoint stays visible
// against a light surface.
export const DIVERGING = ramp([
  [5, 48, 97], [33, 102, 172], [67, 147, 195], [146, 197, 222], [209, 229, 240],
  [235, 235, 235],
  [253, 219, 199], [244, 165, 130], [214, 96, 77], [178, 24, 43], [103, 0, 31],
]);

/** Sequential: linear over [lo, hi]. */
export function sequentialScale(lo, hi) {
  return (v) => SEQUENTIAL(hi === lo ? 0.5 : (v - lo) / (hi - lo));
}

/** Diverging: symmetric about zero regardless of the values supplied.
 *  Passing an asymmetric range still yields a ramp centred on zero. */
export function divergingScale(lo, hi) {
  const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
  return (v) => DIVERGING(0.5 + Math.max(-1, Math.min(1, v / m)) * 0.5);
}

export function scaleFor(kind, lo, hi) {
  return kind === "error" ? divergingScale(lo, hi) : sequentialScale(lo, hi);
}

/** Tick values for the colorbar. Error bars always show an explicit 0. */
export function colorbarTicks(kind, lo, hi, n = 5) {
  if (kind === "error") {
    const m = Math.max(Math.abs(lo), Math.abs(hi));
    return [-m, -m / 2, 0, m / 2, m];
  }
  const out = [];
  for (let i = 0; i < n; i++) out.push(lo + ((hi - lo) * i) / (n - 1));
  return out;
}

export const rgbCss = (c) => `rgb(${c[0]},${c[1]},${c[2]})`;
