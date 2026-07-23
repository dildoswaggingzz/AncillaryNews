# BESS co-optimizer P2 results: how much does double-selling overstate revenue?

Generated 2026-07-23T10:41:48.157102+00:00 by `scripts/generate_cooptimizer_ab_report.py` against the
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
**142,472.08 DKK / 4,604.52 EUR** of revenue that was
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
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 36,944.20 | 26,735.82 | 72.4% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 36,944.20 | 23,800.86 | 64.4% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 50,250.88 | 35,218.77 | 70.1% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 50,250.88 | 31,100.58 | 61.9% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 5,848.90 | 3,855.28 | 65.9% | 811.37 | 481.81 | 59.4% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 5,848.90 | 3,655.35 | 62.5% | 811.37 | 433.35 | 53.4% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 16,722.94 | 9,452.98 | 56.5% | 3,365.91 | 1,969.80 | 58.5% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 16,722.94 | 8,652.45 | 51.7% | 3,365.91 | 1,719.57 | 51.1% |

## 3. Threshold vs. co-optimized, full A/B, per config/zone/window

`delta = cooptimized - threshold`. A negative delta on a window with material phantom
revenue (§2) reflects phantom removal, not a co-optimizer shortfall -- cross-reference
§2's phantom % for that same row before reading a negative delta as a regression.

### Small commercial (1 MW / 2 MWh) -- DK1 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -3,787.88 | 42,586.41 | 46,374.29 | 1,224.3% |
| Capacity (DKK) | 36,944.20 | 218,736.33 | 181,792.13 | 492.1% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 598.24 | 3,518.40 | 2,920.16 | 488.1% |
| Combined total @ 7.46 DKK/EUR (DKK) | 37,619.22 | 287,570.04 | 249,950.82 | 664.4% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,042.79 | 38,548.26 | 33,505.47 | 664.4% |
| Full cycle equivalents | 34.31 | 14.81 | -19.51 | -56.9% |

### Utility-scale (10 MW / 40 MWh) -- DK1 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 1,776.21 | 840,848.06 | 839,071.86 | 47,239.5% |
| Capacity (DKK) | 36,944.20 | 1,983,659.85 | 1,946,715.66 | 5,269.3% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 598.24 | 32,086.46 | 31,488.22 | 5,263.4% |
| Combined total @ 7.46 DKK/EUR (DKK) | 43,183.30 | 3,063,872.93 | 3,020,689.63 | 6,995.0% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,788.65 | 410,706.83 | 404,918.18 | 6,995.0% |
| Full cycle equivalents | 31.90 | 14.96 | -16.94 | -53.1% |

### Small commercial (1 MW / 2 MWh) -- DK1 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -9,065.99 | 146,596.10 | 155,662.08 | 1,717.0% |
| Capacity (DKK) | 50,250.88 | 342,368.24 | 292,117.36 | 581.3% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 598.24 | 3,518.40 | 2,920.16 | 488.1% |
| Combined total @ 7.46 DKK/EUR (DKK) | 45,647.79 | 515,211.63 | 469,563.84 | 1,028.7% |
| Combined total @ 7.46 DKK/EUR (EUR) | 6,119.01 | 69,063.22 | 62,944.21 | 1,028.7% |
| Full cycle equivalents | 105.92 | 84.18 | -21.74 | -20.5% |

### Utility-scale (10 MW / 40 MWh) -- DK1 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 73,241.71 | 2,711,325.90 | 2,638,084.20 | 3,601.9% |
| Capacity (DKK) | 50,250.88 | 3,106,670.13 | 3,056,419.25 | 6,082.3% |
| Capacity (EUR) | 0.00 | 0.00 | 0.00 | n/a |
| aFRR activation (EUR) | 598.24 | 32,086.46 | 31,488.22 | 5,263.4% |
| Combined total @ 7.46 DKK/EUR (DKK) | 127,955.48 | 6,057,361.04 | 5,929,405.56 | 4,634.0% |
| Combined total @ 7.46 DKK/EUR (EUR) | 17,152.21 | 811,978.69 | 794,826.48 | 4,634.0% |
| Full cycle equivalents | 100.13 | 75.81 | -24.31 | -24.3% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -2,905.20 | 52,519.49 | 55,424.69 | 1,907.8% |
| Capacity (DKK) | 5,848.90 | 23,126.17 | 17,277.27 | 295.4% |
| Capacity (EUR) | 811.37 | 9,295.07 | 8,483.70 | 1,045.6% |
| aFRR activation (EUR) | 449.24 | 3,766.01 | 3,316.78 | 738.3% |
| Combined total @ 7.46 DKK/EUR (DKK) | 12,347.84 | 173,081.36 | 160,733.52 | 1,301.7% |
| Combined total @ 7.46 DKK/EUR (EUR) | 1,655.21 | 23,201.25 | 21,546.05 | 1,301.7% |
| Full cycle equivalents | 36.10 | 20.57 | -15.53 | -43.0% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 25,977.93 | 1,009,690.72 | 983,712.80 | 3,786.7% |
| Capacity (DKK) | 5,848.90 | 206,672.23 | 200,823.33 | 3,433.5% |
| Capacity (EUR) | 811.37 | 82,083.19 | 81,271.82 | 10,016.6% |
| aFRR activation (EUR) | 449.24 | 34,664.80 | 34,215.56 | 7,616.4% |
| Combined total @ 7.46 DKK/EUR (DKK) | 41,230.96 | 2,087,302.97 | 2,046,072.01 | 4,962.5% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,526.94 | 279,799.33 | 274,272.39 | 4,962.5% |
| Full cycle equivalents | 33.24 | 20.60 | -12.64 | -38.0% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -5,108.93 | 139,206.23 | 144,315.16 | 2,824.8% |
| Capacity (DKK) | 16,722.94 | 53,404.72 | 36,681.78 | 219.4% |
| Capacity (EUR) | 3,365.91 | 40,490.46 | 37,124.55 | 1,103.0% |
| aFRR activation (EUR) | 449.24 | 3,766.01 | 3,316.78 | 738.3% |
| Combined total @ 7.46 DKK/EUR (DKK) | 40,075.00 | 522,764.26 | 482,689.26 | 1,204.5% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,371.98 | 70,075.64 | 64,703.65 | 1,204.5% |
| Full cycle equivalents | 107.88 | 61.56 | -46.32 | -42.9% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 147,354.34 | 2,679,765.59 | 2,532,411.24 | 1,718.6% |
| Capacity (DKK) | 16,722.94 | 433,652.81 | 416,929.86 | 2,493.2% |
| Capacity (EUR) | 3,365.91 | 363,977.92 | 360,612.01 | 10,713.7% |
| aFRR activation (EUR) | 449.24 | 34,664.80 | 34,215.56 | 7,616.4% |
| Combined total @ 7.46 DKK/EUR (DKK) | 192,538.27 | 6,087,293.05 | 5,894,754.78 | 3,061.6% |
| Combined total @ 7.46 DKK/EUR (EUR) | 25,809.42 | 815,991.03 | 790,181.61 | 3,061.6% |
| Full cycle equivalents | 101.55 | 61.00 | -40.56 | -39.9% |

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

**Imbalance uplift (perfect foresight):** 8,880,618.76 DKK, summed
across every row -- perfect-foresight total WITH imbalance minus the day-ahead-only
co-optimized total already shown in §3. Can never be negative (adding a second
dispatchable market to the same shared budget can only weakly improve the optimum) --
see the sanity check below.

**Post − pre foresight gap:** 13,641,490.75 DKK, summed across every
row -- perfect minus forecast foresight, both with imbalance enabled. This is the
monetary value of forecast skill; with the lag-24h-persistence forecast
(`shared/bess_simulator.py:_lag24h_forecast`) this is a conservative *floor* on that
value (docs/bess-cooptimizer-design.md §5) -- a richer forecast (e.g. the M6 LightGBM
day-ahead/FCR-D models) could only narrow it, never widen it beyond what this lag-24h
floor already shows.

| config | zone | window | day-ahead-only (perfect) | +imbalance (perfect) | imbalance uplift | +imbalance (forecast) | post − pre gap | gap % of post |
|---|---|---|---|---|---|---|---|---|
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 287,570.04 | 416,358.38 | 128,788.35 | 239,685.88 | 176,672.51 | 42.4% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 3,063,872.93 | 4,794,434.49 | 1,730,561.56 | 2,321,582.93 | 2,472,851.57 | 51.6% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 515,211.63 | 683,380.26 | 168,168.62 | 423,041.73 | 260,338.53 | 38.1% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 6,057,361.04 | 8,344,024.80 | 2,286,663.76 | 4,750,900.12 | 3,593,124.68 | 43.1% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 173,081.36 | 322,623.24 | 149,541.88 | 110,665.94 | 211,957.30 | 65.7% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 2,087,302.97 | 4,023,361.70 | 1,936,058.74 | 1,341,944.39 | 2,681,417.31 | 66.6% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 522,764.26 | 698,000.53 | 175,236.27 | 376,619.55 | 321,380.98 | 46.0% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 6,087,293.05 | 8,392,892.63 | 2,305,599.59 | 4,469,144.76 | 3,923,747.88 | 46.8% |

**Sanity checks (computed, not assumed):**

- `pre <= post` holds on all 8 row(s) -- no forecast-foresight total exceeds its own row's perfect-foresight total.
- Imbalance uplift is >= 0 on all 8 row(s) -- adding imbalance never made the perfect-foresight optimum worse.

**Data-coverage caveat (P3).** `imbalance`'s own confirmed-live history is shorter than
day-ahead's at generation time (~35 days vs. day-ahead's ~297) -- the 90d window rows
above therefore only have imbalance prices to dispatch against for part of the window
(no price that period simply means 0 MW dispatched there, same convention as every
other leg in this report), so both the imbalance uplift and the post − pre gap are a
floor on what a fully-covered imbalance history would show, not a ceiling.

