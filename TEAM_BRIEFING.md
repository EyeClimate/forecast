# Forecast Scoreboard — Team Briefing

*A plain-language guide to what we built, the AI weather models we compared, how
we scored them, and what the results say. No meteorology or ML background
assumed. (Written 2026-07-30; numbers reflect the 38-init evaluation described
below.)*

Live results page: https://eyeclimate.github.io/forecast/

---

## 1. The elevator pitch

Over the last three years, AI models have learned to forecast the weather —
and they now rival the physics-based supercomputer models that national weather
agencies have refined for 50 years, while running in *minutes on one GPU*
instead of hours on a supercomputer.

Many labs claim their model is best. We built a **scoreboard** that settles the
question on our own hardware: every model gets the exact same starting
snapshot of the atmosphere, forecasts 5 days ahead, and is graded against what
actually happened, with the same metrics for everyone. Think of it as a
**time-trial race**: same start line, same course, same stopwatch.

**Headline result:** on our 38-day test, ECMWF's **AIFS** and Shanghai AI Lab's
**FengWu** are neck-and-neck at the top for large-scale accuracy; **AIFS,
FuXi, and GraphCast** lead on rain prediction. Every AI model beats the
no-skill baseline by an order of magnitude.

---

## 2. Background: how weather forecasting works (2 minutes)

- The atmosphere is a fluid. Traditional forecasting ("numerical weather
  prediction", NWP) simulates its physics forward in time from a snapshot of
  current conditions. The world's best NWP system is run by **ECMWF** (the
  European Centre for Medium-Range Weather Forecasts).
- Since 2022, deep-learning models trained on 40+ years of historical weather
  have learned to do the same thing *statistically*: given the state of the
  atmosphere now (and 6 hours ago, for some models), predict the state 6 hours
  from now. Apply that step repeatedly ("autoregressively") and you get a
  5-, 10-, or 15-day forecast.
- All models here work on the same canvas: a **0.25° latitude-longitude grid**
  (~25 km squares, 721 × 1440 points covering the whole globe), with dozens of
  variables per point (temperature, winds, pressure, humidity at many
  altitudes).
- Forecast quality is always reported **as a function of lead time** — how far
  ahead the forecast looks. Everyone is nearly perfect at +6 hours; the
  interesting question is who degrades slowest out to +120 hours (5 days).

---

## 3. The experiment

| Design choice | What we did | Why |
|---|---|---|
| Starting snapshots ("initial conditions") | **ERA5 reanalysis** — the same snapshot for every model | Removes the "who got better input data" confounder |
| Test dates | **38 forecast start times ("inits")**: every day of January 2023 (31) + July 1–7, 2023 (7), all at 00 UTC | Winter + a summer week; out-of-training-sample for all models |
| Forecast length | 20 steps × 6 h = **5 days** | The "medium-range" horizon where forecasts are hardest and most valuable |
| Ground truth | **ERA5** again, at each forecast's valid time | The community-standard reference for what "actually happened" |
| Scale | 640,480 individual scores in the database | Every (model × start time × lead × variable × region × metric) combination |

**What is ERA5?** ECMWF's "reanalysis" — a physics model continuously corrected
with every available real-world observation (satellites, weather balloons,
stations, aircraft), producing the best available hourly reconstruction of the
actual global atmosphere from 1940 to now. It's the dataset most of these AI
models were trained on, and the standard ground truth for scoring them
(this is also how the WeatherBench2 benchmark, the academic standard, does it).

**The baseline.** We also "run" **persistence** — the forecast that tomorrow
equals today, i.e. the starting snapshot copied forward unchanged. It has zero
skill by construction. Any model worth running must beat it decisively; it
marks the floor of the score charts.

---

## 4. The contenders (10 models)

All run at the same 0.25° resolution with 6-hour steps, inside NVIDIA's
**Earth2Studio** framework on our own 4× RTX 6000 Ada (48 GB) box.

| Model | Who made it | Family / idea | Predicts rain? |
|---|---|---|---|
| **AIFS** | ECMWF (the world's leading weather agency) | Graph + transformer, their operational AI system | Yes, natively |
| **FengWu** | Shanghai AI Lab | Transformer, multi-model ensemble-style training | No |
| **GraphCast** (operational) | Google DeepMind | Graph neural network on a sphere mesh; the 2023 *Science* paper that put AI weather on the map. We run the operational variant, fine-tuned on ECMWF's live analyses | Yes, natively |
| **FuXi** | Fudan University | Cascade of transformers (one per lead-time range) | Yes, natively |
| **Pangu-Weather** (6 h) | Huawei | 3D transformer; the 2023 *Nature* paper | Via add-on (see below) |
| **Aurora** | Microsoft | Large "foundation model" for the atmosphere | Via add-on |
| **Atlas** | NVIDIA (2026) | Generative (produces sharp, realistic-looking fields) | Yes, natively |
| **FourCastNet 3 (FCN3)** | NVIDIA | Spherical neural operator, probabilistic-capable (we run one member) | No |
| **SFNO** | NVIDIA | Spherical Fourier neural operator (elegant math, older generation) | No |
| **Persistence** | — | "Tomorrow = today" no-skill baseline | Copies the start |

**The rain add-on ("diagnostic chain").** Aurora and Pangu don't output
precipitation. For them we bolt on a separate NVIDIA neural network
(PrecipitationAFNOv2) that estimates 6-hour rainfall from their predicted
atmospheric state — deriving two missing inputs (surface pressure, total
column water vapor) along the way. This tests a useful pattern — *any*
forecast model can be extended with downstream "diagnostic" models — but as
you'll see, native rain prediction wins.

---

## 5. How we score (the metrics, in plain words)

We score 6 **state variables** and precipitation, at every 6-hour lead, over 4
regions (Global, Northern-hemisphere extratropics, Tropics,
Southern-hemisphere extratropics).

**The variables** (chosen because they're the community's standard yardsticks):

- `z500` — the height of the 500 hPa pressure surface (~5.5 km altitude). The
  classic measure of whether you got the **large-scale weather pattern**
  (highs, lows, jet stream) right. The headline variable in every model
  intercomparison.
- `t2m` — air temperature at 2 m (what a thermometer in your garden reads).
- `t850` — temperature at ~1.5 km, above surface noise.
- `msl` — sea-level pressure (storm intensity and position).
- `u10m`, `v10m` — east-west and north-south wind at 10 m.
- `tp06` — precipitation accumulated over 6 hours.

**The scores:**

- **RMSE** (root-mean-square error): average size of the error, in the
  variable's own units (Kelvin for temperature, etc.). **Lower is better.**
  We weight by latitude so the vast tropics count proportionally to their
  area (grid cells shrink toward the poles).
- **Bias**: the systematic part of the error — does the model run warm/cold,
  or over/under-predict? Closer to zero is better.
- **ACC** (anomaly correlation coefficient): correlation between the
  forecast's *departure from climatology* and the real departure —
  "did you predict the pattern of unusual weather, not just the seasonal
  average?" 1.0 is perfect; by convention **ACC ≥ 0.6 is a "useful"
  forecast**, and staying above ~0.9 at day 5 is elite. Climatology comes
  from the WeatherBench2 reference dataset.
- **CSI** (critical success index), for rain: treat "≥ 1 mm in 6 h" (also 5,
  10 mm) as an event. CSI = hits ÷ (hits + false alarms + misses). 1.0 is
  perfect, 0 is useless. Harsh but honest — you can't score well by always
  or never predicting rain.
- **FSS** (fractions skill score), for rain: like CSI but forgives small
  position errors by comparing rain *coverage within ~250 km neighborhoods*.
  Rewards "right storm, slightly displaced" — which is genuinely useful —
  where CSI punishes it.

---

## 6. Results

*(Global, latitude-weighted, averaged over all 38 start dates. Arrows show
which direction is better.)*

### Large-scale pattern: z500 RMSE (m²/s², ↓ better)

| Model | +24 h | +72 h | +120 h |
|---|---|---|---|
| **FengWu** | **40.0** | **123.2** | **264.3** |
| AIFS | 43.5 | 125.9 | 266.0 |
| FuXi | 45.6 | 133.6 | 281.6 |
| GraphCast | 46.7 | 134.5 | 291.9 |
| Pangu | 46.2 | 138.6 | 302.7 |
| Aurora | 47.5 | 150.8 | 308.6 |
| FCN3 | 58.1 | 172.5 | 355.6 |
| SFNO | 64.2 | 204.8 | 400.4 |
| *Atlas\** | *50.4* | *138.2* | *266.1* |
| Persistence | 576.3 | 913.1 | 1036.1 |

### Surface temperature: t2m RMSE (Kelvin, ↓ better)

| Model | +24 h | +72 h | +120 h |
|---|---|---|---|
| **FengWu** | 0.73 | **1.05** | **1.46** |
| AIFS | 0.78 | 1.08 | 1.49 |
| Pangu | 0.77 | 1.14 | 1.67 |
| GraphCast | 0.92 | 1.28 | 1.72 |
| FuXi | 1.01 | 1.33 | 1.75 |
| Aurora | **0.73** | 1.25 | 1.78 |
| FCN3 | 1.08 | 1.48 | 2.01 |
| SFNO | 0.95 | 1.51 | 2.16 |
| *Atlas\** | *0.96* | *1.33* | *1.84* |
| Persistence | 2.20 | 3.18 | 3.57 |

### Pattern skill: z500 ACC (↑ better; > 0.6 = "useful", > 0.9 at day 5 = elite)

| Model | +24 h | +72 h | +120 h |
|---|---|---|---|
| AIFS / FengWu | 0.999 | 0.988 | **0.944** |
| FuXi | 0.998 | 0.986 | 0.937 |
| GraphCast | 0.998 | 0.986 | 0.932 |
| Pangu | 0.998 | 0.985 | 0.926 |
| Aurora | 0.998 | 0.982 | 0.925 |
| FCN3 | 0.997 | 0.977 | 0.901 |
| SFNO | 0.997 | 0.968 | 0.873 |
| Persistence | 0.752 | 0.374 | 0.194 |

### Rain: CSI at ≥ 1 mm / 6 h (↑ better)

| Model | +24 h | +72 h | +120 h | Rain source |
|---|---|---|---|---|
| **AIFS** | **0.68** | **0.53** | **0.42** | native |
| FuXi | 0.67 | 0.51 | 0.39 | native |
| GraphCast | 0.64 | 0.50 | 0.38 | native |
| *Atlas\** | *0.61* | *0.44* | *0.32* | native |
| Aurora | 0.35 | 0.29 | 0.23 | add-on chain |
| Pangu | 0.32 | 0.29 | 0.24 | add-on chain |
| Persistence | 0.07 | 0.05 | 0.05 | copied |

*(FSS tells the same story with friendlier numbers: AIFS 0.93 → 0.72 across
24 → 120 h.)*

*\*Atlas has been scored on only 1 of the 38 start dates so far (its full
backfill is pending — it's the slowest model to run). Its numbers are
indicative, not comparable, and are excluded from rankings.*

### What to take away

1. **Every AI model demolishes the baseline** — 10–25× lower error than
   persistence, ACC 0.94 vs 0.19 at day 5. AI weather forecasting is real.
2. **AIFS and FengWu are the class of the field** on large-scale skill,
   essentially tied (their gap is far smaller than day-to-day noise).
3. **Native rain prediction beats bolted-on rain.** The three models that
   output precipitation directly (AIFS, FuXi, GraphCast) form a clear top
   tier; the diagnostic-chain route (Aurora, Pangu) reaches only about half
   their CSI. Still useful — but if you care about rain, choose a
   rain-native model.
4. **Model generations are visible.** SFNO and FCN3 (earlier architectures /
   different design goals) trail the 2023-generation flagships everywhere.
5. **A caution on hidden bugs:** FengWu initially posted absurd scores
   (errors 25× worse than the baseline). The cause was a GPU stream-
   synchronization race in the model wrapper — an infrastructure bug, not a
   model flaw — that corrupted forecasts at random steps. After a two-line
   fix it jumped to the top of the board. Moral: a scoreboard also
   stress-tests your *plumbing*, and "too bad to be true" deserves the same
   scrutiny as "too good to be true".

---

## 7. Honest limitations (say these out loud when presenting)

- **38 winter-heavy days from one year.** Rankings between closely-matched
  models (AIFS vs FengWu, FuXi vs GraphCast) are within noise; the
  tier-level conclusions are robust. Error bars / significance tests are the
  next planned step.
- **ERA5-in, ERA5-out favors ERA5-trained models.** All models start from and
  are graded against ERA5. GraphCast-operational and AIFS are tuned for
  *operational* (real-time) inputs, so this historical setup slightly
  undersells them. A real-time regime (start from live GFS analyses, verify
  against observations) is on the roadmap.
- **Scoring against reanalysis, not raw observations.** Standard practice,
  but ERA5 has its own imperfections, especially for precipitation. Plan:
  add NASA IMERG satellite rainfall as an observational truth.
- **Deterministic forecasts only.** Several models (FCN3, AIFS, Atlas) are
  designed to produce *ensembles* (many possible futures with
  probabilities); we currently grade a single run, which undersells them.
- **Atlas is incomplete** (1 of 38 dates scored so far).

---

## 8. What's on the website

https://eyeclimate.github.io/forecast/

- **Leaderboard** — per-variable rankings at +24/72/120 h, switchable by
  region and metric. Always shows the full roster.
- **Skill vs lead time** — the "who degrades slowest" curves, per variable.
- **Precipitation skill** — CSI / FSS / RMSE at 1, 5, 10 mm thresholds.
- **Model toggle** — chips to add/remove models from the charts (defaults to
  a readable headline subset; 10 lines at once is soup).
- **Methodology** — the fine print, on-page.

Everything regenerates from one metrics database (a 640k-row parquet file);
the whole pipeline — forecast, verify, publish — is one command per date
range and is idempotent (re-runs skip finished work).

---

## 9. Where this goes next ("downstream tasks")

The same scored-forecast infrastructure extends beyond "was the temperature
right":

- **Real-time daily mode** — forecast every morning from live GFS analyses,
  verify as observations arrive, auto-publish. (The scoreboard becomes a
  living dashboard, and operationally-tuned models get a fairer test.)
- **Statistical rigor** — error bars across start dates; head-to-head
  significance tests per model pair.
- **Tropical cyclone tracks** — detect storm centers in forecasts, score
  position error (km) vs the official best-track record. High-stakes,
  high-interest.
- **Wind energy** — 100 m winds (already output by several models) scored at
  wind-farm locations; gust diagnostics.
- **Extremes** — heat-wave hit rates (e.g. CSI on t2m > 35 °C), heavy-rain
  thresholds beyond 10 mm.
- **Ensembles** — probabilistic scoring (CRPS) for the models that support
  it; "chance of rain" instead of "rain: yes/no".
- **More models** — the registry makes adding one a config entry (per-model
  Python environments are already handled — JAX-based and
  custom-dependency models run in isolated envs, orchestrated from one
  command).

---

## 10. Mini-glossary

| Term | Meaning |
|---|---|
| Init / initial condition | The atmosphere snapshot a forecast starts from |
| Lead time | How far ahead a forecast looks (+24 h, +120 h, …) |
| Valid time | The real-world moment a forecast is *about* (init + lead) |
| Reanalysis (ERA5) | Best-available reconstruction of past weather, physics + observations blended |
| 0.25° grid | ~25 km resolution; 721 × 1440 points globally |
| z500 | Height of the 500 hPa surface — the "weather pattern" variable |
| Autoregressive | Feeding a model's output back in as input to step further ahead |
| RMSE / bias / ACC | Error size / systematic error / pattern-correctness (see §5) |
| CSI / FSS | Rain event-hit scores, strict / neighborhood-forgiving (see §5) |
| Persistence | "Tomorrow = today" baseline with zero skill |
| Diagnostic model | Add-on network deriving an extra quantity (e.g. rain) from a forecast |
| Earth2Studio | NVIDIA's open-source framework packaging these models with a common interface |
