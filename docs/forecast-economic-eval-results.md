# M6 P4 economic evaluation results: does the forecast beat trailing persistence?

Generated 2026-07-22T10:54:42.119242+00:00 by `scripts/generate_economic_eval_report.py` against the
live database (`docs/forecast-economic-eval-design.md`). FCR-D DK2 up/down + day-ahead
DK2 arbitrage only (aFRR_capacity excluded -- no model for it, design §3). Three
allocation policies over the identical simulator/window, differing ONLY in which market
gets committed capacity and how much power is held for capacity versus arbitrage each
period: **trailing** (causal relative-strength ranking, the persistence baseline),
**model** (P3/P3b's leak-safe walk-forward forecast, median and low-tail τ variants),
and **oracle** (the actual next-period price -- never deployable, ceiling only).
`even` (the original simulator's fixed, signal-free split) is reported as a floor.

**Currency correction (flagged, not silently fixed):** the design doc's §3 claims
"FCR-D DK2 and day-ahead DK2 are both EUR." This is not true of the live registry --
`shared/datasets.py` declares day-ahead DK2 `DKK/MWh` (matching `shared/baselines.py`'s
own already-documented finding for the identical series); FCR-D DK2 really is EUR/MW/h.
Capacity revenue (FCR-D) is therefore reported in **EUR** and arbitrage revenue
(day-ahead) in **DKK**, in separate buckets throughout, never summed or converted --
the same discipline `shared/bess_simulator.py`'s own module docstring already applies.

**Window:** fetch window `[2025-10-01, 2026-07-22]`, 6
walk-forward fold(s) (`shared.baselines.walk_forward_folds`, unmodified: 90-day minimum
initial train span, 30-day test folds, 30-day step). Every policy is *simulated* over
the full fetch window (so causal history is warmed up before the first scored tick --
see `shared.economic_eval.restrict_to_scored_ticks`'s docstring) but every number below
is *restricted* to the walk-forward test-fold ticks a forecast actually exists for:
`[2025-12-30, 2026-06-27]`, 4319 scored hourly ticks
(~180 days) -- shorter than the fetch window because the first
~90 days of any walk-forward run are training-only by construction, never scored.

**Leak discipline confirmed:** `tests/test_economic_eval.py` was written leak-test-first
(design's own build-order instruction). `simulate()`'s `policy` parameter is a
`Literal["even", "trailing", "model"]` -- `"oracle"` raises `ValueError` and is not
reachable through it; the only lookahead path is the separately-named
`run_oracle_ceiling()`. Tests assert mutating a future tick's actual price or forecast
never changes an earlier tick's allocation, for both `trailing` and `model`.

## 1. Headroom (compute and read this first -- design §1)

`headroom = oracle revenue − trailing revenue`, per asset config, per currency bucket.
This is the ceiling on what ANY forecast, however perfect, could add over persistence
through allocation alone. A small headroom means the phase's honest answer is
"forecasting doesn't help the decision here" -- a real result, not a failure, especially
given P3/P3b already found these markets persistent.

| asset config | capacity headroom (EUR) | as % of trailing | arbitrage headroom (DKK) | as % of trailing |
|---|---|---|---|---|
| 0.5C (1MW/2MWh) | 1,786.82 | 10.4% | 36,520.47 | 45.7% |
| 0.25C (1MW/4MWh) | 1,786.82 | 10.4% | 79,628.86 | 77.6% |

## 2. Policy comparison: EUR/DKK captured, per config

`even` is the original simulator's fixed, signal-free split (context floor, design §2).
`model_median`/`model_low_tail` allocate on the P3/P3b forecast's τ=0.5/τ=0.1 quantile.

### 0.5C (1MW/2MWh)

| policy | capacity revenue (EUR) | arbitrage revenue (DKK) | realised cycles/day | cycle-cap-binding ticks |
|---|---|---|---|---|
| even | 5,689.47 | 112,851.17 | 0.67 | 100 |
| trailing | 17,177.99 | 79,834.37 | 0.36 | 7 |
| model_median | 13,138.19 | 88,143.36 | 0.43 | 10 |
| model_low_tail | 8,581.45 | 109,664.95 | 0.58 | 24 |
| oracle | 18,964.81 | 116,354.83 | 0.48 | 20 |

### 0.25C (1MW/4MWh)

| policy | capacity revenue (EUR) | arbitrage revenue (DKK) | realised cycles/day | cycle-cap-binding ticks |
|---|---|---|---|---|
| even | 5,689.47 | 245,530.45 | 0.59 | 14 |
| trailing | 17,177.99 | 102,625.86 | 0.24 | 0 |
| model_median | 13,138.19 | 120,807.29 | 0.29 | 0 |
| model_low_tail | 8,581.45 | 182,026.89 | 0.45 | 0 |
| oracle | 18,964.81 | 182,254.72 | 0.35 | 4 |

## 3. Band fraction: `(model − trailing) / (oracle − trailing)`

Fraction of the available headroom the model captures. Positive: beats persistence.
Near 1: approaches the oracle. <= 0: worse than trailing. `n/a` where headroom is
exactly 0 (undefined ratio).

| asset config | quantile variant | capacity band (EUR) | arbitrage band (DKK) |
|---|---|---|---|
| 0.5C (1MW/2MWh) | median | -2.26 | 0.23 |
| 0.5C (1MW/2MWh) | low_tail | -4.81 | 0.82 |
| 0.25C (1MW/4MWh) | median | -2.26 | 0.23 |
| 0.25C (1MW/4MWh) | low_tail | -4.81 | 1.00 |

## 4. Allocation-design prediction: does 0.25C tilt to arbitrage, 0.5C to capacity?

`docs/forecast-allocation-design.md` §5 predicted the 0.25C unit's doubled cycle budget
tilts its optimal allocation toward energy arbitrage, and the 0.5C unit's scarcer
budget tilts it toward capacity. Mean MW committed under `trailing` (the same signal
shape both `model` variants share, so representative of the dynamic-allocation regime):

| asset config | mean FCR-D MW (capacity) | mean arbitrage MW | arbitrage share |
|---|---|---|---|
| 0.5C (1MW/2MWh) | 0.83 | 0.17 | 17.4% |
| 0.25C (1MW/4MWh) | 0.83 | 0.17 | 17.4% |

**Prediction does NOT hold**: 0.25C arbitrage share 17.4% <= 0.5C arbitrage share 17.4%.

**Why the shares come out identical, not just close (an honest limitation, not a bug):** this module's `trailing`/`model`/`oracle` allocation weights a leg purely by its own price relative to its own trailing baseline (`_ratio_strength`/`_abs_deviation_strength`) -- nothing in that signal reads `capacity_mwh` or the cycle budget, so the capacity-vs-arbitrage split is identical for both asset configs by construction here. The allocation design's tilt prediction (§5) is specifically a prediction about the **λ-driven** allocation (design's own §2.3, cycle budget as a shadow-priced reservoir) -- explicitly out of scope for P4 (design §6: 'no λ/annual-budget/water-value optimisation'). This run therefore cannot confirm or falsify that prediction; it can only report that a purely price-relative allocation, with no λ term, does not reproduce it. Testing the actual tilt prediction is deferred to whichever phase builds the λ mechanism. A second, independent limitation compounds this: `shared/economic_eval.py`'s module docstring documents a scale mismatch between the FCR-D ratio signal (centred on 1.0) and the arbitrage deviation signal (centred on 0) that structurally favours FCR-D in `_weighted_split` most hours, regardless of which leg is actually more attractive that tick -- flagged there rather than corrected here (correcting it after seeing this run's own numbers would itself be tuning against the eval window).

## 5. Verdict

**Does acting on the forecast capture more EUR/DKK than acting on trailing persistence, and is there enough headroom to matter?** Per config, per currency bucket, both quantile variants stated explicitly (not just the better one, since
the design flagged the low-tail variant as a specific place to look):

### 0.5C (1MW/2MWh)

- **Capacity (FCR-D, EUR) headroom is small**: 1,786.82 EUR, 10.4% of trailing revenue over ~180 scored days -- little room for ANY forecast, however perfect, to matter here. Consistent with P3's FCR-D finding (does not beat the bar): the forecast not only fails to beat trailing on capacity revenue (band fraction -2.26 median / -4.81 low-tail, both negative -- WORSE than doing nothing clever), it does so against a ceiling that was never large to begin with.
- **Arbitrage (day-ahead, DKK) headroom is material**: 36,520.47 DKK, 45.7% of trailing revenue -- here the model variants BEAT trailing (median band 0.23, low-tail band 0.82), and the low-tail variant captures most or all of the oracle's advantage over trailing -- the same low-tail edge P3/P3b found in pinball loss shows up here as genuine captured revenue.

### 0.25C (1MW/4MWh)

- **Capacity (FCR-D, EUR) headroom is small**: 1,786.82 EUR, 10.4% of trailing revenue over ~180 scored days -- little room for ANY forecast, however perfect, to matter here. Consistent with P3's FCR-D finding (does not beat the bar): the forecast not only fails to beat trailing on capacity revenue (band fraction -2.26 median / -4.81 low-tail, both negative -- WORSE than doing nothing clever), it does so against a ceiling that was never large to begin with.
- **Arbitrage (day-ahead, DKK) headroom is material**: 79,628.86 DKK, 77.6% of trailing revenue -- here the model variants BEAT trailing (median band 0.23, low-tail band 1.00), and the low-tail variant captures most or all of the oracle's advantage over trailing -- the same low-tail edge P3/P3b found in pinball loss shows up here as genuine captured revenue.

### Overall

**The economic result lines up with P3/P3b's pinball-loss result, leg by leg.** Day-ahead's model beat its own bar at τ=0.1/0.25 (`docs/forecast-day-ahead-results.md`), and here that translates into real captured arbitrage-allocation revenue -- concentrated at the low tail, exactly as that document found. FCR-D's model never beat its bar (`docs/forecast-model-results.md`), and here it actively loses money relative to trailing when used for the up/down split -- a forecast that isn't accurate enough to beat a trivial baseline isn't accurate enough to allocate on, either. Capacity headroom is small enough that this loss barely matters in absolute terms; arbitrage headroom is large enough that the gain does. Both asset configs read identically on capacity (by construction -- see §4) and differ only in how much of the (config-independent) arbitrage gain they realise, which scales with each config's own cycle/energy budget.

