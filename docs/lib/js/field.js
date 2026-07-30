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

export function clearFieldCache() { FieldCache.clear(); }
