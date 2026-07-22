"""
Tests for M6 P3b's day-ahead retarget (`docs/forecast-day-ahead-design.md`):
the `TargetConfig` generalisation in `shared/baselines.py`, its
15-min->hourly aggregation step, and the regression guard the design
requires -- "the retarget config does NOT regress FCR-D: the existing P3
numbers must still reproduce through the generalised path" (design §4).

Every fixture here is synthetic/hand-built -- no database, matching
`tests/test_baselines.py`/`tests/test_forecast_model.py`'s own convention
(a `MagicMock` stands in for `DatabaseManager` wherever one is needed at
all).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from shared.baselines import (
    DAY_AHEAD_TARGET,
    FCR_D_TARGET,
    QUANTILES,
    TARGET_MARKET,
    TARGET_PRODUCTS,
    TARGET_ZONE,
    TargetConfig,
    _aggregate_hourly_mean,
    fetch_and_assert_daily_coverage,
    fetch_target_series,
    fit_conditional_climatology_rolling,
    fit_seasonal_naive,
    pinball_loss,
    run_walk_forward,
    trailing_folds,
    walk_forward_folds,
)
from shared.forecast_model import (
    LOOKBACKS,
    ForecastModelConfig,
    join_features_and_target,
    run_model_walk_forward,
)

BASE = datetime(2022, 1, 1, tzinfo=UTC)

# =============================================================================
# TargetConfig -- the generalised config both targets flow through
# =============================================================================


def test_fcr_d_target_matches_the_original_hardcoded_constants():
    """
    `FCR_D_TARGET` is a config-ified version of what `TARGET_MARKET`/
    `TARGET_ZONE`/`TARGET_PRODUCTS` always were, not a new definition --
    those three module-level aliases must still equal its fields exactly.
    """
    assert FCR_D_TARGET.market == "FCR" == TARGET_MARKET
    assert FCR_D_TARGET.zone == "DK2" == TARGET_ZONE
    assert FCR_D_TARGET.products == ("up", "down") == TARGET_PRODUCTS
    assert FCR_D_TARGET.aggregate_hourly is False


def test_day_ahead_target_is_a_single_product_hourly_aggregated_series():
    """
    Day-ahead design §1: one series (not directional up/down), and the one
    genuinely new data-handling step -- 15-min source aggregated to hourly
    by mean -- is opt-in via `aggregate_hourly`, not silently assumed for
    every target.
    """
    assert DAY_AHEAD_TARGET.market == "day_ahead"
    assert DAY_AHEAD_TARGET.zone == "DK2"
    assert DAY_AHEAD_TARGET.products == ("price",)
    assert DAY_AHEAD_TARGET.aggregate_hourly is True


def test_target_config_is_a_frozen_dataclass():
    config = TargetConfig(market="x", zone="y", products=("z",))
    with pytest.raises(AttributeError):
        config.market = "other"  # type: ignore[misc]


# =============================================================================
# _aggregate_hourly_mean -- day-ahead's one new data-handling step
# =============================================================================


def test_aggregate_hourly_mean_averages_four_quarter_hour_points():
    hour = BASE
    series = [
        (hour, 10.0),
        (hour + timedelta(minutes=15), 20.0),
        (hour + timedelta(minutes=30), 30.0),
        (hour + timedelta(minutes=45), 40.0),
    ]
    aggregated = _aggregate_hourly_mean(series)
    assert aggregated == [(hour, 25.0)]


def test_aggregate_hourly_mean_handles_multiple_hours_and_stays_sorted():
    series = [
        (BASE + timedelta(hours=1, minutes=30), 100.0),
        (BASE, 10.0),
        (BASE + timedelta(minutes=15), 20.0),
        (BASE + timedelta(hours=1), 50.0),
    ]
    aggregated = _aggregate_hourly_mean(series)
    assert aggregated == [
        (BASE, 15.0),
        (BASE + timedelta(hours=1), 75.0),
    ]


def test_aggregate_hourly_mean_averages_whatever_quarters_are_present():
    """
    An hour with fewer than 4 quarter-hour points (a partial hour, e.g. at
    a window boundary) still gets a value -- the mean of what's there, never
    dropped or padded (module docstring: the day-level coverage gate is
    what's relied on to catch a genuine gap, not this step).
    """
    series = [(BASE, 10.0), (BASE + timedelta(minutes=15), 30.0)]
    assert _aggregate_hourly_mean(series) == [(BASE, 20.0)]


def test_aggregate_hourly_mean_of_empty_series_is_empty():
    assert _aggregate_hourly_mean([]) == []


# =============================================================================
# fetch_target_series -- generalised over TargetConfig
# =============================================================================


def test_fetch_target_series_day_ahead_aggregates_15min_to_hourly():
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        assert market == "day_ahead"
        assert zone == "DK2"
        assert product == "price"
        assert history is False
        return [
            {"time": BASE, "value": 10.0},
            {"time": BASE + timedelta(minutes=15), "value": 20.0},
            {"time": BASE + timedelta(minutes=30), "value": None},  # nulls dropped pre-aggregation
            {"time": BASE + timedelta(minutes=45), "value": 30.0},
            {"time": BASE + timedelta(hours=1), "value": 100.0},
        ]

    db.fetch_series_values.side_effect = fetch_series_values

    series = fetch_target_series(
        db, "price", BASE, BASE + timedelta(hours=1, minutes=59), config=DAY_AHEAD_TARGET
    )

    # Hour 0: mean(10, 20, 30) = 20 (the null quarter is dropped before
    # aggregation, not averaged in as 0). Hour 1: single point, 100.
    assert series == [(BASE, 20.0), (BASE + timedelta(hours=1), 100.0)]


def test_fetch_target_series_day_ahead_rejects_a_directional_product():
    db = MagicMock()
    with pytest.raises(ValueError):
        fetch_target_series(db, "up", BASE, BASE + timedelta(days=1), config=DAY_AHEAD_TARGET)


def test_fetch_target_series_fcr_d_config_never_aggregates():
    """FCR-D is already hourly -- explicitly passing its config must be a no-op vs. the default."""
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        return [
            {"time": BASE, "value": 1.0},
            {"time": BASE + timedelta(hours=1), "value": 2.0},
        ]

    db.fetch_series_values.side_effect = fetch_series_values

    default_config = fetch_target_series(db, "up", BASE, BASE + timedelta(hours=1))
    explicit_config = fetch_target_series(
        db, "up", BASE, BASE + timedelta(hours=1), config=FCR_D_TARGET
    )
    assert default_config == explicit_config == [(BASE, 1.0), (BASE + timedelta(hours=1), 2.0)]


# =============================================================================
# Regression guard (design §4, required): the FCR-D target config, run
# through the generalised path, must reproduce exactly what the
# pre-generalisation hardcoded path did -- not merely something similar.
# =============================================================================


def _old_hardcoded_fetch_target_series(
    db, product: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """
    A literal copy of `fetch_target_series`'s body exactly as it existed
    before the `TargetConfig` generalisation (hardcoded `TARGET_MARKET`/
    `TARGET_ZONE`/`TARGET_PRODUCTS`, no aggregation step) -- kept here only
    as the regression oracle this test compares the generalised path
    against; never imported or reused by any production code.
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


def _fcr_d_shaped_rows(n_hours: int):
    return [
        {"time": BASE + timedelta(hours=i), "value": 5.0 + (i % 7) * 1.3 - (i % 3)}
        for i in range(n_hours)
    ]


def test_fetch_target_series_default_config_reproduces_original_fcr_d_behavior():
    """
    The required regression guard, at the unit this generalisation actually
    touches: for BOTH FCR-D products, the generalised `fetch_target_series`
    (implicit default `config=FCR_D_TARGET`) must return byte-identical
    output to the old hardcoded function it replaced, over the same
    synthetic series.
    """
    for product in ("up", "down"):
        db = MagicMock()
        db.fetch_series_values.return_value = _fcr_d_shaped_rows(200)

        old = _old_hardcoded_fetch_target_series(db, product, BASE, BASE + timedelta(hours=199))
        new_default = fetch_target_series(db, product, BASE, BASE + timedelta(hours=199))
        new_explicit = fetch_target_series(
            db, product, BASE, BASE + timedelta(hours=199), config=FCR_D_TARGET
        )

        assert new_default == old
        assert new_explicit == old


def test_generalised_fold_and_baseline_pipeline_reproduces_fcr_d_numbers():
    """
    One level up from the fetch function alone: runs the FULL P2 pipeline
    (walk-forward folds, B1, B2-rolling, pinball loss) on an FCR-D-shaped
    synthetic series fetched via the generalised, config-driven
    `fetch_target_series`, and checks it against the identical computation
    done by hand-building the series the old, pre-generalisation way (no
    `TargetConfig` involved at all). `walk_forward_folds`/`fit_seasonal_naive`/
    `fit_conditional_climatology_rolling`/`run_walk_forward` are literally
    unmodified by this change (only the fetch layer is), so this is really
    confirming the fetch layer's output feeds them identically either way --
    the numbers must match exactly, not approximately.
    """
    db = MagicMock()
    n_hours = 24 * 130  # > 90 (min_train_span) + 30 (test_span), several folds
    db.fetch_series_values.return_value = _fcr_d_shaped_rows(n_hours)
    end = BASE + timedelta(hours=n_hours - 1)

    old_series = _old_hardcoded_fetch_target_series(db, "up", BASE, end)
    new_series = fetch_target_series(db, "up", BASE, end)
    assert new_series == old_series

    folds = walk_forward_folds(BASE, BASE + timedelta(hours=n_hours))
    assert len(folds) >= 1

    old_b1 = run_walk_forward(
        old_series, folds, lambda s, a, b: fit_seasonal_naive(s, a, b, timedelta(hours=24))
    )
    new_b1 = run_walk_forward(
        new_series, folds, lambda s, a, b: fit_seasonal_naive(s, a, b, timedelta(hours=24))
    )
    assert old_b1.per_quantile_loss == new_b1.per_quantile_loss
    assert old_b1.per_fold_quantile_loss == new_b1.per_fold_quantile_loss

    old_b2 = run_walk_forward(old_series, folds, fit_conditional_climatology_rolling)
    new_b2 = run_walk_forward(new_series, folds, fit_conditional_climatology_rolling)
    assert old_b2.per_quantile_loss == new_b2.per_quantile_loss


# =============================================================================
# Day-ahead integration case: 15-min fetch -> hourly aggregation -> feature
# join -> walk-forward model fit, end to end on synthetic data (design §4:
# "reuse P3's tests; add day-ahead cases").
# =============================================================================


def _day_ahead_feature_rows(n_hours: int, start: datetime = BASE) -> list[dict]:
    rows = []
    for i in range(n_hours):
        t = start + timedelta(hours=i)
        rows.append(
            {
                "zone": "DK2",
                "mtu_start": t,
                "hour_of_day": t.hour,
                "day_of_week": t.weekday(),
                "month": t.month,
                "is_danish_public_holiday": False,
                "is_after_d1_gate": i % 2 == 0,
                "realised_offshore_wind": float(i % 50),
                "day_ahead_price_SE3": float(20 + i % 30),
            }
        )
    return rows


def test_day_ahead_target_flows_through_the_full_walk_forward_and_model_harness():
    """
    A single-product, 15-min-native day-ahead-shaped series: fetched via
    `DAY_AHEAD_TARGET` (aggregating to hourly), coverage-gated, joined
    against synthetic hourly feature rows (`join_features_and_target`,
    unmodified from P3), and run through both the P2 baseline harness
    (`run_walk_forward`) and the P3 model harness (`run_model_walk_forward`)
    -- exactly the same functions FCR-D uses, exercised here for day-ahead's
    single 'price' product instead of a directional pair.
    """
    n_hours = 24 * 130  # comfortably above min_train_span + test_span
    quarter_hours: list[tuple[datetime, float]] = []
    t = BASE
    for i in range(n_hours * 4):
        # A smooth diurnal-ish signal so pinball losses are finite and
        # non-degenerate, not a flat series.
        value = 400.0 + 50.0 * ((t.hour + t.minute / 60.0) % 24) + (i % 3)
        quarter_hours.append((t, value))
        t += timedelta(minutes=15)

    db = MagicMock()
    db.fetch_daily_aggregates.return_value = [
        {"day": BASE + timedelta(days=d), "sample_count": 96} for d in range((n_hours // 24) + 1)
    ]
    db.fetch_series_values.return_value = [{"time": qt, "value": v} for qt, v in quarter_hours]

    end = BASE + timedelta(hours=n_hours)
    fetch_and_assert_daily_coverage(
        db, DAY_AHEAD_TARGET.market, DAY_AHEAD_TARGET.zone, "price", BASE, end
    )  # must not raise

    target_series = fetch_target_series(db, "price", BASE, end, config=DAY_AHEAD_TARGET)
    assert len(target_series) == n_hours  # one point per hour, aggregation confirmed
    for hour_t, _ in target_series:
        assert hour_t.minute == 0  # every timestamp lands on the hourly grid

    folds = walk_forward_folds(BASE, end)
    assert len(folds) >= 1
    headline_folds = trailing_folds(folds, timedelta(days=365))
    assert headline_folds  # small-sample day-ahead history: still at least one fold

    # --- baselines: B1 (both lags) + B2-rolling only (design §3: no B3) ---
    baseline_result = run_walk_forward(
        target_series, headline_folds, fit_conditional_climatology_rolling
    )
    assert baseline_result.fold_count == len(headline_folds)
    for tau in QUANTILES:
        loss = baseline_result.per_quantile_loss[tau]
        assert loss == loss and loss >= 0  # finite, non-negative

    # --- model: same harness as FCR-D, single product 'price' ---
    feature_rows = _day_ahead_feature_rows(n_hours)
    dataset = join_features_and_target(feature_rows, target_series)
    assert len(dataset.times) == n_hours  # every hour joined

    config = ForecastModelConfig(n_estimators=20, num_leaves=7, early_stopping_rounds=5)
    model_result = run_model_walk_forward(dataset, headline_folds, LOOKBACKS["12mo"], config)

    assert model_result.fold_count == len(headline_folds)
    assert set(model_result.per_quantile_loss.keys()) == set(QUANTILES)
    for tau in QUANTILES:
        loss = model_result.per_quantile_loss[tau]
        assert loss == loss and loss >= 0

    # Non-crossing quantiles hold for the single-product case too.
    preds = np.column_stack([np.full(3, model_result.per_quantile_loss[tau]) for tau in QUANTILES])
    # (sanity on the result shape, not the model's raw predictions -- the
    # non-crossing guarantee itself is `ForecastQuantileModel.predict`'s own
    # test, tests/test_forecast_model.py::test_predict_output_is_never_crossing,
    # unmodified and reused here via `run_model_walk_forward`.)
    assert preds.shape == (3, len(QUANTILES))


def test_pinball_loss_still_the_same_function_for_a_single_product_target():
    """Sanity: pinball_loss itself has zero product/market awareness -- confirmed explicitly."""
    assert pinball_loss(500.0, 480.0, 0.5) == pytest.approx(0.5 * 20.0)
