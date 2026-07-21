"""
Tests for `shared/baselines.py`, written in the order the design
(`docs/forecast-baseline-design.md`) mandates: the §5 coverage gate first
(fed a gapped fixture, asserted to raise -- this is the required
deliverable, not a nicety), then the §4 walk-forward fold generator (no
random split, no test-precedes-train), then per-fold fitting for both
baselines (§4's leak discipline, explicitly for B2 -- "the mistake is
easiest to make invisibly"), then the pinball-loss/empirical-quantile
building blocks and the `run_walk_forward` harness that ties it together.

Every fixture here is synthetic/hand-built -- never the real database (the
design's "unit tests must not require the database" constraint), matching
`tests/test_feature_store.py`'s `_make_fake_db` convention where a fake DB
is needed at all.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shared.baselines import (
    B2_ROLLING_LOOKBACK,
    QUANTILES,
    ClimatologyBaseline,
    CoverageGapError,
    Fold,
    SeasonalNaiveBaseline,
    WalkForwardConfig,
    _climatology_key,
    _empirical_quantile,
    _missing_day_ranges,
    assert_full_daily_coverage,
    fetch_and_assert_daily_coverage,
    fetch_target_series,
    fit_conditional_climatology,
    fit_conditional_climatology_rolling,
    fit_seasonal_naive,
    pinball_loss,
    run_walk_forward,
    trailing_folds,
    walk_forward_folds,
)

BASE = datetime(2022, 1, 1, tzinfo=UTC)


# =============================================================================
# §5: the coverage gate -- built and tested FIRST, per the design's build order
# =============================================================================


def test_coverage_gate_raises_on_a_gapped_series():
    """The required deliverable test: feed it a gapped series, assert it raises."""
    present = {date(2022, 1, 1), date(2022, 1, 2), date(2022, 1, 5)}  # 3rd/4th missing

    with pytest.raises(CoverageGapError) as exc_info:
        assert_full_daily_coverage(present, date(2022, 1, 1), date(2022, 1, 5))

    message = str(exc_info.value)
    assert "2022-01-03..2022-01-04" in message


def test_coverage_gate_passes_on_a_complete_series():
    present = {date(2022, 1, 1) + timedelta(days=i) for i in range(5)}
    assert_full_daily_coverage(present, date(2022, 1, 1), date(2022, 1, 5))  # must not raise


def test_coverage_gate_reports_multiple_disjoint_gaps():
    present = {date(2022, 1, 1), date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 6)}
    with pytest.raises(CoverageGapError) as exc_info:
        assert_full_daily_coverage(present, date(2022, 1, 1), date(2022, 1, 6))
    message = str(exc_info.value)
    assert "2022-01-02..2022-01-02" in message
    assert "2022-01-05..2022-01-05" in message
    assert "2 missing day range" in message


def test_coverage_gate_raises_on_a_gap_at_the_very_start():
    """
    The real case this gate caught against the live database (build report):
    FCR-D DK2 `down` has no data at all for its first 30 days -- verified
    the design doc's own "0 missing days" claim is true for `up` but false
    for `down`.
    """
    present = {date(2022, 2, 1) + timedelta(days=i) for i in range(10)}  # Jan entirely missing
    with pytest.raises(CoverageGapError) as exc_info:
        assert_full_daily_coverage(present, date(2022, 1, 1), date(2022, 2, 10))
    assert "2022-01-01..2022-01-31" in str(exc_info.value)


def test_coverage_gate_raises_on_a_gap_at_the_very_end():
    present = {date(2022, 1, 1) + timedelta(days=i) for i in range(3)}
    with pytest.raises(CoverageGapError):
        assert_full_daily_coverage(present, date(2022, 1, 1), date(2022, 1, 10))


def test_missing_day_ranges_returns_empty_list_for_full_coverage():
    present = {date(2022, 1, 1), date(2022, 1, 2)}
    assert _missing_day_ranges(present, date(2022, 1, 1), date(2022, 1, 2)) == []


def test_fetch_and_assert_daily_coverage_raises_using_a_fake_db():
    """
    Exercises the DB-backed wrapper end to end against a fake
    `DatabaseManager.fetch_daily_aggregates` -- confirms the wrapper turns a
    real per-day query result into the same raising behaviour, without a
    database (matching `tests/test_feature_store.py`'s `_make_fake_db`
    style: a MagicMock stands in for the DB).
    """
    db = MagicMock()
    # Day 2 (2022-01-02) has zero rows -- entirely absent from the query result,
    # exactly like a real `fetch_daily_aggregates` gap.
    db.fetch_daily_aggregates.return_value = [
        {"day": datetime(2022, 1, 1, tzinfo=UTC), "sample_count": 24},
        {"day": datetime(2022, 1, 3, tzinfo=UTC), "sample_count": 24},
    ]

    with pytest.raises(CoverageGapError) as exc_info:
        fetch_and_assert_daily_coverage(
            db,
            "FCR",
            "DK2",
            "up",
            datetime(2022, 1, 1, tzinfo=UTC),
            datetime(2022, 1, 3, tzinfo=UTC),
        )
    assert "2022-01-02..2022-01-02" in str(exc_info.value)


def test_fetch_and_assert_daily_coverage_ignores_a_zero_sample_count_day():
    """A day present in the result set with sample_count=0 must still count as missing."""
    db = MagicMock()
    db.fetch_daily_aggregates.return_value = [
        {"day": datetime(2022, 1, 1, tzinfo=UTC), "sample_count": 24},
        {"day": datetime(2022, 1, 2, tzinfo=UTC), "sample_count": 0},
    ]
    with pytest.raises(CoverageGapError):
        fetch_and_assert_daily_coverage(
            db,
            "FCR",
            "DK2",
            "up",
            datetime(2022, 1, 1, tzinfo=UTC),
            datetime(2022, 1, 2, tzinfo=UTC),
        )


def test_fetch_and_assert_daily_coverage_passes_when_complete():
    db = MagicMock()
    db.fetch_daily_aggregates.return_value = [
        {"day": datetime(2022, 1, 1, tzinfo=UTC), "sample_count": 24},
        {"day": datetime(2022, 1, 2, tzinfo=UTC), "sample_count": 24},
    ]
    fetch_and_assert_daily_coverage(
        db, "FCR", "DK2", "up", datetime(2022, 1, 1, tzinfo=UTC), datetime(2022, 1, 2, tzinfo=UTC)
    )  # must not raise


# =============================================================================
# §4: walk-forward folds -- expanding window, no random split
# =============================================================================


def test_folds_respect_the_minimum_90_day_initial_train_span():
    start = BASE
    end = BASE + timedelta(days=95)  # < 90 + 30, so no fold fits
    assert walk_forward_folds(start, end) == []

    end = BASE + timedelta(days=120)  # exactly 90 + 30
    folds = walk_forward_folds(start, end)
    assert len(folds) == 1
    assert folds[0].train_start == start
    assert folds[0].train_end == start + timedelta(days=90)
    assert folds[0].test_start == start + timedelta(days=90)
    assert folds[0].test_end == start + timedelta(days=120)


def test_folds_are_skipped_not_truncated_when_window_too_short_for_a_next_fold():
    start = BASE
    end = BASE + timedelta(days=140)  # one full fold (90+30), then 20 days left over
    folds = walk_forward_folds(start, end)
    assert len(folds) == 1  # the leftover 20 days must NOT produce a truncated second fold


def test_folds_expand_train_window_and_step_by_30_days():
    start = BASE
    end = BASE + timedelta(days=200)
    folds = walk_forward_folds(start, end)
    assert len(folds) == 3  # t = 90, 120, 150 -> test ends at 120, 150, 180 (all <= 200)

    for fold in folds:
        assert fold.train_start == start  # expanding window: train_start never moves
    assert [f.train_end for f in folds] == [
        start + timedelta(days=90),
        start + timedelta(days=120),
        start + timedelta(days=150),
    ]
    assert [f.test_end - f.test_start for f in folds] == [timedelta(days=30)] * 3


def test_no_test_fold_precedes_its_training_data():
    """
    The design's explicit invariant: a random train/test split is invalid
    on this data and must never appear. Every fold's test window must start
    at or after its own training window ends -- checked here across a
    multi-year synthetic range, not just the first fold.
    """
    start = BASE
    end = BASE + timedelta(days=1200)
    folds = walk_forward_folds(start, end)
    assert len(folds) > 5  # sanity: enough folds to make this check meaningful

    for fold in folds:
        assert fold.train_start < fold.train_end
        assert fold.test_start >= fold.train_end
        assert fold.test_start < fold.test_end
        # No point in the test window can be <= any point already used for
        # training -- the strongest form of the invariant.
        assert fold.test_start >= fold.train_end


def test_walk_forward_config_rejects_invalid_knobs():
    with pytest.raises(ValueError):
        WalkForwardConfig(min_train_span=timedelta(0))
    with pytest.raises(ValueError):
        WalkForwardConfig(test_span=timedelta(days=-1))
    with pytest.raises(ValueError):
        WalkForwardConfig(quantiles=())
    with pytest.raises(ValueError):
        WalkForwardConfig(quantiles=(0.0, 0.5))
    with pytest.raises(ValueError):
        WalkForwardConfig(quantiles=(0.5, 1.0))


# --- trailing_folds: the regime-recent headline-bar subset -------------------


def test_trailing_folds_returns_empty_list_for_empty_input():
    assert trailing_folds([], timedelta(days=365)) == []


def test_trailing_folds_selects_only_the_recent_suffix():
    start = BASE
    end = BASE + timedelta(days=800)
    folds = walk_forward_folds(start, end)
    assert len(folds) > 10  # sanity: enough folds to make "a suffix" meaningful

    recent = trailing_folds(folds, timedelta(days=365))

    assert 0 < len(recent) < len(folds)  # a strict, non-trivial suffix
    assert recent == folds[-len(recent) :]  # exactly the tail, nothing from the middle
    anchor = folds[-1].test_end
    for fold in recent:
        assert fold.test_start >= anchor - timedelta(days=365)
    # The fold immediately before the selected suffix (if any) must fail the cutoff --
    # otherwise the boundary itself is wrong, not just "some correct subset".
    excluded = folds[: len(folds) - len(recent)]
    if excluded:
        assert excluded[-1].test_start < anchor - timedelta(days=365)


def test_trailing_folds_returns_everything_when_span_exceeds_the_whole_history():
    folds = walk_forward_folds(BASE, BASE + timedelta(days=200))
    assert trailing_folds(folds, timedelta(days=10_000)) == folds


def test_trailing_folds_selects_exactly_the_last_fold_when_span_equals_one_test_span():
    config = WalkForwardConfig()
    folds = walk_forward_folds(BASE, BASE + timedelta(days=200), config)
    recent = trailing_folds(folds, config.test_span)
    assert recent == [folds[-1]]


# =============================================================================
# §4 leak discipline: every baseline is fit on its training fold only
# =============================================================================


def _hourly_series(start: datetime, hours: int, value_fn) -> list[tuple[datetime, float]]:
    return [
        (start + timedelta(hours=i), value_fn(start + timedelta(hours=i))) for i in range(hours)
    ]


def test_seasonal_naive_residuals_fitted_only_on_training_fold():
    """
    A cumulative-level series whose day-over-day increment is a small,
    constant 1.0 for the 10 train-fold days and a huge 100000.0 for 5
    later, test-fold-only days (so `t - 24h` is exactly "yesterday's
    level", and the residual `actual - naive` equals that day's increment
    for every hour). Fitting correctly on the training fold alone must
    reproduce the small increment; fitting (incorrectly, as if this
    baseline were fit once on the whole series rather than per fold) with
    `train_end` pushed out to cover the test-fold-only days too must not.
    """
    lag = timedelta(hours=24)
    seed_day = BASE  # day_index 0 -- one lag's worth of history seeded before train_start
    train_start = BASE + timedelta(days=1)
    train_end = BASE + timedelta(days=11)  # 10 train days: day_index 1..10
    leaked_train_end = BASE + timedelta(days=16)  # +5 more days: day_index 11..15

    increments = [0.0] + [1.0] * 10 + [100_000.0] * 5  # index 0 unused (day 0 is the seed level)
    daily_level = [100.0]
    for inc in increments[1:]:
        daily_level.append(daily_level[-1] + inc)

    series: list[tuple[datetime, float]] = []
    for day_index, level in enumerate(daily_level):
        day_start = seed_day + timedelta(days=day_index)
        for hour in range(24):
            series.append((day_start + timedelta(hours=hour), level))

    correctly_fit = fit_seasonal_naive(series, train_start, train_end, lag)
    # The bug this test guards against: fitting with train_end pushed out to
    # also cover the days that should only ever appear in a TEST fold.
    leaked_fit = fit_seasonal_naive(series, train_start, leaked_train_end, lag)

    for tau in QUANTILES:
        assert correctly_fit.residual_quantiles[tau] == pytest.approx(1.0, abs=1e-9)
    # The leaked fit's upper quantile is dragged sharply higher by the
    # test-fold-only 100000.0 increments -- proof the correct fit isn't
    # simply "always 1.0" by construction, but because it excluded them.
    assert leaked_fit.residual_quantiles[0.9] > 1000.0


def test_seasonal_naive_predict_returns_none_without_a_lag_observation():
    baseline = SeasonalNaiveBaseline(lag=timedelta(hours=24), residual_quantiles={0.5: 0.0})
    assert baseline.predict(BASE, {}) is None


def test_seasonal_naive_predict_is_point_plus_residual_quantiles():
    baseline = SeasonalNaiveBaseline(
        lag=timedelta(hours=24), residual_quantiles={0.1: -2.0, 0.5: 0.0, 0.9: 3.0}
    )
    t = BASE + timedelta(hours=24)
    series_map = {BASE: 50.0}
    preds = baseline.predict(t, series_map)
    assert preds == {0.1: 48.0, 0.5: 50.0, 0.9: 53.0}


def test_climatology_is_fit_on_the_training_fold_only_not_the_full_series():
    """
    §4's named hazard, made concrete: the SAME (hour, month) bucket recurs
    in both a training-fold January and a later, test-fold-only January.
    Training-fold observations for that bucket cluster near 10; test-fold
    observations for the identical bucket are wildly different (near 1000).
    Fitting correctly on the training fold alone must reproduce the ~10
    level; fitting (incorrectly) on the full series must not.
    """
    train_start = datetime(2022, 1, 1, tzinfo=UTC)
    train_end = datetime(2023, 1, 1, tzinfo=UTC)  # one full year -> every (hour, month) covered
    full_end = datetime(2024, 1, 1, tzinfo=UTC)  # a second year, test-fold-only

    series: list[tuple[datetime, float]] = []
    t = train_start
    while t < full_end:
        if t < train_end:
            series.append((t, 10.0 + (t.hour % 3)))  # tight cluster around 10-12
        else:
            series.append((t, 1000.0 + (t.hour % 3)))  # test-fold-only: wildly different level
        t += timedelta(hours=1)

    correctly_fit = fit_conditional_climatology(series, train_start, train_end)
    # The bug this test guards against: fitting on the FULL series (train_end
    # pushed out to cover the test-fold-only year too) would blend in the ~1000
    # values for every bucket.
    leaked_fit = fit_conditional_climatology(series, train_start, full_end)

    key = _climatology_key(datetime(2022, 6, 15, 12, 0, tzinfo=UTC))  # (hour=..., month=6)
    for tau in QUANTILES:
        assert correctly_fit.group_quantiles[key][tau] < 100.0  # must stay near the ~10-12 cluster
    # The leaked fit's UPPER quantile for the identical bucket is dragged
    # sharply higher by the second year's test-fold-only observations --
    # demonstrating the correctly-fit baseline is NOT simply "always < 100"
    # by construction, but specifically because it excluded the leak (the
    # leaked fit's low quantiles stay in the first year's cluster purely
    # because that cluster sorts first -- it's the upper tail that gives the
    # leak away, same shape as any 50/50 two-cluster contamination would).
    assert leaked_fit.group_quantiles[key][0.9] > 500.0


# --- B2-rolling: trailing-window climatology (coordinator directive, post
# first-results review -- FCR-D DK2's multi-year price collapse makes an
# EXPANDING climatology a strawman baseline) --------------------------------


def test_climatology_rolling_uses_only_the_trailing_lookback_window():
    """
    A long (400-day) training fold whose oldest ~220 days sit in an "old
    regime" cluster (~70) and whose most recent 180 days (the lookback
    window, starting 2022-08-09) sit in a "new regime" cluster (~5) --
    FCR-D DK2's real shape, scaled down to a fast-running fixture. April
    (entirely in the old-regime period, and entirely OUTSIDE the trailing
    lookback window) is the probe month: the rolling variant has never seen
    an April observation at all for this fold, so it must fall back to its
    own (new-regime, ~5) unconditional quantiles via `.predict`; the
    expanding variant, fit on the identical series, has plenty of April
    observations, all from the old regime, and must not.
    """
    train_start = datetime(2022, 1, 1, tzinfo=UTC)
    train_end = train_start + timedelta(days=400)
    lookback = timedelta(days=180)
    regime_change = train_end - lookback  # 2022-08-09

    series: list[tuple[datetime, float]] = []
    t = train_start
    while t < train_end:
        level = 70.0 if t < regime_change else 5.0
        series.append((t, level + (t.hour % 3) * 0.01))
        t += timedelta(hours=1)

    rolling = fit_conditional_climatology(series, train_start, train_end, lookback=lookback)
    expanding = fit_conditional_climatology(series, train_start, train_end)

    probe = datetime(2022, 4, 15, 12, 0, tzinfo=UTC)  # April -- entirely pre-regime-change
    assert _climatology_key(probe) not in rolling.group_quantiles  # never observed within lookback
    assert _climatology_key(probe) in expanding.group_quantiles  # observed, all old-regime

    rolling_prediction = rolling.predict(probe, {})
    expanding_prediction = expanding.predict(probe, {})
    for tau in QUANTILES:
        assert rolling_prediction[tau] < 10.0  # falls back to the new-regime overall level
        assert expanding_prediction[tau] > 40.0  # the old-regime group level, unchanged


def test_climatology_rolling_convenience_wrapper_matches_explicit_lookback():
    train_start = datetime(2022, 1, 1, tzinfo=UTC)
    train_end = train_start + timedelta(days=400)
    series = _hourly_series(train_start, hours=400 * 24, value_fn=lambda t: 5.0 + (t.hour % 3))

    via_wrapper = fit_conditional_climatology_rolling(series, train_start, train_end)
    via_explicit_lookback = fit_conditional_climatology(
        series, train_start, train_end, lookback=B2_ROLLING_LOOKBACK
    )
    assert via_wrapper == via_explicit_lookback


def test_climatology_rolling_falls_back_to_the_full_training_span_when_shorter_than_lookback():
    """
    A fold whose own training span (50 days) is shorter than the 180-day
    lookback: `effective_start` clamps to `train_start`, so rolling and
    expanding must be identical for that fold -- not a special-cased
    branch, just `max(train_start, train_end - lookback) == train_start`.
    """
    train_start = datetime(2022, 1, 1, tzinfo=UTC)
    train_end = train_start + timedelta(days=50)
    series = _hourly_series(train_start, hours=50 * 24, value_fn=lambda t: 30.0 + (t.hour % 3))

    rolling = fit_conditional_climatology_rolling(series, train_start, train_end)
    expanding = fit_conditional_climatology(series, train_start, train_end)
    assert rolling == expanding


def test_climatology_rolling_never_reads_at_or_after_train_end():
    """
    Same leak-safety property as the expanding variant's own test above,
    checked explicitly for the rolling wrapper: pushing `train_end` out to
    cover what should be test-fold-only data contaminates the fit, proving
    the trailing-window logic doesn't accidentally bypass the `train_end`
    boundary it shares with the expanding variant.
    """
    train_start = datetime(2022, 1, 1, tzinfo=UTC)
    train_end = datetime(2022, 3, 1, tzinfo=UTC)  # 59 days -- shorter than the 180-day lookback
    leaked_train_end = datetime(2022, 3, 15, tzinfo=UTC)  # +14 test-fold-only days

    series: list[tuple[datetime, float]] = []
    t = train_start
    while t < leaked_train_end:
        level = 5.0 if t < train_end else 100_000.0
        series.append((t, level))
        t += timedelta(hours=1)

    correctly_fit = fit_conditional_climatology_rolling(series, train_start, train_end)
    leaked_fit = fit_conditional_climatology_rolling(series, train_start, leaked_train_end)

    for tau in QUANTILES:
        assert correctly_fit.overall_quantiles[tau] == pytest.approx(5.0, abs=1e-9)
    assert leaked_fit.overall_quantiles[0.9] > 1000.0


def test_climatology_falls_back_to_overall_quantiles_for_an_unseen_group():
    """
    A training fold shorter than a year cannot have observed every
    (hour, month) bucket -- exactly the shape of P2's own first few
    walk-forward folds (90-day minimum initial train span). The documented
    backoff: fall back to the training fold's unconditional quantiles.
    """
    train_start = datetime(2022, 1, 1, tzinfo=UTC)
    train_end = datetime(2022, 2, 1, tzinfo=UTC)  # January only -> July is unseen
    series = _hourly_series(train_start, hours=31 * 24, value_fn=lambda t: 42.0)

    baseline = fit_conditional_climatology(series, train_start, train_end)
    july_key = (12, 7)
    assert july_key not in baseline.group_quantiles

    predicted = baseline.predict(datetime(2022, 7, 15, 12, 0, tzinfo=UTC), {})
    assert predicted == baseline.overall_quantiles


def test_climatology_fit_raises_on_an_empty_training_window():
    with pytest.raises(ValueError):
        fit_conditional_climatology([], BASE, BASE + timedelta(days=1))


def test_seasonal_naive_fit_raises_when_no_residuals_are_available():
    series = [(BASE, 10.0)]  # a single point, no t - lag observation exists
    with pytest.raises(ValueError):
        fit_seasonal_naive(series, BASE, BASE + timedelta(hours=1), timedelta(hours=24))


# =============================================================================
# pinball loss / empirical quantile building blocks
# =============================================================================


def test_pinball_loss_is_zero_for_a_perfect_prediction():
    for tau in QUANTILES:
        assert pinball_loss(10.0, 10.0, tau) == 0.0


def test_pinball_loss_matches_hand_computed_values():
    # actual=10, predicted=8 -> under-prediction, penalised by tau
    assert pinball_loss(10.0, 8.0, 0.9) == pytest.approx(0.9 * 2.0)
    # actual=10, predicted=12 -> over-prediction, penalised by (1 - tau)
    assert pinball_loss(10.0, 12.0, 0.9) == pytest.approx(0.1 * 2.0)


def test_pinball_loss_at_tau_half_is_half_the_absolute_error():
    assert pinball_loss(10.0, 7.0, 0.5) == pytest.approx(1.5)
    assert pinball_loss(7.0, 10.0, 0.5) == pytest.approx(1.5)


def test_empirical_quantile_matches_known_values():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert _empirical_quantile(values, 0.0) == 10.0
    assert _empirical_quantile(values, 1.0) == 50.0
    assert _empirical_quantile(values, 0.5) == 30.0


def test_empirical_quantile_interpolates_between_points():
    values = [0.0, 10.0]
    assert _empirical_quantile(values, 0.5) == pytest.approx(5.0)
    assert _empirical_quantile(values, 0.25) == pytest.approx(2.5)


def test_empirical_quantile_of_single_value_is_that_value():
    assert _empirical_quantile([7.0], 0.1) == 7.0
    assert _empirical_quantile([7.0], 0.9) == 7.0


def test_empirical_quantile_raises_on_empty_input():
    with pytest.raises(ValueError):
        _empirical_quantile([], 0.5)


def test_quantile_predictions_are_monotonic_non_decreasing_in_tau():
    """
    A sanity property both baselines should satisfy by construction (the
    empirical quantile function is monotonic in tau): a wider quantile
    range should never predict LOWER for a higher tau.
    """
    series = _hourly_series(BASE, hours=24 * 20, value_fn=lambda t: 50.0 + (t.hour - 12))
    baseline = fit_seasonal_naive(
        series, BASE + timedelta(hours=24), BASE + timedelta(days=10), timedelta(hours=24)
    )
    predicted = baseline.predict(BASE + timedelta(days=15), dict(series))
    ordered = [predicted[tau] for tau in sorted(QUANTILES)]
    assert ordered == sorted(ordered)


# =============================================================================
# run_walk_forward harness
# =============================================================================


def test_run_walk_forward_raises_on_no_folds():
    with pytest.raises(ValueError):
        run_walk_forward([], [], lambda series, a, b: None)


def test_run_walk_forward_gives_zero_loss_for_a_perfect_predictor():
    class _PerfectBaseline:
        def predict(self, t, series_map):
            return {tau: series_map[t] for tau in QUANTILES}

    series = _hourly_series(BASE, hours=24 * 130, value_fn=lambda t: 50.0 + (t.hour - 12))
    folds = walk_forward_folds(BASE, BASE + timedelta(hours=24 * 130))
    assert folds  # sanity

    result = run_walk_forward(series, folds, lambda s, a, b: _PerfectBaseline())

    assert result.fold_count == len(folds)
    for tau in QUANTILES:
        assert result.per_quantile_loss[tau] == pytest.approx(0.0, abs=1e-9)


def test_run_walk_forward_reports_the_exact_fold_count_and_window():
    series = _hourly_series(BASE, hours=24 * 200, value_fn=lambda t: 10.0)
    folds = walk_forward_folds(BASE, BASE + timedelta(hours=24 * 200))

    result = run_walk_forward(
        series, folds, lambda s, a, b: fit_seasonal_naive(s, a, b, timedelta(hours=24))
    )

    assert result.fold_count == len(folds)
    assert result.window_start == folds[0].train_start
    assert result.window_end == folds[-1].test_end
    assert len(result.per_fold_quantile_loss) == len(folds)


def test_run_walk_forward_uses_a_fresh_per_fold_fit_not_a_global_one():
    """
    Integration-level version of the per-fold-fitting requirement: a spy
    fit_fn records every (train_start, train_end) it was called with, and
    every call's train_end must be strictly less than that fold's test_end
    -- i.e. the fitter is never handed the test window's own data.
    """
    calls: list[tuple[datetime, datetime]] = []

    def _spy_fit(series, train_start, train_end):
        calls.append((train_start, train_end))
        return fit_seasonal_naive(series, train_start, train_end, timedelta(hours=24))

    series = _hourly_series(BASE, hours=24 * 200, value_fn=lambda t: 10.0)
    folds = walk_forward_folds(BASE, BASE + timedelta(hours=24 * 200))

    run_walk_forward(series, folds, _spy_fit)

    assert len(calls) == len(folds)
    for (train_start, train_end), fold in zip(calls, folds, strict=True):
        assert train_start == fold.train_start
        assert train_end == fold.train_end
        assert train_end <= fold.test_start


# =============================================================================
# fetch_target_series
# =============================================================================


def test_fetch_target_series_rejects_unknown_product():
    db = MagicMock()
    with pytest.raises(ValueError):
        fetch_target_series(db, "price", BASE, BASE + timedelta(days=1))


def test_fetch_target_series_dedupes_via_history_false_and_sorts_ascending():
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        assert market == "FCR"
        assert zone == "DK2"
        assert product == "up"
        assert history is False  # must read the deduped market_data view, not raw history
        return [
            {"time": BASE + timedelta(hours=2), "value": 3.0},
            {"time": BASE, "value": 1.0},
            {"time": BASE + timedelta(hours=1), "value": None},  # nulls dropped
        ]

    db.fetch_series_values.side_effect = fetch_series_values

    series = fetch_target_series(db, "up", BASE, BASE + timedelta(hours=3))

    assert series == [(BASE, 1.0), (BASE + timedelta(hours=2), 3.0)]


def test_climatology_baseline_is_a_dataclass_of_the_expected_shape():
    baseline = ClimatologyBaseline(group_quantiles={}, overall_quantiles={0.5: 1.0})
    assert baseline.predict(BASE, {}) == {0.5: 1.0}


def test_fold_is_a_frozen_dataclass():
    fold = Fold(train_start=BASE, train_end=BASE, test_start=BASE, test_end=BASE)
    with pytest.raises(AttributeError):
        fold.train_start = BASE + timedelta(days=1)  # type: ignore[misc]
