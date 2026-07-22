# M6 P2 Design: baselines, and the bar P3 must clear

**Date:** 2026-07-21
**Status:** Design, ready to build. Fourth M6 design doc, after the dataset scope, the
allocation design, and the feature store design.
**Prerequisite:** P1 is merged (`shared/feature_store.py`). Target data verified complete on
2026-07-21 — FCR DK2 has 1693 days with zero gaps.

---

## 0. Why this phase exists

P2 produces one number per target: **the pinball loss a trivial method achieves.** That is the
bar. The scope doc's warning is the entire justification:

> Most published price-forecast work fails to beat lagged persistence plus hour-of-day dummies.
> If P3 does not clear these numbers, the answer is to **stop, not to add features.**

A weak baseline makes a mediocre model look good, which is how forecasting projects end up
deployed and losing money. So the baselines here are deliberately strong — the goal is to make
P3 *earn* its complexity, not to flatter it.

This phase is **baselines only**. No LightGBM, no feature selection, no tuning of anything that
could be called a model. If P2 starts looking like P3, it has failed at its job.

---

## 1. Target

**FCR-D DK2 capacity price**, both directions:

| | |
|---|---|
| source | `market_data_history`, `market='FCR'`, `zone='DK2'` |
| products | `up` (FCR-D up), `down` (FCR-D down) |
| granularity | hourly |
| unit | EUR/MW/h |
| coverage | 40,652 points (`up`), 39,911 (`down`), 2021-12-01 → present, **0 missing days of 1693** |
| horizon | `timedelta(hours=12)` — matches P1's D-1 default |

Day-ahead is **not** a P2 target (operator decision, 2026-07-21). It is easy to add once this
harness exists; do not build it now.

Note `product='price'` on this market is **FCR-N**, not FCR-D, and `d1_*` are the D-1 *early*
auction. Neither is the target. Do not mix them in.

---

## 2. Two constraints that shape the design

### 2.1 Window constraint — applies only once B3 lands (deferred, see §3.1)

`day_ahead` DK2 begins **2025-09-30** (`DayAheadPrices`' own start; `Elspotprices` would extend
it to 1999 but is deliberately not ingested — scope §4 P0 item 5). FCR-D begins 2021-12-01.

**Comparing baselines scored on different windows is meaningless.** So:

- **Primary comparison — the bar P3 must beat — is computed on the common overlap window
  (2025-09-30 → present, ~295 days), with all three baselines scored on identical folds.**
- Secondarily, report baselines 1 and 2 on the full 1693-day history, clearly labelled as a
  *different* window and not comparable to the primary table.

P3 must later be evaluated on the same primary window, or the comparison is void. State this in
the results output, not just in this document.

### 2.2 Dedupe is mandatory

`market_data_history` is append-only and holds duplicate revisions (P1 design §4.3 — verified:
`aFRR_lfc_limits` had ~53k rows over ~33k distinct points). Every target query must use
`DISTINCT ON (time) ... ORDER BY time, fetched_at DESC`, the pattern
`shared/db_manager.py:fetch_market_data` already uses. A naive `SELECT` silently double-counts.

---

## 3. The three baselines

All three emit **quantile** forecasts at τ ∈ {0.1, 0.25, 0.5, 0.75, 0.9} — not point forecasts.
The allocation design §3 requires distributions because a capacity bid is a decision under
asymmetric loss, and pinball loss only means something against quantiles.

**B1 — seasonal naive.** Point forecast is the target at `t − 24h` (and a variant at `t − 168h`;
report both). Quantiles come from the empirical distribution of that method's residuals over
the training fold only. This is the "lagged persistence" the scope doc warns about, and it is
usually the hardest to beat.

**B2 — conditional climatology.** Empirical quantiles of the target grouped by
`(hour-of-day, month)`, computed on the training fold only. No dependence on recent values at
all. Often beats naive methods on capacity auctions, where the diurnal shape is strong and
day-to-day persistence is weak.

**B3 — day-ahead-anchored quantile regression. DEFERRED to P3, deliberately — see §3.1.**

### 3.1 P2 adds no dependencies, and that is why B3 waits

Verified 2026-07-21: this repo has **no numerical stack whatsoever**. `pyproject.toml`'s
dependencies are httpx, tenacity, psycopg2, apscheduler, feedparser, trafilatura, anthropic,
qdrant-client, fastembed, fastapi, uvicorn, jinja2, prometheus-client, python-multipart. There
is no numpy, pandas, scipy, scikit-learn or statsmodels. (`fastembed` may pull numpy
transitively; **relying on a transitive dependency is not acceptable** — if numpy is wanted it
gets declared.)

This is a fork in the road, and the honest framing is that it is a question of *when*, not
*whether*: **P3 needs numpy and LightGBM and there is no way around that.** The options are to
add the stack now, or at P3.

**Decision: add nothing at P2.** Two reasons.

1. **B1 and B2 are exactly the bar the scope doc names.** Its warning is that most work fails to
   beat "lagged persistence plus hour-of-day dummies" — B1 *is* lagged persistence, B2 *is*
   hour-of-day dummies. They are the load-bearing baselines. B3 is a useful extra, not the bar.
2. **Both are trivially pure-Python.** Sorting, empirical percentiles and grouped means need no
   library. Adding a numerical stack to compute a percentile would be absurd, and dependency
   changes here are not free — the venv is baked into the Docker images, so they force a
   `--build` across four services.

So P2 ships dependency-free, and the numpy/LightGBM decision is made once, deliberately, at P3
— where it is genuinely unavoidable and where B3 can be built properly alongside the models it
belongs with.

**Consequence for §2.1:** with B3 deferred, the day-ahead overlap window no longer constrains
anything. B1 and B2 both run on the **full 1693-day history**, and that is the primary
comparison window. The overlap discussion in §2.1 applies only when B3 lands at P3 — at which
point the primary table must be recomputed on the common window, or the comparison is void.

---

## 4. Evaluation

**Pinball loss**, per quantile and averaged, per target product, per baseline.

**Walk-forward expanding-window CV.** Train on `[start, t)`, test on `[t, t + 30 days)`, advance
`t` by 30 days, repeat. **A random train/test split is invalid on this data** and must not
appear anywhere. The first fold needs a minimum train span — use 90 days — and folds before
that are skipped, not silently truncated.

Report a table: baseline × product × quantile → pinball loss, plus the fold count and the exact
window each number was computed on.

**Leak discipline.** Every baseline's parameters (residual quantiles, climatology bins,
regression coefficients) are fitted on the training fold only, never the full series. This is
the same property P1 enforces structurally, and it is just as easy to get wrong here — a
climatology computed over all data and then scored on a subset of it will look excellent and
mean nothing.

---

## 5. The coverage gate — do this before computing anything

Three separate data failures during M6 shared one shape: **a report said fine, the data said
otherwise.** The span check that hid a 25-day hole, the poller that logged healthy saves while
capturing zero history, the backfill that reported success while truncating 88 days.

So P2 does not trust its inputs. Before any baseline is fitted:

> Assert the target window has **zero missing days** with a per-day coverage query. If any day
> is missing, **fail loudly with the missing ranges** — do not compute a baseline on it, do not
> warn and continue.

A pinball loss computed over gap-ridden data is worse than no number, because it becomes the
bar for everything after it. This gate is a required part of the deliverable, not a nicety.

---

## 6. Acceptance

- The §5 coverage gate exists and is exercised by a test that feeds it a gapped series and
  asserts it raises.
- Pinball loss for B1 and B2 on the full 1693-day history, per product and per quantile, with
  the exact window and fold count stated alongside every number.
- **No new dependencies** (§3.1) — `poetry.lock` and `pyproject.toml` unchanged.
- Walk-forward folds only; a test asserts no test fold precedes its training data in time.
- Every baseline's parameters fitted per-fold, not globally; a test covers this for B2, where
  the mistake is easiest to make invisibly.
- Full suite green (`poetry run pytest`, currently 555 on `main`). Report pre-existing failures
  separately.
- Lint/format per `.pre-commit-config.yaml`.
- Results written to a committed markdown table in `docs/`, so the bar is a reviewable artefact
  and not something that has to be recomputed to be known.

---

## 7. Out of scope

- **Any model.** No LightGBM, no gradient boosting, no feature-store-driven learning. That is
  P3, and P3 does not start until these numbers exist.
- **Day-ahead as a target** (§1). Later.
- **The feature store.** P2 reads the target series directly. It deliberately does *not* consume
  `shared/feature_store.py` — a baseline that needs the feature store is not a baseline. B3's
  day-ahead input comes straight from `market_data_history`.
- **Tuning.** If a baseline has a knob, pick a defensible value and record it. Tuning baselines
  to be beatable is the failure mode this phase exists to prevent.
