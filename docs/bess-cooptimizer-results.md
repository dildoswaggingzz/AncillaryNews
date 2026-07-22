# BESS co-optimizer P2 results: how much does double-selling overstate revenue?

Generated 2026-07-22T12:57:50.314054+00:00 by `scripts/generate_cooptimizer_ab_report.py` against the
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
**142,478.97 DKK / 4,613.40 EUR** of revenue that was
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
- DK2 / 30d: Small commercial (1 MW / 2 MWh) 66.1% vs. Utility-scale (10 MW / 40 MWh) 62.9%
- DK2 / 90d: Small commercial (1 MW / 2 MWh) 56.6% vs. Utility-scale (10 MW / 40 MWh) 51.9%

Direction confirmed on every pair. Magnitude stayed substantial for both configs in this run, not near-zero for the larger one -- see the arbitrage deltas in §3: the realized `day_ahead` window was highly volatile, which drives the threshold engine's fixed-size arbitrage z-score logic to pin SoC at its usable-band extremes a large fraction of ticks *regardless of battery size*, and any capacity leg committed during those ticks is phantom by construction -- a real property of this evaluation window, not of battery scale alone.

## 2. Phantom-revenue diagnostic, per config/zone/window

| config | zone | window | threshold capacity (DKK) | phantom (DKK) | phantom % | threshold capacity (EUR) | phantom (EUR) | phantom % |
|---|---|---|---|---|---|---|---|---|
| Small commercial (1 MW / 2 MWh) | DK1 | 30d | 36,944.18 | 26,735.81 | 72.4% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 30d | 36,944.18 | 23,800.85 | 64.4% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK1 | 90d | 50,250.86 | 35,218.76 | 70.1% | 0.00 | 0.00 | 0.0% |
| Utility-scale (10 MW / 40 MWh) | DK1 | 90d | 50,250.86 | 31,100.57 | 61.9% | 0.00 | 0.00 | 0.0% |
| Small commercial (1 MW / 2 MWh) | DK2 | 30d | 5,826.39 | 3,850.22 | 66.1% | 817.18 | 483.94 | 59.2% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 30d | 5,826.39 | 3,663.87 | 62.9% | 817.18 | 435.65 | 53.3% |
| Small commercial (1 MW / 2 MWh) | DK2 | 90d | 16,700.44 | 9,447.93 | 56.6% | 3,371.72 | 1,971.94 | 58.5% |
| Utility-scale (10 MW / 40 MWh) | DK2 | 90d | 16,700.44 | 8,660.97 | 51.9% | 3,371.72 | 1,721.87 | 51.1% |

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
| Arbitrage (DKK) | -2,905.20 | 61,296.36 | 64,201.56 | 2,209.9% |
| Capacity (DKK) | 5,826.39 | 43,648.60 | 37,822.20 | 649.2% |
| Capacity (EUR) | 817.18 | 0.00 | -817.18 | -100.0% |
| aFRR activation (EUR) | 495.64 | 1,495.17 | 999.52 | 201.7% |
| Combined total @ 7.46 DKK/EUR (DKK) | 12,714.85 | 116,098.91 | 103,384.06 | 813.1% |
| Combined total @ 7.46 DKK/EUR (EUR) | 1,704.40 | 15,562.86 | 13,858.45 | 813.1% |
| Full cycle equivalents | 36.10 | 34.58 | -1.52 | -4.2% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 30d (2026-06-24 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 25,977.93 | 1,113,142.96 | 1,087,165.03 | 4,185.0% |
| Capacity (DKK) | 5,826.39 | 370,076.92 | 364,250.53 | 6,251.7% |
| Capacity (EUR) | 817.18 | 0.00 | -817.18 | -100.0% |
| aFRR activation (EUR) | 495.64 | 12,626.21 | 12,130.56 | 2,447.4% |
| Combined total @ 7.46 DKK/EUR (DKK) | 41,597.98 | 1,577,411.40 | 1,535,813.42 | 3,692.0% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,576.14 | 211,449.25 | 205,873.11 | 3,692.0% |
| Full cycle equivalents | 33.24 | 31.19 | -2.05 | -6.2% |

### Small commercial (1 MW / 2 MWh) -- DK2 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | -5,108.93 | 167,418.75 | 172,527.68 | 3,377.0% |
| Capacity (DKK) | 16,700.44 | 143,282.55 | 126,582.11 | 758.0% |
| Capacity (EUR) | 3,371.72 | 0.00 | -3,371.72 | -100.0% |
| aFRR activation (EUR) | 495.64 | 1,495.17 | 999.52 | 201.7% |
| Combined total @ 7.46 DKK/EUR (DKK) | 40,442.02 | 321,855.25 | 281,413.23 | 695.8% |
| Combined total @ 7.46 DKK/EUR (EUR) | 5,421.18 | 43,144.13 | 37,722.95 | 695.8% |
| Full cycle equivalents | 107.88 | 100.74 | -7.14 | -6.6% |

### Utility-scale (10 MW / 40 MWh) -- DK2 -- 90d (2026-04-25 to 2026-07-24)

Currencies present: DKK, EUR.

| metric | threshold | co-optimized | delta | delta % |
|---|---|---|---|---|
| Arbitrage (DKK) | 147,354.34 | 3,015,343.75 | 2,867,989.41 | 1,946.3% |
| Capacity (DKK) | 16,700.44 | 1,172,064.55 | 1,155,364.12 | 6,918.2% |
| Capacity (EUR) | 3,371.72 | 0.00 | -3,371.72 | -100.0% |
| aFRR activation (EUR) | 495.64 | 12,626.21 | 12,130.56 | 2,447.4% |
| Combined total @ 7.46 DKK/EUR (DKK) | 192,905.29 | 4,281,599.81 | 4,088,694.52 | 2,119.5% |
| Combined total @ 7.46 DKK/EUR (EUR) | 25,858.62 | 573,940.99 | 548,082.38 | 2,119.5% |
| Full cycle equivalents | 101.55 | 90.04 | -11.52 | -11.3% |

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

- Small commercial (1 MW / 2 MWh) / DK2 / 30d: threshold EUR capacity 817.18, co-optimized EUR capacity 0.00.
- Utility-scale (10 MW / 40 MWh) / DK2 / 30d: threshold EUR capacity 817.18, co-optimized EUR capacity 0.00.
- Small commercial (1 MW / 2 MWh) / DK2 / 90d: threshold EUR capacity 3,371.72, co-optimized EUR capacity 0.00.
- Utility-scale (10 MW / 40 MWh) / DK2 / 90d: threshold EUR capacity 3,371.72, co-optimized EUR capacity 0.00.

**Data-coverage caveat.** `day_ahead` coverage is gap-free over every window above
(§2's window-discovery gate); `FCR`/`aFRR_capacity`/`FFR`/`aFRR_energy` are not held to
the same gate and several have materially shorter confirmed-live history than
day-ahead's -- a period with no price for one of these legs simply earns 0 that period
(`_value_at_or_before`), which understates (never overstates) both the threshold and
co-optimized capacity/activation totals equally, so it does not bias the phantom
fraction (a ratio of the threshold's own booked revenue) but does mean the absolute DKK/
EUR figures above are a floor, not a ceiling, on what a fully-covered history would show.

