# M6 P3b Design: day-ahead price model — the fundamentals test

**Date:** 2026-07-22
**Status:** Design, ready to build. Branches off `main` (has P2 baselines + P3 model harness).
**Depends on:** P1 (`shared/feature_store.py`), P2 (`shared/baselines.py`), P3
(`shared/forecast_model.py`) — all merged. This is a **retarget of the P3 harness**, not new
modelling machinery.

---

## 0. The question this answers

P3 showed fundamentals don't beat a rolling climatology on FCR-D DK2. But FCR-D is a special
case: a **capacity** price collapsing under battery entry, driven by supply the fundamentals
can't see. Day-ahead **energy** price is the opposite kind of market — it is *directly* driven by
wind, solar, and load, exactly the fundamentals the feature store carries, and it has **not**
structurally collapsed.

So this is the clean test of the modelling approach itself:

> **Do fundamentals beat a climatology on a normal, fundamentals-driven market?**

- **If yes:** the approach works, and FCR-D's failure is specifically its cannibalisation — which
  is precisely what the supply-event features (separate track) exist to address.
- **If no:** the problem is deeper than FCR-D, and the whole feature/model approach needs
  rethinking before more targets.

Either result is decision-relevant. Report the verdict plainly, exactly as P3 did — a loss here
is a finding, not a failure to paper over.

---

## 1. Target

**Day-ahead price, DK2** (`market='day_ahead'`, `zone='DK2'`, `product='price'`).

- Source is **15-minute** (96 points/day, uniform across its DB history from 2025-09-30 —
  verified). The feature store is **hourly** (`mtu_start`). So the target is **aggregated
  15-min → hourly by mean** to meet the feature grid. This is a v1 simplification: hourly
  day-ahead loses the intraday shape a battery ultimately cares about, but it makes the
  experiment directly comparable to P3's hourly FCR-D setup. Note 15-min/intraday is a later
  refinement (consistent with the intraday deferral in the allocation design).
- Unit: EUR/MWh. Quantiles τ ∈ {0.1, 0.25, 0.5, 0.75, 0.9}, pinball loss — identical to P3.

**Coverage:** the target is complete — 0 missing of 294 days (2025-10-01 → present), verified.
The P2 coverage gate still runs on the target before training; it must pass.

---

## 2. The binding constraint: feature history, not target history

The day-ahead *target* extends to 2025-09-30, but the *features* (`wind_solar_forecast`,
`prodex`) only reach back to **2025-09-25** after the recent backfill (2 genuine Energinet
publication gaps: 2025-11-22/23, which the feature store tolerates as nulls). So the usable
window is **~294 days**, and walk-forward with a 90-day minimum initial train + 30-day folds
yields only **~6–7 folds**. That is a small evaluation, and the results doc must say so — a
6-fold verdict is weaker evidence than P3's 12, and honesty about that is part of the
deliverable.

This is also why the FCR-D collapse lesson (bounded lookback) matters less here: 294 days barely
exceeds a 12-month lookback, and day-ahead has no regime collapse to bias an expanding fit. Use
the **same bounded-lookback mechanism P3 already has**, reported at a single **12-month
lookback** (≈ all available history). Do not invent a second window scheme.

---

## 3. Reuse — this is a retarget, not a rebuild

- **`shared/baselines.py`** hardcodes `TARGET_MARKET = "FCR"` and directional products
  (`up`/`down`). Generalise the target into a small config (market, zone, product, and an
  optional 15-min→hourly aggregation step) so both FCR-D and day-ahead flow through the *same*
  fold generator, pinball loss, coverage gate, and baseline fits. **Do not fork the harness** —
  a second copy is how model and baseline silently drift out of comparability.
- **Baselines: B1 (seasonal naive t-24h, t-168h) and B2-rolling only.** B3 (day-ahead-anchored
  regression) is meaningless here — day-ahead *is* the target. The bar is `min(B1, B2-rolling)`,
  same as P3.
- **`shared/forecast_model.py`** quantile LightGBM, walk-forward, per-fold refit, non-crossing
  quantiles — all reused. Only the target source changes.

Day-ahead is a single series (not directional), so there is one product, not two.

---

## 4. Acceptance

- Day-ahead runs through the reused P2/P3 harness (same fold generator, pinball loss, coverage
  gate) with the 15-min→hourly target aggregation, at a 12-month lookback.
- Headline: model vs `min(B1, B2-rolling)`, `beats_bar` per τ, and an explicit verdict line.
- The retarget config does **not** regress FCR-D: the existing P3 numbers must still reproduce
  through the generalised path. A test asserts the FCR-D target config yields the same series the
  old hardcoded path did.
- Non-crossing quantiles, walk-forward, per-fold refit — reuse P3's tests; add day-ahead cases.
- Full suite green (`poetry run pytest`; `main` is at 631). Report pre-existing failures
  separately.
- `docs/forecast-day-ahead-results.md`: the numbers, the fold count (stated as a small-sample
  caveat), the verdict, and an **explicit comparison to P3's FCR-D result** — because the
  contrast between the two markets is the actual finding.
- Lint/format per `.pre-commit-config.yaml`. No new dependencies (numpy/lightgbm already present).

---

## 5. Out of scope

- **15-minute / intraday target.** v1 is hourly (§1). Intraday is deferred (allocation design).
- **The allocation layer / λ / bess_simulator economic eval.** That is P4, across both targets.
- **Other zones / markets.** DK2 day-ahead only.
- **Supply-event features.** The parallel track (PR #12); not consumed here.
- **Tuning.** Fixed modest tree params, one declared lookback. Same anti-tuning discipline as P2/P3.
