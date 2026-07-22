# BESS co-optimizer P2 results: how much does double-selling overstate revenue?

Generated 2026-07-22T13:59:16.023057+00:00 by `scripts/generate_cooptimizer_ab_report.py` against the
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
**142,465.51 DKK / 4,613.21 EUR** of revenue that was
**infeasible** under the same both-endpoint no-double-selling headroom rule the
co-optimizer enforces (`shared.bess_dispatch_milp.phantom_capacity_revenue`) -- the
battery lacked the SoC headroom to actually deliver the committed MW. That is
**64.9% of the threshold engine's total DKK capacity
revenue** and **55.1% of its total EUR capacity revenue**
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

- DK1 / 30d: Small commercial (1 MW / 2 MWh) 72.4% vs. Utility-scale (10 MW / 40 MWh) 64.4%
- DK1 / 90d: Small commercial (1 MW / 2 MWh) 70.1% vs. Utility-scale (10 MW / 40 MWh) 61.9%
- DK2 / 30d: Small commercial (1 MW / 2 MWh) 65.9% vs. Utility-scale (10 MW / 40 MWh) 62.5%
- DK2 / 90d: Small commercial (1 MW / 2 MWh) 56.5% vs. Utility-scale (10 MW / 40 MWh) 51.7%

Direction confirmed on every pair. Magnitude stayed substantial for both configs in this run, not near-zero for the larger one -- see the arbitrage deltas in §3: the realized `day_ahead` window was highly volatile, which drives the threshold engine's fixed-size arbitrage z-score logic to pin SoC at its usable-band extremes a large fraction of ticks *regardless of battery size*, and any capacity leg committed during those ticks is phantom by construction -- a real property of this evaluation window, not of battery scale alone.

## 2. Phantom-revenue diagnostic, per config/zone/window

| config | zone | window | threshold capacity (DKK) | phantom (DKK) | phantom % | threshold capacity (EUR) | phantom (EUR) | phantom % |
|---|---|---|---|---|---|---|---|---|
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 36,944.18 | 26,735.81 | 72.4% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 36,944.18 | 23,800.85 | 64.4% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 50,250.86 | 35,218.76 | 70.1% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 50,250.86 | 31,100.57 | 61.9% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 5,844.41 | 3,853.51 | 65.9% | 816.33 | 483.92 | 59.3% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 5,844.41 | 3,653.85 | 62.5% | 816.33 | 435.58 | 53.4% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 16,718.45 | 9,451.22 | 56.5% | 3,370.87 | 1,971.91 | 58.5% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 16,718.45 | 8,650.95 | 51.7% | 3,370.87 | 1,721.80 | 51.1% |

## 3. Threshold vs. co-optimized, full A/B, per config/zone/window

`delta = cooptimized - threshold`. A negative delta on a window with material phantom
revenue (§2) reflects phantom removal, not a co-optimizer shortfall -- cross-reference
§2's phantom % for that same row before reading a negative delta as a regression.

### Small commercial (1 MW / 2 MWh) -- DK1 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -3,787.88 | 42,195.58 | 45,983.46 | 1,214.0% |
| Capacity (DKK) | 36,944.18 | 228,118.58 | 191,174.40 | 517.5% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 736.40 | 159.59 | -576.81 | -78.3% |
| Combined total @ 7.46 DKK/EUR (DKK) | 38,649.87 | 271,504.72 | 232,854.85 | 602.5% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,180.95 | 36,394.73 | 31,213.79 | 602.5% |
| Full cycle equivalents | 34.31 | 15.46 | -18.85 | -54.9% |

### Utility-scale (10 MW / 40 MWh) -- DK1 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 1,776.21 | 843,831.33 | 842,055.13 | 47,407.5% |
| Capacity (DKK) | 36,944.18 | 2,058,089.12 | 2,021,144.93 | 5,470.8% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 736.40 | 1,131.36 | 394.96 | 53.6% |
| Combined total @ 7.46 DKK/EUR (DKK) | 44,213.96 | 2,910,360.39 | 2,866,146.43 | 6,482.4% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,926.80 | 390,128.74 | 384,201.93 | 6,482.4% |
| Full cycle equivalents | 31.90 | 15.47 | -16.44 | -51.5% |

### Small commercial (1 MW / 2 MWh) -- DK1 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -9,065.99 | 146,205.26 | 155,271.25 | 1,712.7% |
| Capacity (DKK) | 50,250.86 | 351,750.50 | 301,499.63 | 600.0% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 736.40 | 159.59 | -576.81 | -78.3% |
| Combined total @ 7.46 DKK/EUR (DKK) | 46,678.45 | 499,146.32 | 452,467.87 | 969.3% |
| Combined total @ 7.46 DKK/EUR (EUR) | 6,257.16 | 66,909.69 | 60,652.53 | 969.3% |
| Full cycle equivalents | 105.92 | 84.83 | -21.08 | -19.9% |

### Utility-scale (10 MW / 40 MWh) -- DK1 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 73,241.71 | 2,714,309.17 | 2,641,067.47 | 3,606.0% |
| Capacity (DKK) | 50,250.86 | 3,181,099.39 | 3,130,848.53 | 6,230.4% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 736.40 | 1,131.36 | 394.96 | 53.6% |
| Combined total @ 7.46 DKK/EUR (DKK) | 128,986.14 | 5,903,848.51 | 5,774,862.37 | 4,477.1% |
| Combined total @ 7.46 DKK/EUR (EUR) | 17,290.37 | 791,400.60 | 774,110.24 | 4,477.1% |
| Full cycle equivalents | 100.13 | 76.32 | -23.81 | -23.8% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -2,905.20 | 61,164.33 | 64,069.53 | 2,205.3% |
| Capacity (DKK) | 5,844.41 | 44,459.48 | 38,615.08 | 660.7% |
| Capacity (EUR) | 816.33 | 0.00 | -816.33 | -100.0% |
| aFRR activation (EUR) | 494.38 | 1,600.44 | 1,106.07 | 223.7% |
| Combined total @ 7.46 DKK/EUR (DKK) | 12,717.13 | 117,563.13 | 104,846.00 | 824.4% |
| Combined total @ 7.46 DKK/EUR (EUR) | 1,704.71 | 15,759.13 | 14,054.42 | 824.4% |
| Full cycle equivalents | 36.10 | 34.45 | -1.65 | -4.6% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 25,977.93 | 1,110,869.95 | 1,084,892.02 | 4,176.2% |
| Capacity (DKK) | 5,844.41 | 378,837.00 | 372,992.59 | 6,382.0% |
| Capacity (EUR) | 816.33 | 0.00 | -816.33 | -100.0% |
| aFRR activation (EUR) | 494.38 | 12,824.23 | 12,329.85 | 2,494.0% |
| Combined total @ 7.46 DKK/EUR (DKK) | 41,600.26 | 1,585,375.67 | 1,543,775.42 | 3,711.0% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,576.44 | 212,516.85 | 206,940.40 | 3,711.0% |
| Full cycle equivalents | 33.24 | 31.13 | -2.11 | -6.3% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -5,108.93 | 167,288.76 | 172,397.69 | 3,374.4% |
| Capacity (DKK) | 16,718.45 | 144,091.39 | 127,372.94 | 761.9% |
| Capacity (EUR) | 3,370.87 | 0.00 | -3,370.87 | -100.0% |
| aFRR activation (EUR) | 494.38 | 1,600.44 | 1,106.07 | 223.7% |
| Combined total @ 7.46 DKK/EUR (DKK) | 40,444.30 | 323,319.47 | 282,875.17 | 699.4% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,421.49 | 43,340.41 | 37,918.92 | 699.4% |
| Full cycle equivalents | 107.88 | 100.61 | -7.26 | -6.7% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 147,354.34 | 3,013,029.13 | 2,865,674.79 | 1,944.8% |
| Capacity (DKK) | 16,718.45 | 1,180,866.23 | 1,164,147.78 | 6,963.3% |
| Capacity (EUR) | 3,370.87 | 0.00 | -3,370.87 | -100.0% |
| aFRR activation (EUR) | 494.38 | 12,824.23 | 12,329.85 | 2,494.0% |
| Combined total @ 7.46 DKK/EUR (DKK) | 192,907.57 | 4,289,564.09 | 4,096,656.52 | 2,123.6% |
| Combined total @ 7.46 DKK/EUR (EUR) | 25,858.92 | 575,008.59 | 549,149.67 | 2,123.6% |
| Full cycle equivalents | 101.55 | 89.97 | -11.58 | -11.4% |

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

**Observed P1 decomposition effect -- EUR capacity crowded out by arbitrage.** In
4 row(s) below, the threshold engine booked real EUR capacity
revenue but the co-optimizer's reported EUR capacity revenue is ~0. This is a known
consequence of the sequential DKK-then-EUR decomposition
(`shared/bess_dispatch_milp.py`'s module docstring): Solve 1 (arbitrage + DKK legs)
decides how much power/headroom to leave for Solve 2's EUR legs *without* knowing
what EUR revenue it is foregoing, and on a window where arbitrage is this lucrative
(the realized `day_ahead` series in this run's window is highly volatile -- see the
large arbitrage deltas in §3), Solve 1 has every incentive to claim the entire power
budget for arbitrage, leaving Solve 2 nothing. **This is not a finding that EUR
capacity reservation is unprofitable** -- the threshold engine's own booked EUR
revenue in the same rows proves otherwise -- it is a limitation of P1's currency-
decomposition choice, flagged here rather than silently absorbed into the headline
numbers:

- Small commercial (1 MW / 2 MWh) / DK2 / 30d: threshold EUR capacity 816.33, co-optimized EUR capacity 0.00.
- Utility-scale (10 MW / 40 MWh) / DK2 / 30d: threshold EUR capacity 816.33, co-optimized EUR capacity 0.00.
- Small commercial (1 MW / 2 MWh) / DK2 / 90d: threshold EUR capacity 3,370.87, co-optimized EUR capacity 0.00.
- Utility-scale (10 MW / 40 MWh) / DK2 / 90d: threshold EUR capacity 3,370.87, co-optimized EUR capacity 0.00.

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

**Imbalance uplift (perfect foresight):** 9,368,939.45 DKK, summed
across every row -- perfect-foresight total WITH imbalance minus the day-ahead-only
co-optimized total already shown in §3. Can never be negative (adding a second
dispatchable market to the same shared budget can only weakly improve the optimum) --
see the sanity check below.

**Post − pre foresight gap:** 12,683,183.92 DKK, summed across every
row -- perfect minus forecast foresight, both with imbalance enabled. This is the
monetary value of forecast skill; with the lag-24h-persistence forecast
(`shared/bess_simulator.py:_lag24h_forecast`) this is a conservative *floor* on that
value (docs/bess-cooptimizer-design.md §5) -- a richer forecast (e.g. the M6 LightGBM
day-ahead/FCR-D models) could only narrow it, never widen it beyond what this lag-24h
floor already shows.

| config | zone | window | day-ahead-only (perfect) | +imbalance (perfect) | imbalance uplift | +imbalance (forecast) | post − pre gap | gap % of post |
|---|---|---|---|---|---|---|---|---|
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 271,504.72 | 402,732.39 | 131,227.67 | 230,027.28 | 172,705.12 | 42.9% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 2,910,360.39 | 4,702,913.65 | 1,792,553.26 | 2,248,741.10 | 2,454,172.55 | 52.2% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 499,146.32 | 669,754.27 | 170,607.95 | 412,556.99 | 257,197.28 | 38.4% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 5,903,848.51 | 8,252,503.96 | 2,348,655.45 | 4,674,711.56 | 3,577,792.41 | 43.4% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 117,563.13 | 272,824.88 | 155,261.75 | 83,428.92 | 189,395.96 | 69.4% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 1,585,375.67 | 3,674,943.82 | 2,089,568.15 | 1,225,720.16 | 2,449,223.66 | 66.6% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 323,319.47 | 506,638.15 | 183,318.69 | 245,038.93 | 261,599.22 | 51.6% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 4,289,564.09 | 6,787,310.63 | 2,497,746.54 | 3,466,212.90 | 3,321,097.73 | 48.9% |

**Sanity checks (computed, not assumed):**

- `pre <= post` holds on all 8 row(s) -- no forecast-foresight total exceeds its own row's perfect-foresight total.
- Imbalance uplift is >= 0 on all 8 row(s) -- adding imbalance never made the perfect-foresight optimum worse.

**Data-coverage caveat (P3).** `imbalance`'s own confirmed-live history is shorter than
day-ahead's at generation time (~35 days vs. day-ahead's ~297) -- the 90d window rows
above therefore only have imbalance prices to dispatch against for part of the window
(no price that period simply means 0 MW dispatched there, same convention as every
other leg in this report), so both the imbalance uplift and the post − pre gap are a
floor on what a fully-covered imbalance history would show, not a ceiling.

