# M6 P4 Design: economic evaluation — does the forecast bid better?

**Date:** 2026-07-22
**Status:** Design, ready to build. Sixth M6 design doc, and the capstone: it answers whether the
forecasts are *useful*, not merely *accurate*. Branches off `main`.
**Depends on:** P3 (`shared/forecast_model.py`), P3b (day-ahead), P2 (`shared/baselines.py`),
and `shared/bess_simulator.py`. All merged.

---

## 0. The reframing that the simulator forces

P3/P3b scored forecasts on pinball loss and found they don't beat trivial baselines at the
quantiles that matter. P4 asks the different, and ultimately more important, question: **does
acting on the forecast capture more money than acting on a trivial baseline?** A model can lose
on price accuracy and still win on the *decision*, because a decision needs only the right
*ranking* of options, not the right *level*.

But `shared/bess_simulator.py` constrains what "acting on a forecast" can even mean, and this
must be understood before building (docstring §2, verified):

> Capacity revenue is `procured_clearing_price * committed_mw * hours`, and the commitment
> **always clears at the realised price.** There is no bid/clear model.

So a forecast **cannot** add value by bidding a better price — you always get the realised price.
It can add value in exactly one place: **allocation** — deciding, each period, *which market to
commit capacity to* and *how much power to hold for capacity versus arbitrage*. The simulator
already has a trailing-price allocation mode (`capacity_allocation="price_ranked"`,
`_leg_relative_strength`, causal) — which is a **persistence forecast in disguise.** That is the
economic analog of the P2 baseline, and it is what the model must beat.

---

## 1. The diagnostic that comes first — and may end the phase

Before evaluating any model, compute the **headroom**:

> **headroom = (revenue under perfect-foresight allocation) − (revenue under trailing-persistence
> allocation)**, per asset config, over the eval window.

Perfect-foresight allocation is an **oracle**: each period it allocates using the *actual*
next-period clearing prices. It is not a deployable policy — it is the ceiling on what *any*
forecast, however perfect, could add over persistence.

**If headroom is small, the phase is over and the answer is "no".** If a perfect forecast barely
beats trailing persistence, then no model — P3's, a better one, anything — can add economic value
through allocation here, and the honest conclusion is that forecasting doesn't help the decision
on this asset in this regime. Given P3/P3b already showed these markets are quite persistent
(trailing baselines were hard to beat), a small headroom is a live possibility, and reporting it
is a real result, not a failure. This is P2's "if you can't beat persistence, stop" discipline
lifted to the economic level.

Compute and report headroom **first and prominently**. Only if it is material does the model
policy (§2) become worth interpreting.

---

## 2. The three policies, one comparison

For each asset config, run `bess_simulator` over the same window under three allocation policies,
identical in everything except how capacity is allocated each period:

| policy | allocation rule | role |
|---|---|---|
| **trailing** | `price_ranked` — each leg by its own recent trailing price (existing, causal) | the baseline to beat |
| **model** | by the P3/P3b **forecast** of each leg's next-period price (leak-safe, horizon-12h) | the thing under test |
| **oracle** | by the *actual* next-period price | the ceiling (§1); never a real policy |

Headline metric, per config: **EUR captured**, and the model's position in the band —
`(model − trailing) / (oracle − trailing)`, the *fraction of the available headroom the model
captures*. Positive means the forecast beats persistence; near 1 means it approaches the oracle;
≤ 0 means it is worse than doing nothing clever.

Report `even` allocation too as a floor, for context.

---

## 3. Scope — the decisions the models can actually inform

The two models are FCR-D DK2 (up/down) and day-ahead DK2. So the allocation P4 evaluates is
exactly what they cover, and **nothing they don't**:

- **FCR-D up vs down** split — both legs modelled.
- **Capacity (FCR-D) vs arbitrage (day-ahead)** power split — FCR-D modelled; the arbitrage
  opportunity is derivable from the day-ahead forecast (forecast intraday spread).
- **Exclude aFRR_capacity from the P4 config** — there is no aFRR model, so including it would
  force a non-forecast allocation for one leg and muddy the comparison. A config of FCR-D up/down
  + day-ahead arbitrage keeps *every* allocation decision forecast-informed. State this
  restriction explicitly.

Both asset configs from the allocation design: **1 MW / 2 MWh (0.5C)** and **1 MW / 4 MWh
(0.25C)**, `max_cycles_per_day` at its default. The allocation design predicted the 0.25C unit
tilts toward arbitrage and the 0.5C toward capacity — P4 is where that prediction is tested. Note
whether it holds.

**Currency:** FCR-D DK2 and day-ahead DK2 are both **EUR** — clean comparison, no FX. (The
simulator's per-currency buckets already prevent EUR/DKK conflation; this config avoids the issue
entirely by staying in one zone/currency.)

**Eval window:** the FCR-D ∩ day-ahead overlap, **2025-09-30 → present** (~10 months). This is
the post-collapse regime, which is the deployment-relevant one anyway.

---

## 4. Forecast source — reuse P3/P3b, leak-safe

The `model` policy consumes a **precomputed forecast series**: run P3's/P3b's walk-forward
quantile prediction over the eval window (leak-safe, horizon=12h, exactly as those phases already
produce), yielding a `(time → forecast quantiles)` map per leg. The simulator's `model`
allocation looks up the forecast for each tick.

- **Which quantile drives allocation?** The **median (τ=0.5)** for ranking expected revenue is the
  natural default. But note P3/P3b found the model's edge was at the **low tail (τ=0.1)** — so
  also report a variant allocating on a low quantile (a conservative "what will this market at
  least pay" ranking). If the low-tail variant beats the median variant, that is a real and
  publishable finding about *where* the forecast's value lives.
- **Leak discipline:** the `model` policy uses only leak-safe forecasts (horizon-12h, already
  enforced by the feature store). The `oracle` uses actual future prices and is therefore an
  explicit lookahead — it must be **structurally impossible to select the oracle as a deployable
  policy**, and a test must assert the `model` and `trailing` policies never read a price at or
  after their decision tick.

---

## 5. Acceptance

- `headroom` computed and reported first, per config (§1), as EUR and as a fraction of trailing
  revenue.
- Three policies (trailing, model, oracle) + `even` floor, per asset config, over the overlap
  window, EUR captured.
- The band metric `(model − trailing) / (oracle − trailing)` per config, both quantile variants
  (median, low-tail).
- Leak test: `model`/`trailing` never read a same-or-future-tick price; `oracle` is the only
  lookahead path and cannot be reached as a real policy.
- Cycle reporting (realised cycles/day) alongside every result — the allocation design's §2.4
  requirement, so runs stay comparable to external benchmarks.
- Full suite green (`poetry run pytest`; `main` at 691). Report pre-existing failures separately.
- `docs/forecast-economic-eval-results.md`: headroom first, then the policy comparison, then an
  explicit verdict — **does acting on the forecast capture more EUR than trailing persistence,
  and is there enough headroom for it to matter?** If not, say so plainly; that is the honest
  economic capstone to the modelling arc.
- No new dependencies. No tuning of the allocation rules against the eval window's own numbers.

---

## 6. Out of scope

- **The λ / annual-budget / water-value optimisation** (allocation design §2.3). P4 uses the
  existing rolling-24h `max_cycles_per_day`. The seasonal-λ refinement is a later phase; flag it
  where the cycle cap binds.
- **A bid/clear model.** The simulator assumes commitments clear; building probabilistic
  clearing (using the quantile forecast as P(clear)) is a real extension but a separate phase —
  note it, because it is the *other* place a quantile forecast could add value that this
  simulator currently can't express.
- **aFRR, other zones, DKK legs.** DK2 EUR only (§3).
- **Supply-event features.** Still accruing; not consumed here.
- **15-minute granularity.** Hourly, consistent with P3b.
