# BESS co-optimizer P2 results: how much does double-selling overstate revenue?

Generated 2026-07-23T16:12:59.767985+00:00 by `scripts/generate_cooptimizer_ab_report.py` against the
live database (`docs/bess-cooptimizer-design.md` §7 P2 row / §8). Every figure below is
an **estimate** from a simulated backtest, not a real trading outcome -- same posture as
every other BESS figure this project publishes (`shared/bess_simulator.py`'s module
docstring, README). Read-only: `run_backtest` only reads `market_data`/
`market_data_history`; nothing here is persisted via `save_bess_run`.

**The framing (read this before the numbers):** the threshold engine's double-selling
defect means its reported total can be *higher* than the co-optimizer's on some windows
-- that excess is **phantom** (infeasible) revenue, not a co-optimizer regression. On
windows where the threshold engine never double-sells, the co-optimizer's perfect-
foresight total is `>=` the threshold's, as a true optimum must be. Both directions are
shown below; neither is framed as "the co-optimizer earning less is worse" -- see
§4 for the full reading note.

## 1. Headline: the double-selling overstatement

Across 8 (config x zone x window) run(s) on real ingested data, the
threshold engine's booked capacity revenue included
**106,114.65 DKK / 4,561.28 EUR** of revenue that was
**infeasible** under the same both-endpoint no-double-selling headroom rule the
co-optimizer enforces (`shared.bess_dispatch_milp.phantom_capacity_revenue`) -- the
battery lacked the SoC headroom to actually deliver the committed MW. That is
**64.1% of the threshold engine's total DKK capacity
revenue** and **54.8% of its total EUR capacity revenue**
across these runs, computed purely from the threshold trace itself (not confounded by the
co-optimizer's own, separately different arbitrage timing -- see §4). This diagnostic is
also a per-leg, not a per-direction-group, bound (`phantom_capacity_revenue`'s own
docstring) -- if anything it *understates* the true phantom total when multiple legs
share a direction the same period.

**Sanity check -- does a bigger, proportionally-ample-energy battery see a lower
phantom fraction?** `ILLUSTRATIVE_CONFIGS` uses a *fixed* `capacity_commit_mw` (0.3
MW) regardless of battery size, so this isn't guaranteed to shrink to near-zero for
the larger config -- but the direction should hold. In this run,
**Utility-scale (10 MW / 40 MWh)**'s phantom fraction was lower than **Small commercial (1 MW / 2 MWh)**'s in
4 of 4 paired (zone, window) run(s):

- DK1 / 30d: Small commercial (1 MW / 2 MWh) 71.3% vs. Utility-scale (10 MW / 40 MWh) 66.4%
- DK1 / 90d: Small commercial (1 MW / 2 MWh) 70.1% vs. Utility-scale (10 MW / 40 MWh) 61.9%
- DK2 / 30d: Small commercial (1 MW / 2 MWh) 70.0% vs. Utility-scale (10 MW / 40 MWh) 64.6%
- DK2 / 90d: Small commercial (1 MW / 2 MWh) 57.0% vs. Utility-scale (10 MW / 40 MWh) 52.3%

Direction confirmed on every pair. Magnitude stayed substantial for both configs in this run, not near-zero for the larger one -- see the arbitrage deltas in §3: the realized `day_ahead` window was highly volatile, which drives the threshold engine's fixed-size arbitrage z-score logic to pin SoC at its usable-band extremes a large fraction of ticks *regardless of battery size*, and any capacity leg committed during those ticks is phantom by construction -- a real property of this evaluation window, not of battery scale alone.

## 2. Phantom-revenue diagnostic, per config/zone/window

| config | zone | window | threshold capacity (DKK) | phantom (DKK) | phantom % | threshold capacity (EUR) | phantom (EUR) | phantom % |
|---|---|---|---|---|---|---|---|---|
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 9,095.08 | 6,489.12 | 71.3% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 9,095.08 | 6,036.60 | 66.4% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 50,735.98 | 35,543.51 | 70.1% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 50,735.98 | 31,396.00 | 61.9% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 5,981.81 | 4,184.79 | 70.0% | 795.99 | 470.34 | 59.1% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 5,981.81 | 3,864.69 | 64.6% | 795.99 | 419.32 | 52.7% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 17,021.30 | 9,699.53 | 57.0% | 3,363.08 | 1,961.07 | 58.3% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 17,021.30 | 8,900.41 | 52.3% | 3,363.08 | 1,710.54 | 50.9% |

## 3. Threshold vs. co-optimized, full A/B, per config/zone/window

`delta = cooptimized - threshold`. A negative delta on a window with material phantom
revenue (§2) reflects phantom removal, not a co-optimizer shortfall -- cross-reference
§2's phantom % for that same row before reading a negative delta as a regression.

### Small commercial (1 MW / 2 MWh) -- DK1 -- 30d (2026-06-25 to 2026-07-25)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -2,823.33 | 52,986.36 | 55,809.70 | 1,976.7% |
| Capacity (DKK) | 9,095.08 | 58,298.47 | 49,203.39 | 541.0% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 698.20 | 4,018.37 | 3,320.17 | 475.5% |
| Combined total @ 7.46 DKK/EUR (DKK) | 11,480.30 | 141,261.89 | 129,781.59 | 1,130.5% |
| Combined total @ 7.46 DKK/EUR (EUR) | 1,538.91 | 18,935.91 | 17,397.00 | 1,130.5% |
| Full cycle equivalents | 34.89 | 31.92 | -2.97 | -8.5% |

### Utility-scale (10 MW / 40 MWh) -- DK1 -- 30d (2026-06-25 to 2026-07-25)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 3,222.56 | 978,987.14 | 975,764.58 | 30,279.2% |
| Capacity (DKK) | 9,095.08 | 518,443.52 | 509,348.44 | 5,600.3% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 698.20 | 36,664.69 | 35,966.49 | 5,151.3% |
| Combined total @ 7.46 DKK/EUR (DKK) | 17,526.19 | 1,770,949.26 | 1,753,423.07 | 10,004.6% |
| Combined total @ 7.46 DKK/EUR (EUR) | 2,349.36 | 237,392.66 | 235,043.31 | 10,004.6% |
| Full cycle equivalents | 32.22 | 29.64 | -2.57 | -8.0% |

### Small commercial (1 MW / 2 MWh) -- DK1 -- 90d (2026-04-26 to 2026-07-25)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -8,047.16 | 147,279.08 | 155,326.24 | 1,930.2% |
| Capacity (DKK) | 50,735.98 | 342,191.63 | 291,455.65 | 574.5% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 698.20 | 4,018.37 | 3,320.17 | 475.5% |
| Combined total @ 7.46 DKK/EUR (DKK) | 47,897.38 | 519,447.76 | 471,550.38 | 984.5% |
| Combined total @ 7.46 DKK/EUR (EUR) | 6,420.56 | 69,631.07 | 63,210.51 | 984.5% |
| Full cycle equivalents | 107.04 | 83.91 | -23.12 | -21.6% |

### Utility-scale (10 MW / 40 MWh) -- DK1 -- 90d (2026-04-26 to 2026-07-25)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 85,988.44 | 2,725,407.15 | 2,639,418.71 | 3,069.5% |
| Capacity (DKK) | 50,735.98 | 3,099,063.16 | 3,048,327.17 | 6,008.2% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 698.20 | 36,664.69 | 35,966.49 | 5,151.3% |
| Combined total @ 7.46 DKK/EUR (DKK) | 141,932.98 | 6,097,988.91 | 5,956,055.93 | 4,196.4% |
| Combined total @ 7.46 DKK/EUR (EUR) | 19,025.87 | 817,424.79 | 798,398.92 | 4,196.4% |
| Full cycle equivalents | 101.05 | 75.69 | -25.37 | -25.1% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 30d (2026-06-25 to 2026-07-25)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -2,061.70 | 47,564.62 | 49,626.31 | 2,407.1% |
| Capacity (DKK) | 5,981.81 | 22,525.59 | 16,543.78 | 276.6% |
| Capacity (EUR) | 795.99 | 9,145.15 | 8,349.16 | 1,048.9% |
| aFRR activation (EUR) | 514.91 | 4,315.64 | 3,800.72 | 738.1% |
| Combined total @ 7.46 DKK/EUR (DKK) | 13,699.46 | 170,507.68 | 156,808.22 | 1,144.6% |
| Combined total @ 7.46 DKK/EUR (EUR) | 1,836.39 | 22,856.26 | 21,019.87 | 1,144.6% |
| Full cycle equivalents | 36.62 | 20.46 | -16.16 | -44.1% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 30d (2026-06-25 to 2026-07-25)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 28,737.77 | 914,306.01 | 885,568.24 | 3,081.5% |
| Capacity (DKK) | 5,981.81 | 210,242.88 | 204,261.07 | 3,414.7% |
| Capacity (EUR) | 795.99 | 80,539.58 | 79,743.59 | 10,018.2% |
| aFRR activation (EUR) | 514.91 | 39,417.86 | 38,902.95 | 7,555.2% |
| Combined total @ 7.46 DKK/EUR (DKK) | 44,498.93 | 2,019,431.38 | 1,974,932.45 | 4,438.2% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,965.00 | 270,701.26 | 264,736.25 | 4,438.2% |
| Full cycle equivalents | 33.54 | 20.49 | -13.06 | -38.9% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 90d (2026-04-26 to 2026-07-25)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -4,332.16 | 140,819.97 | 145,152.13 | 3,350.6% |
| Capacity (DKK) | 17,021.30 | 54,771.05 | 37,749.75 | 221.8% |
| Capacity (EUR) | 3,363.08 | 40,174.54 | 36,811.46 | 1,094.6% |
| aFRR activation (EUR) | 514.91 | 4,315.64 | 3,800.72 | 738.1% |
| Combined total @ 7.46 DKK/EUR (DKK) | 41,618.95 | 527,487.73 | 485,868.78 | 1,167.4% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,578.95 | 70,708.81 | 65,129.86 | 1,167.4% |
| Full cycle equivalents | 108.91 | 61.80 | -47.11 | -43.3% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 90d (2026-04-26 to 2026-07-25)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 157,834.03 | 2,703,047.36 | 2,545,213.34 | 1,612.6% |
| Capacity (DKK) | 17,021.30 | 447,914.54 | 430,893.23 | 2,531.5% |
| Capacity (EUR) | 3,363.08 | 361,322.68 | 357,959.60 | 10,643.8% |
| aFRR activation (EUR) | 514.91 | 39,417.86 | 38,902.95 | 7,555.2% |
| Combined total @ 7.46 DKK/EUR (DKK) | 203,785.14 | 6,140,486.32 | 5,936,701.18 | 2,913.2% |
| Combined total @ 7.46 DKK/EUR (EUR) | 27,317.04 | 823,121.49 | 795,804.45 | 2,913.2% |
| Full cycle equivalents | 102.42 | 61.05 | -41.36 | -40.4% |

## 4. Reading this: two directions, both honest

**Direction A -- phantom overstatement (§1/§2).** The threshold engine subtracts only
*power*, never energy/SoC, before offering capacity (`shared/bess_simulator.py`'s module
docstring §0). On a window where its arbitrage leg discharges the battery toward
`soc_min` while it is *also* booking up-reserve payments, part of that reserve payment is
for MW the battery could not have delivered -- phantom revenue. This is the honest
answer to "how wrong is the number the Morning Brief currently publishes", and it is
computed independently of anything the co-optimizer does.

**Direction B -- co-optimizer upside on feasible windows.** Where the threshold engine's
dispatch never double-sells, its reported total is a real, feasible outcome, and the
co-optimizer -- a true perfect-foresight optimum over the identical battery physics and
identical prices -- is `>=` it (an optimum can never underperform a feasible policy).
Any positive delta in §3 on such a window is genuine additional revenue the threshold
heuristic's z-score/fixed-split logic left on the table (docs/bess-cooptimizer-design.md
§0, points 2/3), not phantom removal.

**Never read a negative §3 delta, by itself, as "the co-optimizer is worse."** Check
§2's phantom % for that same (config, zone, window) row first: a large phantom fraction
means most or all of the negative delta is phantom removal (the threshold total was
never honestly achievable); a small or zero phantom fraction alongside a negative
combined-total delta would instead be a genuine finding worth investigating.

No row in this run has that combination -- every negative revenue delta observed (§3) co-occurs with a material phantom fraction (§2), consistent with phantom removal rather than a co-optimizer shortfall.

**Data-coverage caveat.** `day_ahead` coverage is gap-free over every window above
(§2's window-discovery gate); `FCR`/`aFRR_capacity`/`FFR`/`aFRR_energy` are not held to
the same gate and several have materially shorter confirmed-live history than
day-ahead's -- a period with no price for one of these legs simply earns 0 that period
(`_value_at_or_before`), which understates (never overstates) both the threshold and
co-optimized capacity/activation totals equally, so it does not bias the phantom
fraction (a ratio of the threshold's own booked revenue) but does mean the absolute DKK/
EUR figures above are a floor, not a ceiling, on what a fully-covered history would show.

## 5. P3: imbalance uplift + post − pre foresight gap

Two further co-optimized runs per (config, zone, window) row above, both with
`energy_markets=("day_ahead", "imbalance")` -- a BESS *chooses* its imbalance exposure,
so it is a second dispatchable energy market sharing the day-ahead leg's power/SoC
budget, not passive settlement (docs/bess-cooptimizer-design.md §6).

**Imbalance uplift (perfect foresight):** 9,337,203.96 DKK, summed
across every row -- perfect-foresight total WITH imbalance minus the day-ahead-only
co-optimized total already shown in §3. Can never be negative (adding a second
dispatchable market to the same shared budget can only weakly improve the optimum) --
see the sanity check below.

**Post − pre foresight gap:** 13,509,680.18 DKK, summed across every
row -- perfect minus forecast foresight, both with imbalance enabled. This is the
monetary value of forecast skill; with the lag-24h-persistence forecast
(`shared/bess_simulator.py:_lag24h_forecast`) this is a conservative *floor* on that
value (docs/bess-cooptimizer-design.md §5) -- a richer forecast (e.g. the M6 LightGBM
day-ahead/FCR-D models) could only narrow it, never widen it beyond what this lag-24h
floor already shows.

| config | zone | window | day-ahead-only (perfect) | +imbalance (perfect) | imbalance uplift | +imbalance (forecast) | post − pre gap | gap % of post |
|---|---|---|---|---|---|---|---|---|
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 141,261.89 | 287,816.15 | 146,554.26 | 119,799.70 | 168,016.45 | 58.4% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 1,770,949.26 | 3,868,587.38 | 2,097,638.12 | 1,482,887.52 | 2,385,699.86 | 61.7% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 519,447.76 | 689,412.29 | 169,964.53 | 424,413.74 | 264,998.55 | 38.4% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 6,097,988.91 | 8,420,820.83 | 2,322,831.92 | 4,832,534.98 | 3,588,285.85 | 42.6% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 170,507.68 | 319,374.52 | 148,866.84 | 110,405.04 | 208,969.48 | 65.4% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 2,019,431.38 | 3,952,553.10 | 1,933,121.72 | 1,313,919.66 | 2,638,633.44 | 66.8% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 527,487.73 | 704,376.77 | 176,889.04 | 380,448.64 | 323,928.13 | 46.0% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 6,140,486.32 | 8,481,823.85 | 2,341,337.52 | 4,550,675.43 | 3,931,148.42 | 46.3% |

**Sanity checks (computed, not assumed):**

- `pre <= post` holds on all 8 row(s) -- no forecast-foresight total exceeds its own row's perfect-foresight total.
- Imbalance uplift is >= 0 on all 8 row(s) -- adding imbalance never made the perfect-foresight optimum worse.

**Data-coverage caveat (P3).** `imbalance`'s own confirmed-live history is shorter than
day-ahead's at generation time (~35 days vs. day-ahead's ~297) -- the 90d window rows
above therefore only have imbalance prices to dispatch against for part of the window
(no price that period simply means 0 MW dispatched there, same convention as every
other leg in this report), so both the imbalance uplift and the post − pre gap are a
floor on what a fully-covered imbalance history would show, not a ceiling.

