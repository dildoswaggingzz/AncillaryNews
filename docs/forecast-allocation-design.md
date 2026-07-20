# M6 Design: Cycle budget, feature store, and the allocation layer

**Date:** 2026-07-20
**Status:** Design — not yet built. Companion to `docs/forecast-datasets-scope.md` (M6 scope),
which this document assumes has been read. Scope answers *what data*; this answers *what the
model emits and how the decision is made*.
**Operator inputs captured here:** asset configurations, cycle budget, zone sequencing, and
horizon structure, confirmed 2026-07-20. These resolve §5 Q1, Q2 and Q3 of the scope document.

---

## 0. What this adds to the M6 scope

Three changes, each following from the asset spec below:

1. The cycle cap becomes an **annual budget (~550 cycles/yr)** with the daily figure as a soft
   guideline, replacing the rolling-24h hard cap in `shared/bess_simulator.py:683-691`.
   1.5 cyc/day survives as a **reported metric**, not a constraint (§2.4).
2. The day-ahead forecast target changes from hourly price levels to **k-th order statistics
   of the intraday price distribution**, k set by the cycle budget (§4.2).
3. aFRR **activation energy** joins activation price as a forecast target, because it is the
   stochastic draw on the cycle budget (§4.3).

And one that follows from zone sequencing rather than the asset spec:

4. **P0 ingests both zones** even though DK2 is modelled first, because DK1's aFRR border data
   is on the same 90-day destruction clock (§2b.1), and the market taxonomy becomes a
   declarative per-zone registry rather than late filtering (§2b.2).
5. **D-1 and intraday are one nested decision, not two tracks.** Separate forecast models per
   horizon; a single two-stage decision layer carrying the D-1 commitment as state. D-1 is
   built first because intraday's value *is* the recourse value and is undefined without it
   (§2c).

---

## 1. Asset configurations

Two configurations, both **1 MW**, evaluated as a pair. Config A is already the
`BessConfig` default (`shared/bess_simulator.py:183-243`); config B is a `capacity_mwh`
override. No other field differs.

| | **A — 1 MW / 2 MWh (0.5C)** | **B — 1 MW / 4 MWh (0.25C)** |
|---|---|---|
| Usable SoC band (10–90%) | 1.60 MWh | 3.20 MWh |
| 1 full-cycle-equivalent (FCE) | 2.0 MWh to grid | 4.0 MWh to grid |
| FCE round trip @ 90% RTE | 2.00h dis + 2.22h chg = **4.22h** | 4.00h dis + 4.44h chg = **8.44h** |
| Theoretical max cycles/day | **5.68** | **2.84** |
| At 1.5 cyc/day | 6.3h/day (26% of day) | 12.7h/day (53% of day) |
| **Headroom above 1.5 cyc/day** | **×3.79** | **×1.89** |
| Annual throughput @ 550 cyc | 1100 MWh/yr | 2200 MWh/yr |

Arithmetic uses the config defaults: `round_trip_efficiency=0.90` split symmetrically across
legs (`leg_efficiency = 0.90**0.5`, per `shared/bess_simulator.py:646`), SoC band 10–90%.

### 1.1 The configs differ in exactly one revenue arm

Both are 1 MW. Capacity markets buy MW, not MWh — so **if both prequalify, both sell an
identical product and earn identical capacity revenue.** The entire economic difference lives
in the energy arm: arbitrage depth, and headroom to absorb aFRR activation.

Note also that 1.5 cyc/day is *not* a like-for-like comparison across the two. The budget is
defined against `capacity_mwh` (`full_cycle_equivalents`, `shared/bess_simulator.py:395-404`),
so config B receives **twice the daily energy budget at the same power rating**. Doubled energy
budget against an identical capacity arm is the whole experiment.

### 1.2 [VERIFY] — two assumptions that gate the comparison

- **Prequalification parity.** Nordic LER (Limited Energy Reservoir) rules impose endurance and
  energy-management obligations on batteries in FCR. Both durations are expected to clear the
  endurance bar, but the SoC-restoration obligations while committed bite harder on a 2h unit,
  and restoration itself draws on the cycle budget. Confirm against Energinet's prequalification
  terms before treating the two configs as capacity-identical — the "identical capacity revenue"
  claim in §1.1 depends on it.
- **What a warranty counts as a cycle.** The code defines an FCE as `discharge_MWh /
  capacity_mwh`, i.e. against nameplate. That is **1.32 traverses of the usable 10–90% band**
  (identical ratio for both configs, since the band scales with capacity). Supplier warranties
  usually also count throughput against nameplate — which would match — but if the warranty
  counts band traverses instead, the real budget is ~24% smaller than assumed here. Confirm
  before the 550 figure is trusted.

---

## 2. The cycle budget

### 2.1 It is a constraint, not a cost

At ~550 cycles/year the limit is warranty-shaped, not economic. Throughput is **free up to the
budget and forbidden beyond it**. There is no smooth per-MWh degradation price to subtract from
revenue, so the allocation layer must not model one — the earlier "argmax net of cycling cost"
framing is wrong for this asset and should not be implemented.

### 2.2 Annual, with the daily figure as a soft guideline

Confirmed operator decision. 550 cyc/yr averages to 1.51 cyc/day, so the annual budget permits
the daily figure *every* day — banking matters only for exceeding it on selected days.

The value of that flexibility is **asymmetric between the two configs**, and this is the main
quantitative result of this section:

- **Config A has ×3.79 headroom** above 1.5 cyc/day before hitting its physical ceiling. It can
  spike to 3–4 cycles on a volatile day and repay by running light through calm weeks.
- **Config B has ×1.89 headroom.** At 1.5 cyc/day it already occupies 53% of the day, and one
  FCE costs it 8.44h round trip. It is **power-limited near its own cycle ceiling** and cannot
  meaningfully spike.

So switching from a rolling-24h cap to an annual budget unlocks real option value for config A
and mostly cosmetic flexibility for config B. Expect the backtest to show the change earning
its keep almost entirely on the 0.5C unit. If it does not, suspect the allocation layer before
suspecting the finding.

This also matters commercially: if the supplier warranty genuinely is a rolling-daily cap rather
than an annual count, the backtest quantifies what that clause costs — a number to negotiate
with, and worth more on config A.

### 2.3 λ — the cycle budget is a reservoir

With an annual budget, the shadow price of a cycle is time-varying. Write **λ_t** = DKK per MWh
discharged, the dual of the budget constraint: the opportunity cost of spending a cycle now
rather than banking it. Every hourly decision then reduces to a single comparison —

> does this hour's revenue exceed λ_t × MWh drawn?

This is structurally the **water-value problem**, and the analogy is exact enough to be useful
rather than decorative. The marginal Nordic hydro producer prices FCR by computing the
opportunity cost of releasing water now versus later; you price cycles the same way, against
the same seasonal volatility. Two consequences:

- The hydro-scheduling literature (SDP, water-value tables) transfers directly. Do not invent a
  formulation.
- Reservoir levels appear on **both sides** of the model — as a feature driving Nordic capacity
  prices (scope §2 discussion of opportunity-cost pricing), and as the analogue for your own
  budget. Worth keeping conceptually distinct in code to avoid confusion.

**Implementation order — do not start with SDP.**
1. **Flat λ.** Solve for the single λ that exhausts 550 cycles over a backtest year. Cheap,
   and the honest baseline any seasonal scheme must beat.
2. **Seasonal λ.** λ per month, calibrated so expected annual throughput hits budget. Captures
   most of the winter-volatility banking value at a fraction of the complexity.
3. **State-dependent λ(t, budget_remaining).** Backward induction. Only if (2) shows material
   gains over (1).

Report the gain of each step over the previous. If step 2 does not beat step 1 on config A,
stop — the banking hypothesis in §2.2 is then falsified and the annual budget is not worth its
complexity.

### 2.4 1.5 cyc/day remains a reported metric

Operator requirement: the daily figure is how this asset is compared against external sources
and vendor benchmarks. Even once it stops being the binding constraint, **every backtest result
must report realised cycles/day** (mean, p50, p95, max) alongside annual throughput and budget
utilisation. `BacktestResult.full_cycle_equivalents` already computes the numerator; this is a
reporting addition, not a new calculation.

---

## 2b. Zones — DK2 first, DK1 not designed out

Confirmed operator decision: **model DK2 first, but keep DK1 live.** These are different
instructions for different layers, and conflating them is the main architectural risk in this
milestone.

### 2b.1 Ingest both zones now — the retention clock runs on DK1 too

The Tier-1 EDS datasets (`Forecasts_Hour`, `ElectricityProdex5MinRealtime`,
`AfrrBorderAvailableTransferCapacity`) are keyed by `PriceArea` or `BorderName`, so covering
both zones in P0 is a filter change, not extra work.

**This is urgent for the same reason DK2 is.** `AfrrBorderAvailableTransferCapacity` carries
`DK1-DE` among its borders and sits on the 90-day rolling window (scope §1.2). Filtering P0 to
DK2 destroys DK1 aFRR border history irrecoverably, one day per day, and DK1 would then start
its modelling phase from zero rather than from whatever P0 banked. Backfill and poll **both
zones** regardless of which zone gets modelled first.

### 2b.2 The market taxonomy is zone-dependent, not a late filter

DK1 and DK2 sit in **different synchronous areas** — DK2 in the Nordic system, DK1 in
Continental Europe. This is not a labelling difference; it changes the product set, the price
formation mechanism, and which features mean anything:

| | **DK2 (Nordic)** | **DK1 (Continental)** |
|---|---|---|
| Fast reserve product | FCR-N and FCR-D, up/down separate | FCR, symmetric (Continental design) |
| FCR clearing | Nordic, `FcrNdDK2` gives auction results | German FCR Cooperation — **regelleistung.net**, not in EDS |
| Scarcity drivers | Nordic inertia, Nordic hydro water value, SE nuclear trips | Continental frequency, German wind/solar, DE thermal |
| Day-ahead coupling | SE4 | DE — the dominant coupling |
| Key interconnectors | Öresund (SE4), Kontek (DE), Great Belt | Skagerrak (NO), COBRA (NL), Viking (GB), DE, Great Belt |

Two features already in the system illustrate the trap: `InertiaNordicSyncharea` and
`FFRdemandDK2` are **DK2 features with no DK1 meaning**, and `FcrNdDK2` has no DK1 analogue at
all. `BessConfig.capacity_markets` already defaults to a DK1-safe pair for exactly this reason
(`shared/bess_simulator.py:209-215`) — that comment is the existing precedent to generalise.

**Design directive:** make zone a first-class dimension with a **declarative zone → eligible-
market-set registry**, so product differences are data rather than branching logic scattered
through the feature builder and allocation layer. The feature builder must tolerate a feature
being *undefined for a zone* rather than assuming a fixed column set. This is cheap now and
expensive to retrofit — it is the difference between DK1 being a config entry and DK1 being a
rewrite.

The scope's `(zone, mtu_start)` feature-store key (scope §4 P1) already anticipates this; the
registry is what stops the key from being decorative.

### 2b.3 DK1 FCR is blocked on an external dependency — track it, don't bury it

DK1 FCR clears through the German FCR Cooperation auction. `FcrDK1` publishes realised prices
only, never the auction inputs, so **any serious DK1 FCR model needs regelleistung.net** — an
external scrape with a different data contract, different reliability, and its own ingestor.
Scope §2 flags this; it should be a tracked work item with its own investigation, not a
footnote, since it is the single thing standing between DK1 and parity with DK2.

Note this does **not** block the rest of DK1. Day-ahead, aFRR capacity and imbalance are all
available for DK1 today. Only the FCR target is gated.

**Verified 2026-07-20** by direct query — `AfrrReservesNordic` serves `PriceArea` ∈ {DK1, DK2,
FI, …} with `UpPriceEUR`/`UpPriceDKK` and `DownPrice*` both populated, so DK1 aFRR capacity
needs no FX handling and no new ingestion beyond widening the zone filter. Sample row
(2026-07-21T21:00 UTC):

| PriceArea | UpDemandMW | UpProcuredMW | UpPriceEUR | DownDemandMW | DownPriceEUR |
|---|---|---|---|---|---|
| DK1 | 90 | 90 | 2.16 | 20 | 0.01 |
| DK2 | 22 | 22 | 2.31 | 33 | 0.12 |

**DK1's aFRR up-demand is ~4× DK2's** (90 MW vs 22 MW) at comparable clearing prices. On this
snapshot the DK1 aFRR capacity pool is materially the larger of the two — direct support for
§2b.4, and a reason the DK1 track should not be allowed to drift indefinitely. **[VERIFY]**
whether the ratio holds across seasons and hours before treating it as a market-sizing fact;
one summer-night row is an indication, not a distribution.

### 2b.4 What DK1-first would have bought

Recorded so the decision is revisitable rather than re-litigated: DK1 has more wind, more
volatility and a larger price area, so its arbitrage and imbalance opportunity is plausibly
larger. It was passed over on **data availability, not economics** — the FCR blocker above plus
DK2's 4.7-year `FcrNdDK2` label history. If regelleistung.net ingestion lands earlier than
expected, revisit rather than defaulting to sequence.

---

## 2c. Horizons — D-1 first, intraday as recourse

Confirmed operator decision 2026-07-20, resolving scope §5 Q3. The question raised was whether
D-1 auction bidding and intraday activation positioning should be **separate tracks** or a
**merged model**. Neither, as posed — and the reason matters more than the conclusion.

### 2c.1 Separate tracks would double-count the asset

D-1 and intraday are not two markets pursued in parallel. They are **two decision points on one
1 MW asset**, and the second is conditional on the first. A D-1 commitment of 1 MW of FCR-D for
hour 14 is binding; there is then **zero MW free** for intraday positioning in hour 14.
Intraday is not an additional opportunity — it is the *residual after D-1*.

**Failure mode to design against:** running the two as independent tracks and summing their
revenues. Each track implicitly assumes it owns the whole battery, so the stacked figure is
both impressive and unachievable. This is the same structural error as treating the two configs'
revenue arms as additive (§1.1) or ignoring λ (§2.1) — the asset is a single scarce resource,
and any framing where revenue streams add without a shared constraint is wrong. Given such a
number would plausibly reach an investment case, this is recorded as a named hazard, not a
footnote.

### 2c.2 "Merged model" has two meanings with opposite answers

**Merged *forecast* — one model, horizon as a feature: no.** Horizons differ in feature
availability, and pooling them invites exactly the leak scope §1.3 identified. A D-1 model may
see only `ForecastDayAhead`; an intraday model sees `Forecast5Hour` / `Forecast1Hour`. Pooled
rows let short-horizon information contaminate long-horizon training. **Train separate models
per horizon, sharing one feature store and one codebase.**

**Merged *decision* — one optimizer spanning both stages: yes.** The correct object is a
two-stage stochastic program: commit capacity at D-1 under uncertainty, take recourse intraday
as forecasts resolve. Start with the myopic version (D-1 optimises assuming no recourse) and
measure the gap, per the λ escalation discipline in §2.3.

### 2c.3 The sequencing is logically forced, not merely pragmatic

**The value of the intraday track is the recourse value** — how much better the two-stage
decision performs than the one-stage one. That quantity is *undefined* without a D-1 policy to
measure against. Intraday cannot be evaluated first.

The engineering asymmetry runs the same way. D-1 is a once-daily batch job over datasets with
years of history. Intraday requires a near-real-time pipeline over the 5-min and millisecond
datasets — the ones on 90-day retention (scope §1.2), with the least history and the hardest
latency requirements. D-1 is materially the cheaper build.

**Expect recourse value to separate the two configs.** For config A, if FCR-D is near-cycle-free
and pays, the optimum is largely "sell capacity D-1 and hold" — thin intraday residual. Config
B's doubled energy budget (§1) leaves materially more to optimise intraday. A small recourse
value on config A is a **finding, not a failure**.

### 2c.4 Build directive

**One forecast layer, two horizons. One decision layer, two stages.**

1. Build **D-1 first** — FCR-D DK2 and day-ahead, per §4.
2. **Build the seam now.** The allocation interface takes the D-1 commitment as an explicit
   state input from the outset, even while stage 2 is a no-op. Same reasoning as the zone
   registry (§2b.2): cheap now, a rewrite if retrofitted.
3. P1's horizon parameter **is** the multi-horizon mechanism — not extra work, but the property
   P1 was already defined by (§3).
4. Measure recourse value once stage 2 lands.

*Acceptance:* the allocation layer's signature accepts a per-hour committed-MW state and
enforces that stage-2 decisions cannot exceed residual power or residual budget. A test asserts
that total committed MW across both stages never exceeds `power_mw` in any hour — this is the
§2c.1 hazard made mechanically impossible.

### 2c.5 Note on external comparability

The stated motive for considering both horizons was comparison against multi-market
optimisation models. Commercial optimisers do use the nested D-1-plus-recourse structure and
report revenue stacking, so both stages are eventually needed for like-for-like comparison. But
they also report against a **perfect-foresight ceiling**, which is already this design's
headline metric (§5) — so a D-1-only model is comparable on that axis from day one.
Benchmarking need not wait for intraday.

---

## 3. P1 — feature store

Unchanged from scope §4 P1, with one addition. The defining property remains the **leak-safe
as-of join**: the builder takes a horizon parameter and cannot physically emit a column
unavailable at that horizon.

**Write the horizon test first.** A unit test asserting that a horizon-`h` frame contains no
column derived from data published after `mtu_start - h` is the entire point of P1; everything
else is plumbing. `ForecastCurrent` must be droppable at load time (scope §1.3).

**Addition — typed event features.** The news pipeline's output joins here as sparse numeric
columns under the same as-of discipline (an event known at T is a feature from T+1 only):
announced-capacity-entering-within-90d, TSO demand-volume change, regime-change flag,
known-outage flag. This is the differentiated input and it must exist before P3, or the model
gets designed around its absence.

---

## 4. Forecast targets, revised

Scope §3's ranking stands (start with FCR-D DK2 and day-ahead). Three revisions follow from the
asset spec.

### 4.1 FCR-D is the default state for both configs

FCR-D is a contingency reserve: it pays to stand ready and activates only on large frequency
deviations, so it draws almost nothing from the cycle budget. Combined with §1.1 — identical
capacity revenue across both configs — **FCR-D capacity price is the highest-value single
target for both assets.** This confirms scope §3's "start with #1" on economic grounds, not
just data-availability grounds.

### 4.2 Day-ahead: forecast order statistics, not levels

Neither battery cares what 14:00 clears at. Config A cares about the spread between the
**3rd-highest and 3rd-lowest** hour of the day; config B about the **6th** — because that is
where each budget runs out (3.0 and 6.0 MWh/day at 1 MW).

So the target is the **k-th order statistic of the intraday price distribution**, with k the
cycle budget expressed in hours. This is easier than forecasting 24 levels, directly
decision-relevant, and composes with the quantile-LightGBM approach in scope §3 — the quantile
objective and pinball-loss evaluation carry over unchanged.

Note k becomes a function of λ_t once §2.3 step 2 lands: a banking day has a larger k. Keep k a
parameter, not a constant.

### 4.3 aFRR activation energy is a target in its own right

`afrr_activation_participation_rate: float = 0.3` (`shared/bess_simulator.py:231`) is the
weakest assumption in the current simulator, and it bites config A hardest. A static 30% hides a
real operational risk: an activation-heavy day can exhaust a 3.0 MWh budget and force the unit
out of the market, **forfeiting committed capacity revenue** — a loss the current model cannot
express. For config B's 6.0 MWh budget the same day is a nuisance.

Therefore forecast the **distribution of aFRR activation energy**, not just activation price.
Sizing an aFRR commitment against the cycle budget requires the tail, not the mean.

This is the second independent argument for scope §4 P0's urgency: `AfrrEnergyActivation` sits
on the 90-day rolling retention window (scope §1.2) and is the only source for both the price
and the energy target. Every unarchived day costs training data for the constraint that binds
config A.

---

## 5. P4 — allocation layer and economic evaluation

**One forecast, two decision layers.** The price-vector forecast is asset-agnostic — it does not
know the MWh rating. λ and the allocation are asset-specific. Keep that seam clean and a third
configuration costs nothing. **Do not train per-asset models.**

Allocation per hour, given the forecast price vector and λ_t:

- **FCR-D:** revenue ≈ capacity price × MW. Near-zero budget draw.
- **aFRR capacity:** capacity price × MW + E[activation revenue] − λ_t × E[activation MWh],
  sized so the activation tail (§4.3) does not breach budget.
- **Day-ahead arbitrage:** (discharge price − charge price / RTE) × MWh − λ_t × MWh.
- **Idle.**

**Falsifiable prediction, to check before trusting any fitted model:** config B's optimal
allocation should tilt toward energy arbitrage and config A's toward capacity, because config
A's scarcer budget implies a higher λ. If the fitted allocation does not reproduce this, the
error is upstream of the model.

**Headline metric: DKK captured versus a perfect-foresight ceiling and versus the P2 baseline** —
per config, never MAE. A model improving MAE while losing money on bids is a failed model and
only the economic metric surfaces it.

*Acceptance:* both configs backtested through `run_backtest` over the same period; results
report annual throughput, budget utilisation, realised cycles/day distribution (§2.4), and DKK
versus both ceiling and baseline.

---

## 6. Changes required in `shared/bess_simulator.py`

1. **Annual budget alongside the rolling-24h cap.** `max_cycles_per_day` and its rolling window
   (`:683-691`, `:819-821`) stay — they are the correct model of a daily-capped warranty and the
   comparison case for §2.2. Add an annual budget field and a λ-driven policy as an alternative
   mode. Persisted configs must keep reproducing their stored numbers, consistent with the
   existing `capacity_allocation` precedent (`:216-224`).
2. **Cycle reporting** per §2.4 on `BacktestResult`.
3. **Replace the static aFRR participation rate** with a forecast-driven activation-energy
   draw (§4.3), keeping the static rate as the default so existing runs reproduce.
4. **Capacity forfeit on budget exhaustion.** The failure mode in §4.3 — committed capacity lost
   because the budget ran out — is currently inexpressible. `cycle_cap_binding` (`:302-303`)
   flags the truncation but no revenue penalty follows.

---

## 7. Open questions

1. **Warranty cycle definition** — §1.2, gates the 550 figure.
2. **Is 550 a hard ceiling or a budget with an overrun price?** If exceeding it costs warranty
   coverage rather than being forbidden, λ acquires a natural upper bound and the optimisation
   changes shape.
3. **When does regelleistung.net ingestion get scoped?** §2b.3 — the only thing gating DK1 FCR,
   and the trigger for revisiting §2b.4.
4. **Which multi-market benchmark, specifically?** §2c.5 assumes comparison against
   perfect-foresight ceiling and P2 baseline. If a named external model or vendor figure is the
   intended comparator, its stacking convention needs checking against §2c.1 before any
   headline number is put beside it.

*Resolved 2026-07-20:* scope §5 Q1 (asset — §1), Q2 (zone — §2b), Q3 (horizon — §2c). No open
question now gates P0 or P1.
