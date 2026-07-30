/* Unit conversion gate for docs/lib/js/units.js.
 *
 * The case this exists for: an error field is a *difference*, so an offset unit
 * must not apply its offset. A 2 K error is a 2 °C error, not -271.15 °C. That
 * mistake produces a map that renders perfectly and is wrong by 273 everywhere,
 * with no visual tell at all — which is exactly the kind of thing a test has to
 * catch instead of a reader.
 *
 * Run with `npm run check:units`.
 */

import { unitFor, toDisplay, formatDisplay } from "../docs/lib/js/units.js";

let failures = 0;

function close(name, got, want, tol = 1e-6) {
  const ok = Number.isFinite(got) && Math.abs(got - want) <= tol;
  if (!ok) { failures++; console.error(`FAIL ${name}: got ${got}, want ${want}`); }
  else console.log(`  ok  ${name}`);
}

function eq(name, got, want) {
  const ok = got === want;
  if (!ok) { failures++; console.error(`FAIL ${name}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`); }
  else console.log(`  ok  ${name}`);
}

console.log("absolute conversions (forecast view)");
close("288.15 K -> °C", toDisplay(288.15, "K", "metric", "forecast"), 15);
close("288.15 K -> °F", toDisplay(288.15, "K", "imperial", "forecast"), 59);
close("273.15 K -> °C", toDisplay(273.15, "K", "metric", "forecast"), 0);
close("101325 Pa -> hPa", toDisplay(101325, "Pa", "metric", "forecast"), 1013.25);
// 54000 / 9.80665 = 5506.47 m = 550.647 dam — a typical 500 hPa height.
close("54000 m2/s2 -> dam", toDisplay(54000, "m2/s2", "metric", "forecast"), 550.6468, 1e-3);

console.log("delta conversions (error view) — the offset must NOT be applied");
close("2 K error -> °C", toDisplay(2, "K", "metric", "error"), 2);
close("-2 K error -> °C", toDisplay(-2, "K", "metric", "error"), -2);
close("2 K error -> °F", toDisplay(2, "K", "imperial", "error"), 3.6);
close("0 K error -> °C", toDisplay(0, "K", "metric", "error"), 0);
close("100 Pa error -> hPa", toDisplay(100, "Pa", "metric", "error"), 1);

console.log("zero stays zero on the error view for every unit and system");
for (const u of ["K", "Pa", "m2/s2", "m/s", "mm/6h"]) {
  for (const sys of ["metric", "imperial"]) {
    close(`0 ${u} error (${sys})`, toDisplay(0, u, sys, "error"), 0);
  }
}

console.log("sign is preserved on the error view");
for (const u of ["K", "Pa", "m2/s2", "m/s", "mm/6h"]) {
  for (const sys of ["metric", "imperial"]) {
    const got = toDisplay(-5, u, sys, "error");
    if (!(got < 0)) { failures++; console.error(`FAIL sign ${u}/${sys}: got ${got}`); }
    else console.log(`  ok  sign ${u}/${sys}`);
  }
}

console.log("labels");
eq("K metric label", unitFor("K", "metric").label, "°C");
eq("K imperial label", unitFor("K", "imperial").label, "°F");
eq("Pa metric label", unitFor("Pa", "metric").label, "hPa");

console.log("unknown units pass through rather than throwing");
eq("unknown label", unitFor("furlongs", "metric").label, "furlongs");
close("unknown value", toDisplay(7, "furlongs", "metric", "forecast"), 7);
eq("missing unit label", unitFor(undefined, "metric").label, "");

console.log("formatting");
eq("format 288.15 K", formatDisplay(288.15, "K", "metric", "forecast"), "15.0");
eq("format NaN", formatDisplay(NaN, "K", "metric", "forecast"), "—");

if (failures) { console.error(`\n${failures} failure(s)`); process.exit(1); }
console.log("\nall unit conversions ok");
