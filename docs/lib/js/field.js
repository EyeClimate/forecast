/* PNG field -> Float32Array, plus sampling.
 *
 * The encoding contract is declared in manifest.json per init, not assumed here:
 *
 *   value = scale[0] + (byte - min_byte) * (scale[1] - scale[0]) / levels
 *
 * with byte === encoding.missing reserved for "no data". Reading the formula
 * from the manifest rather than hardcoding it is what lets fields.py change its
 * quantization (16-bit, a log ramp for precipitation) without silently
 * reinterpreting every previously published PNG.
 */

const FieldCache = new Map();   // url -> {w, h, data: Float32Array}

export async function loadField(url, scale, encoding) {
  const key = `${url}|${scale[0]},${scale[1]}`;
  if (FieldCache.has(key)) return FieldCache.get(key);

  const img = await new Promise((res, rej) => {
    const i = new Image();
    i.onload = () => res(i);
    i.onerror = () => rej(new Error(`field not found: ${url}`));
    i.src = url;
  });

  const c = document.createElement("canvas");
  c.width = img.naturalWidth;
  c.height = img.naturalHeight;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, c.width, c.height).data;

  const [lo, hi] = scale;
  const miss = encoding.missing;
  const minByte = encoding.min_byte;
  const levels = encoding.levels;
  const out = new Float32Array(c.width * c.height);
  for (let i = 0, p = 0; i < out.length; i++, p += 4) {
    const b = px[p];                       // single-channel: R carries the value
    out[i] = b === miss ? NaN : lo + ((b - minByte) * (hi - lo)) / levels;
  }

  const f = { w: c.width, h: c.height, data: out };
  FieldCache.set(key, f);
  return f;
}

/* Grid geometry straight from the manifest — never inferred from array shape.
 * An inferred grid is how a north-up field ends up drawn south-up while every
 * numeric check still passes. */
export function gridIndex(grid, lat, lon) {
  const row = (lat - grid.lat_start) / grid.lat_step;
  let l = ((lon - grid.lon_start) % 360 + 360) % 360;
  const col = l / grid.lon_step;
  return { row, col };
}

export function sampleAt(field, grid, lat, lon) {
  const { row, col } = gridIndex(grid, lat, lon);
  if (row < 0 || row > grid.height - 1) return NaN;
  const r0 = Math.floor(row), c0 = Math.floor(col);
  const fr = row - r0, fc = col - c0;
  const r1 = Math.min(r0 + 1, grid.height - 1);
  const c1 = (c0 + 1) % grid.width;               // longitude wraps
  const at = (r, c) => field.data[r * field.w + (c % field.w)];
  const a = at(r0, c0), b = at(r0, c1), c = at(r1, c0), d = at(r1, c1);
  if ([a, b, c, d].some(Number.isNaN)) return at(Math.round(row), Math.round(col));
  return a * (1 - fr) * (1 - fc) + b * (1 - fr) * fc + c * fr * (1 - fc) + d * fr * fc;
}

export function fieldExtent(field) {
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < field.data.length; i++) {
    const v = field.data[i];
    if (Number.isNaN(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return [lo, hi];
}

/* Percentile range of the cells inside a lat/lon box.
 *
 * Percentiles rather than min/max, because min/max is what makes a global t2m
 * field unreadable in the first place: one Antarctic cell at 197 K sets the
 * bottom of a scale that everything else then shares, and the inhabited world
 * collapses into a few adjacent colours. Trimming the tails costs the extremes
 * their own colour and buys structure everywhere else.
 *
 * `bounds` is a Leaflet-style {north, south, west, east} in -180..180. The grid
 * runs 0..360 east from Greenwich, so each column is folded into -180..180
 * before the comparison — the same convention mismatch drawField() handles with
 * its row roll, and getting it wrong here silently samples the antipodes.
 */
export function extentInBounds(field, grid, bounds, pLo = 0.02, pHi = 0.98) {
  const vals = [];
  for (let r = 0; r < field.h; r++) {
    const lat = grid.lat_start + grid.lat_step * r;
    if (lat > bounds.north || lat < bounds.south) continue;
    for (let c = 0; c < field.w; c++) {
      let lon = grid.lon_start + grid.lon_step * c;
      lon = ((lon % 360) + 360) % 360;
      if (lon > 180) lon -= 360;
      const inLon = bounds.west <= bounds.east
        ? lon >= bounds.west && lon <= bounds.east
        : lon >= bounds.west || lon <= bounds.east;   // view straddles the dateline
      if (!inLon) continue;
      const v = field.data[r * field.w + c];
      if (!Number.isNaN(v)) vals.push(v);
    }
  }
  if (!vals.length) return null;
  vals.sort((a, b) => a - b);
  const at = (p) => vals[Math.min(vals.length - 1, Math.max(0, Math.round(p * (vals.length - 1))))];
  return [at(pLo), at(pHi)];
}

export function clearFieldCache() { FieldCache.clear(); }

/* ---------- upsampling ---------- */

/* Catmull-Rom through four samples, evaluated at t in [0,1] between p1 and p2.
 * Interpolating in *value* space and colouring afterwards, rather than
 * interpolating the drawn colours, is what keeps every pixel a colour the ramp
 * actually contains — blending two colours from a non-linear ramp lands
 * between them on the screen but not on the bar. */
const cubic = (p0, p1, p2, p3, t) =>
  p1 + 0.5 * t * (p2 - p0 +
       t * (2 * p0 - 5 * p1 + 4 * p2 - p3 +
       t * (3 * (p1 - p2) + p3 - p0)));

/* Nearest finite of four taps, used where the cubic cannot run. Returning NaN
 * when all four are missing is deliberate: missing data has to stay missing and
 * be drawn transparent, not be filled in with whatever was nearby. */
function fallback(a, b, c, d, t) {
  const near = t < 0.5 ? [b, c, a, d] : [c, b, d, a];
  for (const v of near) if (!Number.isNaN(v)) return v;
  return NaN;
}

/* Bicubic upsample of a decoded field by an integer factor.
 *
 * The overlay used to be handed to Leaflet at the grid's native 360x181 and
 * stretched about 4x by the browser's bilinear filter, which is why the map
 * read as mushy. Resampling here does not add information the 1 deg data does
 * not have — nothing can — but it replaces bilinear's flat facets and diamond
 * artefacts with a smooth surface, and it keeps local maxima from being clipped
 * flat the way linear interpolation clips them.
 *
 * Done separably: a horizontal pass then a vertical one, which is 8 taps per
 * output pixel instead of the 16 a direct 2D kernel would need. At 4x that is
 * the difference between a redraw you notice and one you do not.
 *
 * Longitude wraps and latitude clamps, matching sampleAt(). The field passed in
 * is already rolled to start at -180, so wrapping in x is correct here.
 */
export function upsample(field, factor) {
  if (factor <= 1) return field;
  const { w, h, data } = field;
  const W = w * factor, H = h * factor;

  // Horizontal: w -> W, height unchanged.
  const mid = new Float32Array(W * h);
  for (let y = 0; y < h; y++) {
    const row = y * w;
    for (let X = 0; X < W; X++) {
      const sx = (X + 0.5) / factor - 0.5;
      const x1 = Math.floor(sx);
      const t = sx - x1;
      const at = (i) => data[row + ((i % w) + w) % w];
      const a = at(x1 - 1), b = at(x1), c = at(x1 + 1), d = at(x1 + 2);
      mid[y * W + X] = (Number.isNaN(a) || Number.isNaN(b) || Number.isNaN(c) || Number.isNaN(d))
        ? fallback(a, b, c, d, t)
        : cubic(a, b, c, d, t);
    }
  }

  // Vertical: h -> H.
  const out = new Float32Array(W * H);
  for (let Y = 0; Y < H; Y++) {
    const sy = (Y + 0.5) / factor - 0.5;
    const y1 = Math.floor(sy);
    const t = sy - y1;
    const clamp = (i) => Math.min(h - 1, Math.max(0, i));
    const r0 = clamp(y1 - 1) * W, r1 = clamp(y1) * W,
          r2 = clamp(y1 + 1) * W, r3 = clamp(y1 + 2) * W;
    for (let X = 0; X < W; X++) {
      const a = mid[r0 + X], b = mid[r1 + X], c = mid[r2 + X], d = mid[r3 + X];
      out[Y * W + X] = (Number.isNaN(a) || Number.isNaN(b) || Number.isNaN(c) || Number.isNaN(d))
        ? fallback(a, b, c, d, t)
        : cubic(a, b, c, d, t);
    }
  }
  return { w: W, h: H, data: out };
}
