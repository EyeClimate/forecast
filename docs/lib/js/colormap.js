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
 *
 * WHERE THE MAGNITUDE RULE HAS AN EXCEPTION, AND WHY.
 *
 * The single-hue rule assumes the reader arrives with no prior expectation
 * about what colour a value should be, so any ordered ramp will do and the
 * perceptually uniform one is best. That assumption is false for a handful of
 * weather variables, and temperature is the extreme case: every reader already
 * knows cold is blue and hot is red. Viridis says cold is dark purple and hot
 * is yellow. Drawing t2m in viridis does not merely look unconventional — it
 * asks the reader to suppress a mapping they cannot suppress, and the map stops
 * being read as weather.
 *
 * So the ramp is chosen per variable, and each choice below has to justify
 * itself against the default rather than the other way round. Variables with no
 * established convention still get viridis; error views always get the
 * diverging ramp regardless of variable, because there the polarity rule
 * outranks any per-variable convention.
 *
 * Assignment lives in config.yaml (`display.map_palettes`) and reaches the page
 * through manifest.json, so this file holds the ramps and not the policy.
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
// Still the right choice for any variable the reader has no prior expectation
// about, which is why it remains the fallback.
export const SEQUENTIAL = ramp([
  [68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142],
  [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37],
]);

/* Temperature: the conventional cold-to-hot meteorological ramp, violet
 * through blue and green to red. Spread to cover a *global* field, roughly
 * -75 °C to +45 °C, rather than the narrow range a regional chart would use —
 * the pale band sits near freezing, so the 0 °C line is legible without being
 * drawn. Deliberately not perceptually uniform: matching the reader's existing
 * colour semantics is worth more here than uniform lightness steps. */
const TEMPERATURE = ramp([
  [ 60,   0,  90], [ 40,  40, 160], [ 30,  90, 200], [ 60, 150, 225],
  [120, 195, 235], [190, 225, 235], [170, 215, 150], [240, 230, 140],
  [245, 180,  80], [230, 110,  60], [170,  30,  45],
]);

/* Precipitation: white through green and blue to magenta, the accumulation
 * convention. Starting at near-white rather than at a saturated colour is the
 * point — precip is mostly zero, and a ramp whose low end is vivid paints the
 * entire dry world as though something were happening there. */
const PRECIP = ramp([
  [252, 253, 251], [214, 240, 210], [150, 214, 160], [ 68, 170, 105],
  [ 60, 175, 195], [ 40, 105, 180], [ 85,  60, 175], [150,  45, 155],
  [205,  35,  85],
]);

/* Wind speed: calm to storm. Low end near-white for the same reason as precip —
 * most of the field is unremarkable and should recede — climbing through teal
 * and yellow into red and finally violet for the jet-stream tail. */
const WIND = ramp([
  [244, 250, 251], [186, 222, 232], [126, 202, 194], [125, 205, 130],
  [228, 224, 115], [240, 168,  72], [224,  92,  62], [172,  35,  62],
  [118,  22,  92],
]);

/* Named ramps config.yaml's `display.map_palettes` can select. A name absent
 * from here falls back to viridis rather than throwing: fields.py can export a
 * variable before this file has an opinion about it, and an unconventional ramp
 * is a cosmetic problem where a blank map is not. */
export const PALETTES = {
  viridis: SEQUENTIAL,
  temperature: TEMPERATURE,
  precip: PRECIP,
  wind: WIND,
};

/* Palettes whose colours carry an absolute meaning, not merely an order.
 *
 * A viridis map says "this cell is higher than that one". A temperature map
 * says "this cell is about freezing", and it says that because the reader is
 * decoding blue and red directly, without consulting the bar. That only holds
 * while the ramp is pinned to a fixed range of values.
 *
 * So these palettes are the ones that stretch-to-view damages: rescaling to the
 * 2nd-98th percentile of a summer European view paints +9 C with the colour the
 * reader has learned means -75 C. The scale is still labelled correctly and the
 * numbers are still right — but the pre-existing convention that justified
 * choosing this ramp over viridis is exactly what has been given up, so the map
 * has to say so rather than let the reader keep decoding colours the old way.
 */
const ANCHORED = new Set(["temperature"]);

export const isAnchored = (palette) => ANCHORED.has(palette);

// Cool -> neutral grey -> warm. Grey, not white, so the midpoint stays visible
// against a light surface.
export const DIVERGING = ramp([
  [5, 48, 97], [33, 102, 172], [67, 147, 195], [146, 197, 222], [209, 229, 240],
  [235, 235, 235],
  [253, 219, 199], [244, 165, 130], [214, 96, 77], [178, 24, 43], [103, 0, 31],
]);

/** The ramp a view actually draws with. Error always wins: polarity outranks
 *  any per-variable colour convention. */
export function rampFor(kind, palette) {
  if (kind === "error") return DIVERGING;
  return PALETTES[palette] || SEQUENTIAL;
}

/** Sequential: linear over [lo, hi], in the named palette. */
export function sequentialScale(lo, hi, palette) {
  const r = PALETTES[palette] || SEQUENTIAL;
  return (v) => r(hi === lo ? 0.5 : (v - lo) / (hi - lo));
}

/** Diverging: symmetric about zero regardless of the values supplied.
 *  Passing an asymmetric range still yields a ramp centred on zero. */
export function divergingScale(lo, hi) {
  const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
  return (v) => DIVERGING(0.5 + Math.max(-1, Math.min(1, v / m)) * 0.5);
}

export function scaleFor(kind, lo, hi, palette) {
  return kind === "error" ? divergingScale(lo, hi) : sequentialScale(lo, hi, palette);
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

/* Bake a scale into a 256-entry lookup table.
 *
 * Colouring a 1440x724 raster by calling the scale per pixel means a million
 * closure calls each allocating a three-element array, which measured at 45 ms
 * a redraw — half the cost of drawing a frame, spent entirely on garbage. A LUT
 * turns that into one multiply and an index.
 *
 * 256 entries lose nothing: the fields arrive as 8-bit quantized PNGs, so the
 * source cannot distinguish more than 256 levels across its scale in the first
 * place. Under stretch-to-view the displayed range is narrower still, so the
 * table is finer than the data either way.
 */
export function buildLUT(scale, lo, hi) {
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const rgb = scale(lo + ((hi - lo) * i) / 255);
    lut[i * 3] = rgb[0]; lut[i * 3 + 1] = rgb[1]; lut[i * 3 + 2] = rgb[2];
  }
  return lut;
}
