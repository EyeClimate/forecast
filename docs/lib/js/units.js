/* Display units.
 *
 * The pipeline stores SI throughout — Kelvin, Pascals, m2/s2 — because that is
 * what the source datasets carry and what the metrics are computed in. Those
 * are storage units, not display units: a colourbar reading "197 227 257 287
 * 317 K" is a data file leaking onto a web page. Conversion happens here, at
 * render time only. Nothing in data/, docs/data/ or metrics.parquet changes.
 *
 * ABSOLUTE AND DELTA CONVERSIONS ARE SEPARATE, and that is the whole reason
 * this file is not three inline expressions. The map draws two kinds of number
 * from the same variable:
 *
 *   forecast view   an absolute value      288 K   -> 14.9 °C
 *   error view      a difference           2 K     ->  2.0 °C,  NOT -271.2 °C
 *
 * Any unit with a non-zero offset (°C, °F) converts differently in the two
 * cases. Applying the absolute conversion to an error field would draw a
 * plausible-looking map of numbers that are wrong by 273, and nothing about the
 * rendering would look broken. So the two are distinct functions per unit and
 * the caller must say which it wants; there is no default.
 */

const IDENTITY = (v) => v;

/* storedUnit -> system -> conversion.
 *
 * `decimals` is the sensible precision *in the display unit*: manifest.json's
 * per-variable `decimals` describes the stored unit, and 2 decimals of Kelvin
 * is not 2 decimals of Celsius worth showing. */
const TABLE = {
  K: {
    metric:   { label: "°C",  abs: (v) => v - 273.15,          delta: IDENTITY,          decimals: 1 },
    imperial: { label: "°F",  abs: (v) => v * 1.8 - 459.67,    delta: (v) => v * 1.8,    decimals: 1 },
  },
  Pa: {
    metric:   { label: "hPa", abs: (v) => v / 100,             delta: (v) => v / 100,    decimals: 1 },
    imperial: { label: "inHg", abs: (v) => v / 3386.389,       delta: (v) => v / 3386.389, decimals: 2 },
  },
  // Geopotential -> geopotential height in decametres, the form every synoptic
  // chart labels 500 hPa contours in. Z/g, then metres to decametres.
  "m2/s2": {
    metric:   { label: "dam", abs: (v) => v / 98.0665,         delta: (v) => v / 98.0665, decimals: 0 },
    imperial: { label: "dam", abs: (v) => v / 98.0665,         delta: (v) => v / 98.0665, decimals: 0 },
  },
  "m/s": {
    metric:   { label: "m/s", abs: IDENTITY,                   delta: IDENTITY,          decimals: 1 },
    imperial: { label: "mph", abs: (v) => v * 2.236936,        delta: (v) => v * 2.236936, decimals: 1 },
  },
  "mm/6h": {
    metric:   { label: "mm/6h", abs: IDENTITY,                 delta: IDENTITY,          decimals: 1 },
    imperial: { label: "in/6h", abs: (v) => v / 25.4,          delta: (v) => v / 25.4,   decimals: 2 },
  },
};

export const SYSTEMS = ["metric", "imperial"];
const STORE_KEY = "scoreboard.units";

export function loadSystem() {
  try {
    const s = localStorage.getItem(STORE_KEY);
    return SYSTEMS.includes(s) ? s : "metric";
  } catch {
    return "metric";                 // private browsing / storage disabled
  }
}

export function saveSystem(system) {
  try { localStorage.setItem(STORE_KEY, system); } catch { /* not fatal */ }
}

/* A variable whose stored unit is not in TABLE passes straight through with its
 * own label. Falling back rather than throwing matters: fields.py can add a
 * variable before this table knows about it, and an unconverted unit is a
 * cosmetic problem where a crashed map is not. */
export function unitFor(storedUnit, system) {
  const row = TABLE[storedUnit];
  if (!row) return { label: storedUnit || "", abs: IDENTITY, delta: IDENTITY, decimals: 2 };
  return row[system] || row.metric;
}

/** Convert one value. `kind` is the map's view kind: "error" values are
 *  differences, everything else is absolute. */
export function toDisplay(value, storedUnit, system, kind) {
  const u = unitFor(storedUnit, system);
  return kind === "error" ? u.delta(value) : u.abs(value);
}

/** Convert and format in one step, at the display unit's own precision. */
export function formatDisplay(value, storedUnit, system, kind, decimals) {
  const u = unitFor(storedUnit, system);
  if (!Number.isFinite(value)) return "—";
  const d = decimals == null ? u.decimals : decimals;
  return (kind === "error" ? u.delta(value) : u.abs(value)).toFixed(d);
}
