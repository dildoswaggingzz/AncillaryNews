"""
M6 P2: the baselines P3 must clear (docs/forecast-baseline-design.md).

Target: **FCR-D DK2 capacity price** (`market='FCR'`, `zone='DK2'`,
`product` in `('up', 'down')`), hourly, EUR/MW/h (design §1). Two
baselines, both emitting **quantile** forecasts at `QUANTILES` (design §3):

- **B1 -- seasonal naive** (`fit_seasonal_naive`/`SeasonalNaiveBaseline`):
  point forecast is the target at `t - lag` (`lag` = 24h or 168h, both
  reported -- design §3's "report both", not a tuned choice), quantiles are
  that point forecast plus the empirical distribution of the method's own
  residuals **on the training fold only**.
- **B2 -- conditional climatology** (`fit_conditional_climatology`/
  `ClimatologyBaseline`): empirical quantiles of the target grouped by
  `(hour-of-day, month)` in **Europe/Copenhagen local time** -- matching
  `shared/feature_store.py`'s `_calendar_features` convention that Danish
  demand/generation (and, by extension, ancillary-price) patterns follow
  local clock time, not UTC; the design doesn't pin a timezone down, so this
  is a documented, non-tuned choice, not a hidden default. A `(hour, month)`
  bucket with no observations in the training fold (structurally guaranteed
  for the earliest folds -- a 90-day minimum initial train span cannot cover
  every month) falls back to the training fold's own unconditional
  quantiles; this is a defensible backoff, not a knob to tune, and is
  covered by `test_climatology_falls_back_to_overall_quantiles_for_an_unseen_group`.

  **Two variants, both reported (coordinator directive, post first-results
  review):** the default is **expanding** (`lookback=None`, everything from
  `train_start`), but FCR-D DK2's mean clearing price fell from ~€73 (2021)
  to ~€5 (2026) -- verified in `docs/forecast-baseline-results.md`'s yearly
  table -- an order-of-magnitude structural decline (battery-fleet growth
  cannibalising a market with fixed TSO demand volume), not noise. An
  expanding climatology trains on the whole €70 era to predict the €5 era,
  which is a strawman baseline dressed as a hard one, not the "structurally
  conservative, no dependence on recent values" property the design
  actually wants. `fit_conditional_climatology_rolling` (`lookback=
  B2_ROLLING_LOOKBACK`, 180 days trailing, still ending at `train_end`,
  never crossing into the test fold -- same leak discipline as the
  expanding variant) is the regime-adaptive alternative; both are reported
  side by side so the *gap between them* is itself the evidence for the
  regime shift, not just an assertion about it.

B3 (day-ahead-anchored regression) is explicitly out of scope here --
deferred to P3 (design §3.1/§7), which is also where this repo's numpy/
LightGBM dependency gets added. **This module adds nothing to
`pyproject.toml`/`poetry.lock`** -- sorting and empirical percentiles need
no library (design §3.1), so `_empirical_quantile` below is hand-rolled
(matches `numpy.percentile`'s default `'linear'` interpolation method, for
anyone who later cross-checks it against a real numerical stack, but is
implemented with none).

**The coverage gate (design §5) is the first thing this module defines, and
tests/test_baselines.py exercises it first, per the design's build order.**
Three separate M6 data failures shared one shape -- "a report said fine, the
data said otherwise" -- so nothing here trusts its input series. Before
fitting or scoring, callers must run `assert_full_daily_coverage`/
`fetch_and_assert_daily_coverage` over the exact window they intend to
score; both **raise with the missing day ranges** on any gap rather than
warn and continue. This caught something real: the design doc's own §1
coverage claim ("0 missing days of 1693") is **verified true for `up`, but
verified FALSE for `down`** -- `down` has no data for its first 30 days
(2021-12-01..2021-12-30 inclusive; real data begins 2021-12-31), confirmed
live 2026-07-21 via the exact per-day query this module's coverage gate
runs. Per the coordinator's follow-up review, both products are now scored
on one **common** window, `2021-12-31 -> present` (not a per-product window
computed by the report script) -- `down`'s true start, costing `up` 30 days
(1.8% of its history) in exchange for identical, directly comparable folds
across both products. The gate still runs, unmodified, against that common
window for both products before anything is fit -- it is expected to pass
now, and stays exactly as strict as before if it ever doesn't (nothing in
this module silently retries or narrows a window on a `CoverageGapError`
any more; that adjustment, when it existed, lived only in the
report-generation script, never here). See
`docs/forecast-baseline-results.md`'s header for the full note.

**Walk-forward CV only** (design §4): `walk_forward_folds` builds
non-overlapping, chronologically-ordered `[train_start, train_end) /
[test_start, test_end)` folds, expanding-window (`train_start` is always
the caller's overall `start`, `train_end` grows), with a minimum 90-day
initial train span (design's own number) -- folds before that span is
reached are skipped, never truncated. **A random train/test split must
never appear anywhere in this module**, and
`test_no_test_fold_precedes_its_training_data` is the regression test for
that. Every baseline's parameters are fit **per fold, on that fold's
training window only** (`fit_seasonal_naive`/`fit_conditional_climatology`
both take an explicit `train_start`/`train_end` and never see the test
window's own values) -- `run_walk_forward` is the harness that ties folds,
per-fold fitting, and per-quantile pinball loss together for one baseline
config at a time.

**The headline bar is regime-recent, not a full-history average**
(coordinator directive): `trailing_folds` selects the suffix of a folds
list within a trailing span (12 months, for the headline reported in
`docs/forecast-baseline-results.md`) of the last fold's own `test_end`. A
full-history walk-forward average over 2021-2026 is reported too, but only
as a secondary, explicitly-labelled "spans a regime change, not a
deployment-relevant target" number -- see that document's yearly-mean table
and per-fold-over-time breakdown, which exist specifically so the price
collapse is *visible* in the output rather than only asserted in prose.

**No feature-store usage** (design §7) -- this module reads the target
series directly via `DatabaseManager.fetch_series_values(...,
history=False)`, the same `market_data` view read
(`DISTINCT ON (time, market, zone, product) ... ORDER BY ..., fetched_at
DESC`, `init-db/01-init.sql`) `shared/feature_store.py` uses for the design
§2.2 dedupe requirement; the design references a `fetch_market_data` method
that does not exist verbatim in `shared/db_manager.py` today (flagged in
the build report) -- the dedupe pattern it describes is this view read, not
a differently-named method.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

# --- target (design §1) ---------------------------------------------------

TARGET_MARKET = "FCR"
TARGET_ZONE = "DK2"
TARGET_PRODUCTS: tuple[str, ...] = ("up", "down")
# Matches P1's D-1 default (design §1). Purely documentary/contextual here:
# neither baseline below consults a decision-time cutoff the way
# shared/feature_store.py's RULE-A/RULE-B joins do -- B1's shortest lag
# (24h) and B2's climatology (no recency dependence at all) are both safely
# clear of a 12h horizon by construction, not because this constant gates
# anything computationally in this module.
TARGET_HORIZON = timedelta(hours=12)

QUANTILES: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")

# B1's two lag variants (design §3: "report both", not a tuned choice).
SEASONAL_NAIVE_LAGS: dict[str, timedelta] = {
    "t-24h": timedelta(hours=24),
    "t-168h": timedelta(hours=168),
}


# --- §5: the coverage gate --------------------------------------------------


class CoverageGapError(Exception):
    """Raised when a target window has one or more missing calendar days (design §5)."""


def _missing_day_ranges(present_days: set[date], start: date, end: date) -> list[tuple[date, date]]:
    """
    Contiguous `[first, last]` missing-day ranges within `[start, end]`
    (both inclusive -- a day-granularity window, distinct from
    `shared/feature_store.py`'s half-open hourly-MTU convention), given the
    set of days that ARE present. Pure function over a set of dates so it,
    and everything built on it, is testable with a synthetic fixture and
    never needs a database (design's "unit tests must not require the
    database" constraint).
    """
    missing: list[tuple[date, date]] = []
    range_start: date | None = None
    d = start
    while d <= end:
        if d not in present_days:
            if range_start is None:
                range_start = d
        elif range_start is not None:
            missing.append((range_start, d - timedelta(days=1)))
            range_start = None
        d += timedelta(days=1)
    if range_start is not None:
        missing.append((range_start, end))
    return missing


def assert_full_daily_coverage(present_days: set[date], start: date, end: date) -> None:
    """
    Design §5's coverage gate, as a pure function: raises `CoverageGapError`
    -- naming every missing day range -- if any calendar day in
    `[start, end]` (inclusive) is absent from `present_days`. Never warns
    and continues; a caller that wants to compute anything past this point
    must not catch this and fall back silently either.
    """
    missing = _missing_day_ranges(present_days, start, end)
    if missing:
        formatted = ", ".join(f"{a.isoformat()}..{b.isoformat()}" for a, b in missing)
        raise CoverageGapError(
            f"target window [{start.isoformat()}, {end.isoformat()}] has "
            f"{len(missing)} missing day range(s): {formatted}"
        )


def fetch_and_assert_daily_coverage(
    db: DatabaseManager, market: str, zone: str, product: str, start: datetime, end: datetime
) -> None:
    """
    DB-backed wrapper around `assert_full_daily_coverage`: a real per-day
    query (`DatabaseManager.fetch_daily_aggregates`, which groups the
    already-deduped `market_data` view by `date_trunc('day', time)` --
    design §2.2's dedupe requirement, satisfied by that view's own
    `DISTINCT ON (time, market, zone, product) ... fetched_at DESC`) decides
    which calendar days have at least one row; any day in `[start.date(),
    end.date()]` with zero rows raises `CoverageGapError`. Call this before
    fitting or scoring any baseline on `[start, end]` (design §5) -- never
    after, and never catch-and-continue past it.
    """
    rows = db.fetch_daily_aggregates(market, zone, product, start, end)
    present_days = {r["day"].date() for r in rows if r["sample_count"]}
    assert_full_daily_coverage(present_days, start.date(), end.date())


def fetch_target_series(
    db: DatabaseManager, product: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """
    Ascending-by-time `(time, value)` pairs for FCR-D DK2 `product`
    (`'up'`/`'down'`) in `[start, end]`, nulls dropped, deduped to the
    latest revision per `time` via `fetch_series_values(history=False)`'s
    `market_data` view read (design §2.2). Callers must run
    `fetch_and_assert_daily_coverage` over the same window first.
    """
    if product not in TARGET_PRODUCTS:
        raise ValueError(f"product must be one of {TARGET_PRODUCTS}, got {product!r}")
    rows = db.fetch_series_values(
        TARGET_MARKET,
        TARGET_ZONE,
        product,
        limit=200_000,
        time_from=start,
        time_to=end,
        history=False,
    )
    return sorted(
        ((r["time"], r["value"]) for r in rows if r["value"] is not None), key=lambda kv: kv[0]
    )


# --- pinball loss and empirical quantiles -----------------------------------


def pinball_loss(actual: float, predicted: float, tau: float) -> float:
    """
    Quantile (pinball) loss of `predicted` against `actual` at quantile
    `tau` -- the metric the whole design exists to compute a bar for
    (design §4). `0` iff `predicted == actual`; asymmetric otherwise,
    penalising under-prediction by `tau` and over-prediction by `1 - tau`.
    """
    diff = actual - predicted
    return max(tau * diff, (tau - 1.0) * diff)


def _empirical_quantile(values: list[float], tau: float) -> float:
    """
    Linear-interpolation empirical quantile of `values` at `tau` --
    matches `numpy.percentile`'s default `'linear'` method (design §3.1:
    "sorting and empirical percentiles are pure Python", not a reason to
    add numpy). Raises `ValueError` on an empty sequence rather than
    returning a sentinel -- an empty training sample means the caller asked
    for a quantile that cannot be computed, not that it's zero.
    """
    if not values:
        raise ValueError("cannot compute an empirical quantile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = tau * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    frac = pos - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


# --- walk-forward folds (design §4) -----------------------------------------


@dataclass(frozen=True)
class Fold:
    """
    One walk-forward fold: fit on `[train_start, train_end)`, score on
    `[test_start, test_end)`. `train_end == test_start` always (contiguous,
    no gap and no overlap) -- see `walk_forward_folds`.
    """

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass(frozen=True)
class WalkForwardConfig:
    """
    Knobs for `walk_forward_folds`/`run_walk_forward`, dataclass-config
    style (matching `shared/bess_simulator.py`'s `BessConfig` and
    `shared/feature_store.py`'s `FeatureStoreConfig` convention) --
    defaults are the design's own numbers (§4: 30-day test span/step, 90-day
    minimum initial train span), not tuned against this data.
    """

    min_train_span: timedelta = timedelta(days=90)
    test_span: timedelta = timedelta(days=30)
    step: timedelta = timedelta(days=30)
    quantiles: tuple[float, ...] = QUANTILES

    def __post_init__(self):
        if self.min_train_span <= timedelta(0):
            raise ValueError("min_train_span must be positive")
        if self.test_span <= timedelta(0):
            raise ValueError("test_span must be positive")
        if self.step <= timedelta(0):
            raise ValueError("step must be positive")
        if not self.quantiles:
            raise ValueError("quantiles cannot be empty")
        if any(not 0.0 < q < 1.0 for q in self.quantiles):
            raise ValueError("every quantile must be in (0, 1)")


def walk_forward_folds(
    start: datetime, end: datetime, config: WalkForwardConfig | None = None
) -> list[Fold]:
    """
    Expanding-window walk-forward folds over `[start, end]` (design §4):
    fold `i`'s train window is always `[start, t_i)` (never a rolling
    window -- `train_start` is `start` for every fold), test window is
    `[t_i, t_i + test_span)`, and `t_i` advances by `step` each fold,
    starting at `start + min_train_span`. A fold is only emitted if its full
    test window fits within `end` -- the first fold needing the minimum
    train span met and every fold's test window is skipped, not truncated,
    once there isn't a full `test_span` left (design §4's explicit
    instruction). Returns `[]` if `end - start < min_train_span + test_span`.

    **A random train/test split is invalid on this data and never appears
    here** -- every fold's `test_start` equals its `train_end` by
    construction, so no test fold can ever precede its own training data
    (see `test_no_test_fold_precedes_its_training_data`).
    """
    config = config or WalkForwardConfig()
    folds: list[Fold] = []
    t = start + config.min_train_span
    while t + config.test_span <= end:
        folds.append(
            Fold(train_start=start, train_end=t, test_start=t, test_end=t + config.test_span)
        )
        t += config.step
    return folds


def trailing_folds(folds: list[Fold], span: timedelta) -> list[Fold]:
    """
    Returns the suffix of `folds` (assumed chronologically ordered, as
    `walk_forward_folds` produces) whose `test_start` falls within `span`
    of the LAST fold's own `test_end` -- the "trailing `span`" subset used
    for a regime-recent headline bar (coordinator directive: "the headline
    bar must be regime-recent, not a 4.6-year average" -- FCR-D DK2's mean
    clearing price fell from ~€73 in 2021 to ~€5 in 2026, so a full-history
    average is dominated by a market that no longer exists). Anchored to
    the folds' own last `test_end`, never to "now" -- a pure function of
    the folds list itself (this module's "no hidden globals" convention),
    so the result stays correct even if `folds` was generated against a
    `present` that's since moved on. Returns `[]` for an empty input.
    """
    if not folds:
        return []
    anchor = folds[-1].test_end
    threshold = anchor - span
    return [fold for fold in folds if fold.test_start >= threshold]


# --- B1: seasonal naive -----------------------------------------------------


@dataclass(frozen=True)
class SeasonalNaiveBaseline:
    """
    B1 (design §3): point forecast is the target at `t - lag`; quantile `tau`'s
    forecast is that point plus `residual_quantiles[tau]`, the empirical
    `tau`-quantile of `(actual - naive_point)` over the training fold that
    fit this instance -- see `fit_seasonal_naive`. Never fit on the full
    series; a fresh instance is fit per fold.
    """

    lag: timedelta
    residual_quantiles: dict[float, float]

    def predict(self, t: datetime, series_map: dict[datetime, float]) -> dict[float, float] | None:
        """
        `None` if `t - lag` has no observation in `series_map` (nothing to
        anchor the naive point forecast on for this `t`) -- callers must
        skip `t` for scoring in that case, not substitute a default.
        """
        naive_point = series_map.get(t - self.lag)
        if naive_point is None:
            return None
        return {tau: naive_point + residual for tau, residual in self.residual_quantiles.items()}


def fit_seasonal_naive(
    series: list[tuple[datetime, float]],
    train_start: datetime,
    train_end: datetime,
    lag: timedelta,
    quantiles: tuple[float, ...] = QUANTILES,
) -> SeasonalNaiveBaseline:
    """
    Fits B1 on `[train_start, train_end)` of `series` only (design §4's leak
    discipline) -- `series` may (and, for realistic folds, will) contain
    points outside that window too, but only training-fold `t`s contribute
    a residual, and only training-fold-or-earlier values are ever read as
    `t - lag` (always chronologically before `t`, so this never reads a
    value from the future relative to the residual it's computing, let
    alone from the test fold). Raises `ValueError` if the training fold
    yields no residuals at all (e.g. `train_end - train_start <= lag` with
    no earlier history either).
    """
    series_map = dict(series)
    residuals: list[float] = []
    for t, actual in series:
        if not (train_start <= t < train_end):
            continue
        naive_point = series_map.get(t - lag)
        if naive_point is None:
            continue
        residuals.append(actual - naive_point)
    if not residuals:
        raise ValueError(
            f"no seasonal-naive residuals available on train window "
            f"[{train_start}, {train_end}) with lag={lag}"
        )
    residual_quantiles = {tau: _empirical_quantile(residuals, tau) for tau in quantiles}
    return SeasonalNaiveBaseline(lag=lag, residual_quantiles=residual_quantiles)


# --- B2: conditional climatology --------------------------------------------


def _climatology_key(t: datetime) -> tuple[int, int]:
    """`(hour, month)` in Europe/Copenhagen local time -- see module docstring."""
    local = t.astimezone(COPENHAGEN_TZ)
    return (local.hour, local.month)


@dataclass(frozen=True)
class ClimatologyBaseline:
    """
    B2 (design §3): quantile `tau`'s forecast for `t` is the training
    fold's empirical `tau`-quantile of the target values observed in `t`'s
    own `(hour-of-day, month)` bucket -- see `fit_conditional_climatology`.
    Falls back to `overall_quantiles` (the training fold's unconditional
    quantiles) for a bucket with no training-fold observations (module
    docstring's backoff note).
    """

    group_quantiles: dict[tuple[int, int], dict[float, float]]
    overall_quantiles: dict[float, float]

    def predict(self, t: datetime, series_map: dict[datetime, float]) -> dict[float, float]:
        # series_map is unused -- climatology has no dependence on recent
        # values at all (design §3) -- but kept in the signature so
        # run_walk_forward can call every baseline's .predict polymorphically.
        del series_map
        return self.group_quantiles.get(_climatology_key(t), self.overall_quantiles)


# B2-rolling's trailing window (coordinator directive, post first-results
# review -- module docstring's "Two variants" note). 180 days is a
# deliberate middle ground, not tuned against this data: short enough to
# track FCR-D DK2's multi-year price collapse rather than averaging across
# it, long enough to still cover every `(hour, month)` bucket at least once
# within a couple of lookback windows and to smooth out day-to-day noise.
B2_ROLLING_LOOKBACK = timedelta(days=180)


def fit_conditional_climatology(
    series: list[tuple[datetime, float]],
    train_start: datetime,
    train_end: datetime,
    quantiles: tuple[float, ...] = QUANTILES,
    lookback: timedelta | None = None,
) -> ClimatologyBaseline:
    """
    Fits B2 on `[effective_start, train_end)` of `series` only -- design
    §4's leak discipline, and the exact mistake the design calls out as
    "easiest to make invisibly" for this baseline (a climatology computed
    over the full series and then scored on a subset looks excellent and
    means nothing). `train_end` is never crossed regardless of `lookback`
    -- that boundary is what makes this leak-safe, and is identical between
    the two variants below.

    `lookback=None` (the default, "expanding"): `effective_start =
    train_start`, i.e. every training-fold observation from the start of
    the fold's own training window.

    `lookback=<timedelta>` ("rolling", see `fit_conditional_climatology_rolling`
    for the `B2_ROLLING_LOOKBACK`-bound convenience wrapper this module
    reports as B2's second variant): `effective_start = max(train_start,
    train_end - lookback)` -- bounded to the trailing `lookback` window
    ending at the fold boundary, falling back to the full training span
    for any fold whose own `[train_start, train_end)` is shorter than
    `lookback` (early folds, per the 90-day minimum initial train span --
    at that point rolling and expanding are identical for that fold, by
    construction, not by a special case).

    Every value that feeds `group_quantiles`/`overall_quantiles` below has
    `effective_start <= time < train_end`; nothing outside that window is
    ever read, for either variant. See
    `test_climatology_is_fit_on_the_training_fold_only_not_the_full_series`
    and `test_climatology_rolling_uses_only_the_trailing_lookback_window`
    for the regression tests this exists to satisfy.
    """
    effective_start = train_start if lookback is None else max(train_start, train_end - lookback)
    groups: dict[tuple[int, int], list[float]] = defaultdict(list)
    all_values: list[float] = []
    for t, value in series:
        if not (effective_start <= t < train_end):
            continue
        groups[_climatology_key(t)].append(value)
        all_values.append(value)
    if not all_values:
        raise ValueError(
            f"no climatology observations on train window [{effective_start}, {train_end})"
        )
    group_quantiles = {
        key: {tau: _empirical_quantile(values, tau) for tau in quantiles}
        for key, values in groups.items()
    }
    overall_quantiles = {tau: _empirical_quantile(all_values, tau) for tau in quantiles}
    return ClimatologyBaseline(group_quantiles=group_quantiles, overall_quantiles=overall_quantiles)


def fit_conditional_climatology_rolling(
    series: list[tuple[datetime, float]],
    train_start: datetime,
    train_end: datetime,
    quantiles: tuple[float, ...] = QUANTILES,
) -> ClimatologyBaseline:
    """
    B2-rolling: `fit_conditional_climatology` bound to the trailing
    `B2_ROLLING_LOOKBACK` (180 days) ending at `train_end`, rather than the
    fold's whole expanding training window -- see module docstring's "Two
    variants" note for why an expanding climatology is a strawman baseline
    against a market that has fallen ~93% in mean clearing price since
    2021. Thin wrapper only, same signature as `fit_seasonal_naive`'s
    partial-application pattern so both fit into `run_walk_forward`'s
    `fit_fn(series, train_start, train_end)` contract directly.
    """
    return fit_conditional_climatology(
        series, train_start, train_end, quantiles=quantiles, lookback=B2_ROLLING_LOOKBACK
    )


# --- walk-forward evaluation harness ----------------------------------------


@dataclass(frozen=True)
class WalkForwardResult:
    """
    `run_walk_forward`'s return value: mean pinball loss per quantile,
    pooled across every fold's test points, plus the exact window and fold
    count every number in `docs/forecast-baseline-results.md` must be
    reported alongside (design §4/§6 -- "the bar must be a reviewable
    artefact"). `per_fold_quantile_loss` is the same breakdown per fold, for
    anyone who wants to check the bar isn't being carried by one unusually
    easy fold.
    """

    fold_count: int
    window_start: datetime
    window_end: datetime
    per_quantile_loss: dict[float, float]
    per_fold_quantile_loss: list[dict[float, float]] = field(default_factory=list)


def run_walk_forward(
    series: list[tuple[datetime, float]],
    folds: list[Fold],
    fit_fn,
    quantiles: tuple[float, ...] = QUANTILES,
) -> WalkForwardResult:
    """
    Runs one baseline config's walk-forward evaluation (design §4): for
    each fold, `fit_fn(series, fold.train_start, fold.train_end)` fits a
    fresh baseline **on that fold's training window only** (never the full
    `series` -- see `fit_seasonal_naive`/`fit_conditional_climatology`), then
    every point of `series` whose time falls in `[fold.test_start,
    fold.test_end)` is scored via the fitted baseline's `.predict(t,
    series_map)`; a `None` prediction (B1's "no `t - lag` observation")
    skips that point rather than substituting a default.

    Raises `ValueError` if `folds` is empty -- a caller passing a window too
    short for even one fold (below `min_train_span + test_span`) must know
    that no result was computed, not silently receive an empty/NaN report.
    """
    if not folds:
        raise ValueError(
            "no walk-forward folds to evaluate (window shorter than "
            "min_train_span + test_span, or folds not yet generated)"
        )
    series_map = dict(series)
    pooled_losses: dict[float, list[float]] = defaultdict(list)
    per_fold: list[dict[float, float]] = []

    for fold in folds:
        baseline = fit_fn(series, fold.train_start, fold.train_end)
        fold_losses: dict[float, list[float]] = defaultdict(list)
        for t, actual in series:
            if not (fold.test_start <= t < fold.test_end):
                continue
            predicted = baseline.predict(t, series_map)
            if predicted is None:
                continue
            for tau in quantiles:
                loss = pinball_loss(actual, predicted[tau], tau)
                pooled_losses[tau].append(loss)
                fold_losses[tau].append(loss)
        per_fold.append(
            {
                tau: (statistics.mean(vals) if vals else float("nan"))
                for tau, vals in fold_losses.items()
            }
        )

    per_quantile_loss = {tau: statistics.mean(vals) for tau, vals in pooled_losses.items() if vals}
    return WalkForwardResult(
        fold_count=len(folds),
        window_start=folds[0].train_start,
        window_end=folds[-1].test_end,
        per_quantile_loss=per_quantile_loss,
        per_fold_quantile_loss=per_fold,
    )
