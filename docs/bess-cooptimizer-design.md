# BESS Co-Optimizer Design: one budget, no double-selling, post vs. pre

**Date:** 2026-07-22
**Status:** Design, ready to build. Branches off `main` (`bess-cooptimizer-design`).
**Depends on:** `shared/bess_simulator.py` (existing threshold engine — kept, not replaced),
`shared/db_manager.py` (`save_bess_run`/`fetch_bess_ticks`, `BacktestResult`/`BessTick` contract),
`shared/datasets.py` (market/zone/product registry), `shared/units.py` (`currency_for`),
`shared/forecast_model.py` (drives the "pre" mode). All merged.

---

## 0. Why the current engine bids wrongfully

`shared/bess_simulator.py:run_backtest` computes three **independent** revenue streams and sums
them. Independence is the bug: a real battery has *one* power rating and *one* state-of-charge,
and every market competes for the same MW and the same MWh. Four concrete defects follow, each
verified against the code:

1. **Double-selling energy it cannot deliver (the load-bearing defect).** Capacity commitment
   subtracts only *power* — `arbitrage_power_mw = power_mw − capacity_commit_mw`
   (`bess_simulator.py:654`) — and reserves **zero energy/SoC**. The arbitrage leg is then free to
   drain the pack to `soc_min` for a discharge (`:810`) while the same tick collects FCR/aFRR
   **up**-capacity payments (`:754`) for MW the battery physically could not deliver, and collects
   **down**-capacity while charging to `soc_max`. The docstring concedes this outright: it "ignores
   any requirement to actually be able to deliver the reserved MW out of current SoC" (`:53-56`).
   Both reference implementations (`BessBidder`, the Alqueva DA-IDA-aFRR-mFRR optimizer) treat this
   as their #1 rule — *"No double-selling: headroom is computed by subtracting committed net
   position before offering capacity."* Ours has no such constraint. **This structurally
   overstates revenue.**

2. **Static capacity commitment, never co-optimized.** `capacity_commit_mw` is a fixed constant
   held back every period regardless of whether that MW is worth more standing as reserve or
   cycling energy. The `capacity_allocation` logic (`even`/`price_ranked`) only splits an
   *already-fixed* total *between* capacity markets — it never makes the actual multi-market
   decision: *this period, reserve or arbitrage?* That trade-off is the essence of multi-market
   bidding, and it is absent.

3. **Myopic outlier arbitrage, not value-optimal dispatch.** The z-score fires only on statistical
   outliers (`:795,805`). On a normal-shaped day with a real but sub-1σ peak/trough spread it
   **never trades**, and it can charge at a "low" that is not the day's actual minimum. As a "what
   would a battery have earned" benchmark this is simply wrong — a real backtest buys the cheapest
   feasible hours and sells the dearest, subject to SoC.

4. **No post/pre separation; single-market energy.** There is one causal heuristic — neither a
   clean perfect-foresight benchmark (post) nor a forecast-swappable pipeline (pre). Energy
   arbitrage reads `day_ahead` only, despite the multi-market goal.

---

## 1. What "correct" looks like — one shared budget

The fix is to stop summing independent streams and instead solve **one optimization per backtest
window** in which energy and reserve compete for a shared power rating and a shared SoC
trajectory. This is a small **linear program (LP)**, mirroring the reference repos' MILP but
without integer variables (see §3 for why the relaxation is exact here).

New module: `shared/bess_dispatch_milp.py`. Exposed through the *existing* entry point as a new
strategy, so nothing about the persisted-run contract changes:

```python
run_backtest(db, zone, start, end, config, strategy="cooptimized")  # new
run_backtest(db, zone, start, end, config)                          # unchanged: "threshold"
```

`strategy` defaults to `"threshold"` so every stored morning-brief and `/dashboard/bess` run keeps
reproducing exactly (the persisted `config` JSONB determinism guarantee in `save_bess_run` is
preserved). The co-optimizer emits the **same `BacktestResult`/`BessTick`** objects — one tick per
period, same revenue fields — so the dashboard, morning brief (`shared/bess_estimator.py`), and M6
economic-eval consume it with no changes.

---

## 2. The LP

**Indexing.** Periods `t = 0..T−1` over `[start, end]`, driven by the finest ingested energy
cadence in scope (15-min MTU where available, else hourly). Capacity legs `m ∈ capacity_markets`,
each with a direction (up/down/symmetric) resolved from its product string.

**Decision variables (all ≥ 0):**

| Variable | Meaning |
|---|---|
| `ch[t]`, `dis[t]` | grid charge / discharge power (MW) for energy arbitrage |
| `soc[t]` | state of charge (MWh) at start of period `t` |
| `cap_up[m,t]`, `cap_dn[m,t]` | reserve capacity committed to leg `m`, up/down (MW) |

**Objective — maximize total revenue** (per-currency; see §4):

```
Σ_t price_energy[t] · (dis[t] − ch[t]) · dt[t]                         # arbitrage
  + Σ_t Σ_m cap_price[m,t] · (cap_up[m,t] + cap_dn[m,t]) · dt[t]       # capacity reservation
  + Σ_t Σ_m act_price[m,t] · cap_up[m,t] · ρ · dt[t]                   # aFRR activation (ρ = participation rate)
```

**Constraints:**

```
soc[t+1] = soc[t] + η·ch[t]·dt[t] − dis[t]·dt[t]/η          # SoC balance, split round-trip η
soc_min ≤ soc[t] ≤ soc_max                                   # usable band (10–90 %)
ch[t] + dis[t] + Σ_m(cap_up+cap_dn)[m,t] ≤ power_mw          # ONE power budget — energy & reserve share it
```

**No-double-selling (the constraint the current code lacks):** committed up-reserve must be
deliverable out of stored energy for the standby period, and down-reserve must have room to absorb:

```
Σ_m cap_up[m,t] · T_act ≤ (soc[t] − soc_min) · η            # up-delivery feasible from SoC
Σ_m cap_dn[m,t] · T_act ≤ (soc_max − soc[t]) / η            # down-absorption feasible into SoC
```

`T_act` is the assumed sustained activation duration (hours) a reserve MW must be able to deliver —
a config parameter (default e.g. 1 h for FCR-style products; documented, not hard-coded). This is
the exact "subtract committed net position before offering capacity" rule from the references,
expressed as an SoC-headroom bound. It is what makes the battery unable to sell up-reserve while
draining itself for arbitrage.

**Cycle cap** (carried over from `max_cycles_per_day`): rolling-24 h discharge energy ≤
`capacity_mwh · max_cycles_per_day`, expressed as a sliding sum of `dis[t]·dt[t]`.

---

## 3. Why it is a pure LP (no integer variables)

The classic reason a battery dispatch needs a binary is to forbid simultaneous charge and
discharge. Here that binary is unnecessary: round-trip efficiency `η < 1` makes any simultaneous
`ch[t] > 0 ∧ dis[t] > 0` strictly revenue-losing (you pay to cycle energy through losses for no
price gain), so the optimum never does it and the LP relaxation is exact. Dropping the binary keeps
the solve fast and deterministic.

**Solver:** PuLP with the bundled **HiGHS** backend (open-source, no license, `poetry add pulp`;
HiGHS ships with recent PuLP). Sizing: a 30-day window at hourly resolution is ~720 periods →
low-thousands of variables → sub-second solve. At 15-min it is ~2,880 periods → still well under a
second. No performance concern at backtest scale.

**Determinism:** HiGHS on a fixed model is deterministic; we pin the solver options so a persisted
`config` reproduces the same dispatch, preserving the `save_bess_run` reproducibility contract.

---

## 4. Currency discipline — unchanged posture, enforced in the objective

The existing per-currency separation (`bess_simulator.py` module docstring §2) is **not** relaxed.
DKK and EUR legs are never summed. Concretely: the LP is solved **once per currency bucket** for
the capacity terms — or equivalently, capacity revenue is accumulated into
`capacity_revenue_by_currency` exactly as today and the objective's capacity term is grouped by
currency — so the optimizer never trades a EUR MW against a DKK MW on raw magnitude. A leg with no
declared currency in the registry still raises `ValueError` (fail loud). `BacktestResult`'s
`total_capacity_revenue_dkk` / `_eur`, `total_afrr_activation_revenue_eur`, and `currencies_present`
keep their current meanings.

> **Modeling note.** Because up/down capacity in different currencies genuinely cannot share one
> scalar objective, the clean formulation optimizes the **DKK-denominated stack and the
> EUR-denominated stack as separate LPs that share the same SoC/power budget only through the
> energy leg** (which is single-currency per zone). P1 will settle the exact decomposition; the
> constraint set above is currency-agnostic and holds either way.

---

## 5. Post vs. pre — both from one engine

Same LP, different price inputs:

- **Post (perfect-foresight benchmark).** Feed *actual* realised prices. The solution is the
  honest ceiling on what any battery could have earned in the real market over the window — an
  oracle, not a deployable policy. This is the "what it would have made" number.

- **Pre (forecast-driven).** Solve the LP on *forecast* prices (`shared/forecast_model.py`), fix
  the resulting schedule (`ch`, `dis`, `cap_*`), then **settle that schedule at actual prices.**
  This is the realistic forecasting evaluation: you bid on what you expected, you get paid what
  happened. Revenue is `Σ actual_price · scheduled_flow`.

The **post − pre gap is the monetary value of forecast skill** — the same headroom logic as the M6
economic-eval design (`docs/forecast-economic-eval-design.md` §1), now with a *feasible* dispatch
underneath it instead of an allocation-only lever.

> **Interaction with M6 P4.** The economic-eval doc explicitly built on the current simulator's
> "capacity always clears, allocation is the only lever" framing (its §0). The co-optimizer widens
> that: a forecast can now add value through **bidding feasibility** (committing reserve only where
> SoC headroom actually supports it), not just allocation. P4's headroom diagnostic still applies —
> it just runs on top of the co-optimized engine once P5 migrates the defaults. No rework of P4 is
> forced; it gains a second, more realistic lever to measure.

---

## 6. Multi-market scope & the data reality

Decision (confirmed with user): **full DA + intraday + imbalance** energy stack. Availability
audit against `shared/datasets.py` and a Nord Pool investigation:

| Stream | Ingested today? | Plan |
|---|---|---|
| Day-ahead energy | ✅ `day_ahead` | Core energy leg, P1. |
| Imbalance settlement | ✅ `imbalance` | Second energy stream, P3. |
| FCR / aFRR capacity | ✅ `FCR`, `aFRR_capacity`, `FFR` | Capacity legs, P1 (both directions). |
| aFRR activation | ✅ `aFRR_energy` | Activation term, P1. |
| **Intraday (IDA) prices** | ❌ **not ingested** | **New ingestion required — P4.** |

**The intraday finding.** There is no intraday price market in the registry (the only "intraday"
string is `ForecastIntraday`, a wind/solar *generation* forecast — not a price). Nord Pool
investigation:

- The public day-ahead endpoint
  (`data.nordpoolgroup.com/auction/day-ahead/prices?...&deliveryAreas=DK1,DK2`) works
  unauthenticated, but it duplicates data we already get from Energinet.
- **All Nord Pool intraday endpoints are subscription-gated** (v2 API `data-api.nordpoolgroup.com`,
  401/403 unauthenticated; continuous XBID via EPEX is ~€3,360/mo). Not viable for free ingestion.
- **Free path for IDA auction prices: ENTSO-E Transparency Platform** — which this project already
  integrates (README §3). It publishes the IDA1/IDA2/IDA3 auction prices at 15-min MTU for DK1/DK2.
  This is the recommended P4 source.
- **Continuous XBID (ID3/ID1 indices) stays out of scope** — paywalled with no free equivalent.
  Documented as a known gap, not silently dropped.

The engine is therefore built **intraday-ready**: energy markets are a pluggable list, so adding
the ENTSO-E IDA series in P4 is a *data* change (new ingestor + registry entry), not an *engine*
change. Imbalance is modeled as a settlement price the scheduled deviation is exposed to, not a
freely dispatchable market (a battery schedules in DA/IDA and is settled on imbalance for the
residual) — P3 fixes the exact DA↔imbalance coupling.

---

## 7. Phasing

| Phase | Deliverable | Gate |
|---|---|---|
| **P0** | This doc, on `bess-cooptimizer-design`. Nord Pool / ENTSO-E IDA audit (done — §6). | ✅ |
| **P1** | `shared/bess_dispatch_milp.py`: DA energy + FCR/aFRR capacity (both directions) + aFRR activation, no-double-selling headroom, **post** mode. Wired as `strategy="cooptimized"`. Unit tests incl. a **double-selling regression test**. | LP matches hand-checked tiny window. |
| **P2** | A/B report: co-optimized vs. threshold revenue on identical windows, quantifying the double-sell overstatement. `scripts/generate_*` report. | Perfect-foresight ≥ threshold on every window. |
| **P3** | Imbalance as second energy stream + **pre**/forecast mode (schedule on forecast, settle on actuals). Post−pre gap reported. | Pre ≤ Post always. |
| **P4** | Intraday: ingest ENTSO-E IDA1/2/3 (new ingestor + `datasets.py` entry), plug into the engine's energy-market list. | IDA series validated like other feeds. |
| **P5** | Migrate morning-brief + `/dashboard/bess` defaults to `strategy="cooptimized"`. | A/B report reviewed. |

---

## 8. Validation gates

- **Double-selling regression test** — a window where the threshold engine books up-capacity while
  discharging to `soc_min`; assert the co-optimizer cannot (headroom constraint binds, revenue is
  lower and *feasible*).
- **Perfect-foresight ≥ heuristic** on every test window (an optimum can never underperform a
  feasible heuristic; if it does, the model is wrong).
- **SoC feasibility** — assert `soc_min ≤ soc[t] ≤ soc_max` and headroom bounds hold at every tick
  of the returned trace.
- **LP vs. brute force** — on a 3–4 period hand-computable window, the LP optimum equals an
  exhaustive search.
- **Currency non-mixing** — a DK2 mixed-currency stack reports `currencies_present == {"DKK","EUR"}`
  with separate totals; no EUR MW is ever traded against a DKK MW in allocation.
- **Reproducibility** — a persisted `config` re-solves to identical dispatch (pinned HiGHS options).

---

## 9. Risks & open questions

- **`T_act` calibration.** The activation-duration parameter driving the headroom bound is a
  modeling choice per product (FCR vs. aFRR have different sustain requirements). P1 sets defensible
  defaults and exposes them on `BessConfig`; it materially affects how much capacity is feasible.
- **DA↔imbalance coupling (P3).** Whether imbalance is a passive settlement of DA-schedule
  deviation or a second dispatchable market changes the LP. Leaning passive-settlement (realistic
  for a price-taker BESS); to be finalized in P3.
- **Symmetric FCR products.** FCR-N / DK1 FCR are symmetric bands — one commitment obligates *both*
  up and down headroom simultaneously. The headroom constraints must bind on both sides for a
  symmetric leg (tighter than an up-only or down-only aFRR leg). P1 handles product symmetry
  explicitly from the product string.
- **Solver as a new dependency.** PuLP+HiGHS is pure-Python-installable and CI-friendly, but it is
  a new runtime dep in the Poetry lock and the ingestor/orchestrator images. P1 confirms it builds
  in the Docker images before committing to it.
