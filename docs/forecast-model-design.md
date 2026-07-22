# M6 P3 Design: quantile models for FCR-D DK2

**Date:** 2026-07-21
**Status:** Design, ready to build. Fifth M6 design doc. Depends on P1 (`shared/feature_store.py`)
and P2 (`shared/baselines.py`, `docs/forecast-baseline-results.md`), both merged/pending.
**Prerequisite reading:** the P2 results doc. P3's entire success criterion is *beating the P2
bar on the same window*, so the numbers there are not context — they are the target.

---

## 0. What P3 must do, and the one way it fails

Produce quantile forecasts of **FCR-D DK2 up/down capacity price** that beat the P2 baselines in
pinball loss **on the trailing-12-month window, against B2-rolling** — not against B1, and not on
full history.

The failure mode this phase must not commit: **beating a weak comparison.** P2's first run made
exactly that error (B1-only bar) and it was caught. So P3's headline comparison is against the
*strongest* baseline per quantile per product, which is B2-rolling almost everywhere. A model
that beats B1 but not B2-rolling has **failed**, and the correct response is to say so, not to
report the flattering comparison.

If P3 does not clear B2-rolling, the scope doc's instruction stands: **stop.** A rolling
climatology that the model cannot beat means the features add nothing over "recent prices by
hour and month," and that is a real, publishable finding — not a reason to keep adding features.

---

## 1. The dependency decision — made here, deliberately

P2 deferred this (baseline design §3.1); P3 is where it is unavoidable.

**Add two declared dependencies: `numpy` and `lightgbm`.**

- **numpy is already imported transitively via `fastembed` (2.5.1 present today), but undeclared.**
  Relying on that is unacceptable — a `fastembed` bump could drop or change it silently. Declare
  it explicitly at the version already resolved.
- **lightgbm** is the model. The scope doc §1.1 settled the class: gradient-boosted trees, not
  LSTMs or transformers, because the regime-consistent sample is small (and P2 showed it is
  *smaller* than the scope doc thought — §3 below).

**This forces a `docker compose build` across the four services that bake the venv into their
image.** It is the first dependency change of the milestone. Flag it in the PR; it is not a
restart, it is a rebuild, and a deploy that skips it ships a venv without lightgbm.

No pandas. The feature store returns `list[dict]`; convert to numpy arrays directly. Pandas would
be a second heavy dependency to do what `list[dict]` → `np.array` already does.

---

## 2. Model

**Quantile gradient boosting**, one LightGBM model per (product, quantile):

- `objective='quantile'`, `alpha=τ` for τ ∈ {0.1, 0.25, 0.5, 0.75, 0.9}
- Evaluated in **pinball loss**, the same metric and quantiles as P2 — identical, or the
  comparison is void.
- Modest capacity: small `num_leaves`, low `learning_rate`, `n_estimators` bounded by early
  stopping on a validation tail of each training fold. This is a small-data regime (§3); a deep
  model will memorise it.

**Quantile crossing must be handled.** Independent per-τ models can produce τ=0.9 < τ=0.5, which
is nonsensical for a bid decision. After prediction, **sort each row's quantile vector
ascending** (isotonic in τ). Assert non-crossing in a test. This is cheap and non-optional — the
allocation layer consumes P(price > bid) and cannot use a crossed quantile function.

---

## 3. Training window — the decision the P2 collapse forced

P2 established that FCR-D DK2 `up` fell **92% from 2022 to 2026**. The scope doc's premise that
FCR-D DK2 is "the target where history is not the binding constraint" (§1.1) is therefore **no
longer safe.** 4.6 years exist, but most of it describes a market that is economically extinct.

This is the same lesson as B2-expanding versus B2-rolling: **training on the full history biases
the model high in deployment conditions.** So:

**Bounded training lookback, not expanding.** Each walk-forward fold trains on a *trailing
window* ending at the fold boundary, not on all history before it. The lookback length is the
single most important hyperparameter in this phase.

- Report the model at **two lookbacks: 12 months and 18 months.** Not to pick the winner and
  hide the other — report both, because the gap between them is itself information about how fast
  the market is moving.
- Do **not** sweep lookback to minimise loss and report only the best. That is tuning against the
  test set, the exact sin P2 exists to prevent. Two pre-declared values, both reported.

**Evaluation window is the trailing 12 months of folds — identical to the P2 headline**, so the
numbers sit in the same table as the bar. Full-history evaluation is not required and is not the
bar (P2 design §2.1 reasoning).

---

## 4. Features, target, and the join

**Features:** `build_features(db, 'DK2', start, end, horizon=timedelta(hours=12))` — 34 columns,
verified live. Drop `zone` (constant) and `mtu_start` (the join key, not a feature). Declare
`hour_of_day`, `day_of_week`, `month`, `is_danish_public_holiday`, `is_after_d1_gate` as
LightGBM categorical features. Everything the feature store emits is already horizon-12h
leak-safe by construction (P1) — **P3 must not reach around the feature store to the raw tables
for a feature**, or it forfeits that guarantee.

**Target:** FCR-D DK2 `up` / `down` from `market_data_history`, deduped
`DISTINCT ON (time) ... fetched_at DESC` (via the `market_data` view / `fetch_series_values`,
the real dedupe home — the P2 design's `fetch_market_data` reference was a naming slip, corrected
in `shared/baselines.py`).

**Join on `mtu_start == target.time`.** Inner join: an hour with a feature row but no target (or
vice versa) is dropped, with the dropped count logged. Reuse P2's coverage gate on the target
side of the window before training — same discipline, same reason.

---

## 5. Reuse, don't reinvent

- **Fold generator:** reuse `shared/baselines.py`'s walk-forward splitter. Do not write a second
  one — two fold implementations that drift apart is how a model and its baseline stop being
  comparable.
- **Pinball loss:** reuse P2's implementation. Same function, or the comparison is meaningless.
- **`trailing_folds`:** reuse for the headline window.

Put the model in `shared/forecast_model.py`; the evaluation runner in
`scripts/generate_forecast_report.py` mirroring `scripts/generate_baseline_report.py`.

---

## 6. Acceptance

- Quantile LightGBM per (product, τ), both lookbacks (12mo, 18mo), evaluated on the trailing-12mo
  fold window with per-fold refitting.
- **Headline table: model vs the best baseline per (product, τ), pinball loss.** A `beats_bar`
  boolean column comparing to `min(B1, B2-rolling)` — the strongest baseline, never a cherry-
  picked weak one.
- Non-crossing quantiles, asserted by test.
- Walk-forward only; a test asserts no test fold precedes its training data, and that the
  training window respects the declared lookback (i.e. does not silently expand).
- Per-fold refitting — model params fit on each fold's training window only. Test it.
- Full suite green (`poetry run pytest`; P2 branch is at 598). Report pre-existing failures
  separately.
- `numpy` and `lightgbm` declared in `pyproject.toml`; `poetry.lock` regenerated. Note in the PR
  that a `docker compose build` is required.
- Results written to `docs/forecast-model-results.md`, with an explicit verdict line: **does the
  model beat the bar, yes or no, per product.** If no, say so plainly — that is a valid and
  useful outcome, not a failure of the phase.

---

## 7. Out of scope

- **All other targets.** FCR-D DK2 only, both directions. Day-ahead, aFRR, imbalance: later
  phases. One target proven end-to-end beats five half-built.
- **The allocation layer and λ** (allocation design §2.3, §5). P3 produces price quantiles; the
  economic evaluation that turns them into bids and runs them through `bess_simulator` is **P4**.
  Do not build it here.
- **Hyperparameter sweeps.** Two pre-declared lookbacks, modest fixed tree params. Tuning to the
  test window is the failure P2 guards against; it does not become acceptable at P3.
- **B3 / day-ahead-anchored regression.** Deferred here from P2 (baseline design §3.1); build it
  as a *third baseline* alongside the model, now that numpy exists, only if cheap — otherwise
  defer again and say so. It is not the bar; B2-rolling is.
- **Feature importance / interpretation.** Worth doing, but after a model clears the bar. If it
  doesn't clear the bar, importances explain noise.
