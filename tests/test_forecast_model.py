"""
Tests for `shared/forecast_model.py` (M6 P3, `docs/forecast-model-design.md`).

Every fixture is synthetic/hand-built -- no database (the design's "unit tests
must not need the DB" constraint, matching `tests/test_baselines.py`'s own
convention). Real LightGBM training is used only where the test needs an
actual fitted model (kept small/fast); everywhere the point is to check WHAT
data reached the trainer rather than what it learned, a `lgb.Dataset`/`lgb.
train` spy is used instead -- exact and fast, no dependence on a real model's
behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

import shared.forecast_model as forecast_model
from shared.baselines import QUANTILES, Fold
from shared.forecast_model import (
    CATEGORICAL_FEATURES,
    MIN_TRAINING_ROWS,
    ForecastModelConfig,
    ForecastQuantileModel,
    effective_train_window,
    fit_quantile_model,
    join_features_and_target,
    run_model_walk_forward,
)

BASE = datetime(2024, 1, 1, tzinfo=UTC)


def _make_feature_rows(
    n_hours: int, start: datetime = BASE, extra: dict[str, list[float | None]] | None = None
) -> list[dict]:
    """
    `n_hours` synthetic `build_features`-shaped rows, one per hour from
    `start`, with the real schema's calendar-feature keys plus a couple of
    representative numeric columns. `extra` overrides/adds numeric columns by
    name -> per-row value list (same length as `n_hours`), for tests that need
    a distinguishable, controllable feature.
    """
    rows = []
    for i in range(n_hours):
        t = start + timedelta(hours=i)
        row = {
            "zone": "DK2",
            "mtu_start": t,
            "hour_of_day": t.hour,
            "day_of_week": t.weekday(),
            "month": t.month,
            "is_danish_public_holiday": False,
            "is_after_d1_gate": i % 2 == 0,
            "realised_offshore_wind": float(i % 50),
            "day_ahead_price_SE3": None if i % 7 == 0 else float(20 + i % 30),
        }
        if extra:
            for key, values in extra.items():
                row[key] = values[i]
        rows.append(row)
    return rows


def _make_target_series(
    n_hours: int, start: datetime = BASE, value_fn=lambda i: 10.0 + (i % 24)
) -> list[tuple[datetime, float]]:
    return [(start + timedelta(hours=i), value_fn(i)) for i in range(n_hours)]


# =============================================================================
# join_features_and_target (design §4)
# =============================================================================


def test_join_drops_feature_rows_with_no_matching_target():
    feature_rows = _make_feature_rows(10)
    target_series = _make_target_series(10)[:7]  # last 3 hours have no target

    dataset = join_features_and_target(feature_rows, target_series)

    assert len(dataset.times) == 7
    assert dataset.dropped_no_target == 3
    assert dataset.dropped_no_feature == 0


def test_join_drops_target_points_with_no_matching_feature_row():
    feature_rows = _make_feature_rows(7)
    target_series = _make_target_series(10)  # 3 extra target points, no features

    dataset = join_features_and_target(feature_rows, target_series)

    assert len(dataset.times) == 7
    assert dataset.dropped_no_target == 0
    assert dataset.dropped_no_feature == 3


def test_join_excludes_zone_and_mtu_start_from_feature_columns():
    feature_rows = _make_feature_rows(5)
    target_series = _make_target_series(5)

    dataset = join_features_and_target(feature_rows, target_series)

    assert "zone" not in dataset.columns
    assert "mtu_start" not in dataset.columns
    assert dataset.columns == tuple(sorted(dataset.columns))  # deterministic order


def test_join_converts_booleans_to_floats_and_none_to_nan():
    feature_rows = _make_feature_rows(8)  # includes an is_after_d1_gate True/False mix
    target_series = _make_target_series(8)

    dataset = join_features_and_target(feature_rows, target_series)

    gate_idx = dataset.columns.index("is_after_d1_gate")
    assert set(dataset.X[:, gate_idx].tolist()) <= {0.0, 1.0}
    price_idx = dataset.columns.index("day_ahead_price_SE3")
    # row 0 (i=0) has day_ahead_price_SE3 = None -> NaN
    assert np.isnan(dataset.X[0, price_idx])


def test_join_categorical_idx_resolves_declared_categorical_columns():
    feature_rows = _make_feature_rows(5)
    target_series = _make_target_series(5)

    dataset = join_features_and_target(feature_rows, target_series)

    resolved = {dataset.columns[i] for i in dataset.categorical_idx}
    assert resolved == set(CATEGORICAL_FEATURES)


def test_join_raises_on_a_declared_categorical_missing_from_the_schema():
    feature_rows = [
        {"zone": "DK2", "mtu_start": BASE, "hour_of_day": 0, "day_of_week": 0, "month": 1}
    ]
    target_series = [(BASE, 5.0)]

    with pytest.raises(ValueError, match="categorical"):
        join_features_and_target(feature_rows, target_series)


def test_join_on_empty_feature_rows_returns_empty_dataset():
    dataset = join_features_and_target([], _make_target_series(3))
    assert dataset.times == []
    assert dataset.dropped_no_feature == 3


# =============================================================================
# effective_train_window (design §3 -- bounded trailing lookback)
# =============================================================================


def test_effective_train_window_bounds_by_lookback():
    fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(days=400),
        test_start=BASE + timedelta(days=400),
        test_end=BASE + timedelta(days=430),
    )
    start, end = effective_train_window(fold, timedelta(days=365))
    assert end == fold.train_end
    assert start == fold.train_end - timedelta(days=365)
    assert start > fold.train_start  # actually bounded, not the full expanding span


def test_effective_train_window_falls_back_to_full_span_for_an_early_fold():
    fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(days=90),  # shorter than the lookback below
        test_start=BASE + timedelta(days=90),
        test_end=BASE + timedelta(days=120),
    )
    start, end = effective_train_window(fold, timedelta(days=365))
    assert start == fold.train_start
    assert end == fold.train_end


def test_effective_train_window_rejects_non_positive_lookback():
    fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(days=10),
        test_start=BASE + timedelta(days=10),
        test_end=BASE + timedelta(days=20),
    )
    with pytest.raises(ValueError):
        effective_train_window(fold, timedelta(0))


# =============================================================================
# fit_quantile_model -- lookback is respected, not silently expanded to full
# history (design §3/§6)
# =============================================================================


def test_fit_quantile_model_respects_the_lookback_not_the_full_history():
    """
    300 hourly rows total. A fold whose lookback covers only ~40 of them
    should NOT be able to fit (well below MIN_TRAINING_ROWS) even though the
    full dataset has plenty of rows -- if `fit_quantile_model` silently used
    the whole dataset regardless of the window, this would incorrectly
    succeed.
    """
    feature_rows = _make_feature_rows(300)
    target_series = _make_target_series(300)
    dataset = join_features_and_target(feature_rows, target_series)

    fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=300),
        test_start=BASE + timedelta(hours=300),
        test_end=BASE + timedelta(hours=330),
    )
    config = ForecastModelConfig(n_estimators=30, early_stopping_rounds=5)

    small_start, small_end = effective_train_window(fold, timedelta(hours=40))
    with pytest.raises(ValueError, match="training row"):
        fit_quantile_model(dataset, small_start, small_end, config)

    large_start, large_end = effective_train_window(fold, timedelta(hours=300))
    model = fit_quantile_model(dataset, large_start, large_end, config)
    assert set(model.boosters.keys()) == set(QUANTILES)


def test_fit_quantile_model_raises_below_min_training_rows():
    feature_rows = _make_feature_rows(50)
    target_series = _make_target_series(50)
    dataset = join_features_and_target(feature_rows, target_series)
    assert len(dataset.times) < MIN_TRAINING_ROWS

    with pytest.raises(ValueError, match="training row"):
        fit_quantile_model(dataset, BASE, BASE + timedelta(hours=50))


def test_fit_quantile_model_rejects_train_end_at_or_before_train_start():
    feature_rows = _make_feature_rows(300)
    target_series = _make_target_series(300)
    dataset = join_features_and_target(feature_rows, target_series)
    with pytest.raises(ValueError, match="train_end"):
        fit_quantile_model(dataset, BASE + timedelta(hours=10), BASE)


# =============================================================================
# Per-fold refit: only the fold's own bounded training window ever reaches
# the trainer (design §4/§6) -- a `lgb.Dataset`/`lgb.train` spy, not a real
# fit, so this is exact and fast.
# =============================================================================


class _DummyBooster:
    best_iteration = 1

    def predict(self, X, num_iteration=None):
        return np.zeros(len(X))


def test_per_fold_refit_uses_only_the_folds_own_training_window(monkeypatch):
    captured_labels: list[np.ndarray] = []

    class _DummyDataset:
        def __init__(
            self, data, label=None, categorical_feature=None, reference=None, free_raw_data=None
        ):
            captured_labels.append(np.array(label))

    def _dummy_train(params, train_set, num_boost_round, valid_sets, callbacks):
        return _DummyBooster()

    monkeypatch.setattr(forecast_model.lgb, "Dataset", _DummyDataset)
    monkeypatch.setattr(forecast_model.lgb, "train", _dummy_train)

    # 1000 hourly rows, distinct sentinel target values so "did the trainer
    # see anything outside the window" is directly checkable.
    feature_rows = _make_feature_rows(1000)
    target_series = _make_target_series(1000, value_fn=lambda i: float(i))
    dataset = join_features_and_target(feature_rows, target_series)

    train_start = BASE + timedelta(hours=500)
    train_end = BASE + timedelta(hours=800)  # window = hours [500, 800) -> y in [500, 800)

    fit_quantile_model(dataset, train_start, train_end)

    all_labels = np.concatenate(captured_labels)  # train_ds + val_ds, exactly the window
    assert all_labels.min() >= 500.0
    assert all_labels.max() < 800.0
    assert len(all_labels) == 300  # every row in the window, none outside it


def test_run_model_walk_forward_refits_per_fold_not_globally(monkeypatch):
    """
    Two folds with disjoint training windows should each only ever see their
    own window's labels -- not the union, and not the whole series.
    """
    captured_windows: list[tuple[float, float]] = []

    class _DummyDataset:
        def __init__(
            self, data, label=None, categorical_feature=None, reference=None, free_raw_data=None
        ):
            arr = np.array(label)
            if len(arr):
                captured_windows.append((float(arr.min()), float(arr.max())))

    def _dummy_train(params, train_set, num_boost_round, valid_sets, callbacks):
        return _DummyBooster()

    monkeypatch.setattr(forecast_model.lgb, "Dataset", _DummyDataset)
    monkeypatch.setattr(forecast_model.lgb, "train", _dummy_train)

    feature_rows = _make_feature_rows(2000)
    target_series = _make_target_series(2000, value_fn=lambda i: float(i))
    dataset = join_features_and_target(feature_rows, target_series)

    fold_a = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=1000),
        test_start=BASE + timedelta(hours=1000),
        test_end=BASE + timedelta(hours=1030),
    )
    fold_b = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=1500),
        test_start=BASE + timedelta(hours=1500),
        test_end=BASE + timedelta(hours=1530),
    )

    run_model_walk_forward(dataset, [fold_a, fold_b], timedelta(hours=200))

    # fold_a's window: hours [800, 1000) -> labels 800..999
    # fold_b's window: hours [1300, 1500) -> labels 1300..1499
    # Each fold produces 2 Dataset() calls (train_ds, val_ds); every captured
    # window must be a subset of its OWN fold's bound, never bleed into the
    # other fold's window or the full series.
    for lo, hi in captured_windows:
        in_fold_a = 800.0 <= lo and hi < 1000.0
        in_fold_b = 1300.0 <= lo and hi < 1500.0
        assert in_fold_a or in_fold_b, (lo, hi)


# =============================================================================
# Non-crossing quantiles (design §2) -- the required, non-optional assertion.
# =============================================================================


def test_predict_output_is_never_crossing():
    """
    Deliberately crossed raw per-tau predictions (as independent per-tau
    models can produce) -- `ForecastQuantileModel.predict` must sort each
    row ascending regardless.
    """

    class _FixedBooster:
        best_iteration = 1

        def __init__(self, value):
            self.value = value

        def predict(self, X, num_iteration=None):
            return np.full(len(X), self.value)

    # Deliberately crossed: tau=0.9's raw prediction is LOWER than tau=0.1's.
    boosters = {
        0.1: _FixedBooster(9.0),
        0.25: _FixedBooster(2.0),
        0.5: _FixedBooster(5.0),
        0.75: _FixedBooster(1.0),
        0.9: _FixedBooster(0.5),
    }
    model = ForecastQuantileModel(quantiles=QUANTILES, boosters=boosters, columns=("x",))

    preds = model.predict(np.zeros((3, 1)))

    assert preds.shape == (3, len(QUANTILES))
    for row in preds:
        assert list(row) == sorted(row.tolist()), "predicted quantile row is not non-decreasing"


def test_predict_output_is_non_crossing_with_a_real_fitted_model():
    """
    Real LightGBM training on a small, noisy synthetic sample -- the exact
    regime where independent per-tau models are most likely to cross for at
    least one row if the sort fix were removed.
    """
    rng = np.random.default_rng(0)
    n = 300
    feature_rows = _make_feature_rows(n)
    noise = rng.normal(scale=5.0, size=n)
    target_series = [
        (BASE + timedelta(hours=i), float(10 + (i % 24) + noise[i])) for i in range(n)
    ]
    dataset = join_features_and_target(feature_rows, target_series)

    config = ForecastModelConfig(n_estimators=20, num_leaves=7, early_stopping_rounds=5)
    model = fit_quantile_model(dataset, BASE, BASE + timedelta(hours=n), config)

    preds = model.predict(dataset.X)
    diffs = np.diff(preds, axis=1)
    assert np.all(diffs >= -1e-9), "quantile crossing detected in a real fitted model's output"


# =============================================================================
# run_model_walk_forward -- walk-forward only, reuses Fold/pinball_loss/
# WalkForwardResult (design §5/§6)
# =============================================================================


def test_run_model_walk_forward_raises_on_a_malformed_fold():
    feature_rows = _make_feature_rows(400)
    target_series = _make_target_series(400)
    dataset = join_features_and_target(feature_rows, target_series)

    bad_fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=300),
        test_start=BASE + timedelta(hours=100),  # precedes train_end -- invalid
        test_end=BASE + timedelta(hours=130),
    )

    with pytest.raises(AssertionError, match="precedes"):
        run_model_walk_forward(dataset, [bad_fold], timedelta(hours=300))


def test_run_model_walk_forward_raises_on_empty_folds():
    dataset = join_features_and_target(_make_feature_rows(10), _make_target_series(10))
    with pytest.raises(ValueError, match="no folds"):
        run_model_walk_forward(dataset, [], timedelta(days=365))


def test_run_model_walk_forward_end_to_end_produces_finite_pinball_losses():
    """
    Small but real end-to-end run: fit + predict + pinball-loss pooling over
    two folds, checking the result shape/finiteness rather than any specific
    loss value (this is not a "does the model beat the bar" test -- that is
    `scripts/generate_forecast_report.py`'s job against real data).
    """
    rng = np.random.default_rng(1)
    n = 700
    feature_rows = _make_feature_rows(n)
    noise = rng.normal(scale=3.0, size=n)
    target_series = [
        (BASE + timedelta(hours=i), float(10 + (i % 24) + noise[i])) for i in range(n)
    ]
    dataset = join_features_and_target(feature_rows, target_series)

    fold_a = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=500),
        test_start=BASE + timedelta(hours=500),
        test_end=BASE + timedelta(hours=550),
    )
    fold_b = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=550),
        test_start=BASE + timedelta(hours=550),
        test_end=BASE + timedelta(hours=600),
    )
    config = ForecastModelConfig(n_estimators=20, num_leaves=7, early_stopping_rounds=5)

    result = run_model_walk_forward(dataset, [fold_a, fold_b], timedelta(hours=500), config)

    assert result.fold_count == 2
    assert set(result.per_quantile_loss.keys()) == set(QUANTILES)
    for loss in result.per_quantile_loss.values():
        assert loss == loss and loss >= 0  # not NaN, non-negative
    assert len(result.per_fold_quantile_loss) == 2


# =============================================================================
# ForecastModelConfig -- quantiles must be identical to shared.baselines.
# QUANTILES, or the comparison is void (design §2).
# =============================================================================


def test_config_rejects_quantiles_that_differ_from_baselines_quantiles():
    with pytest.raises(ValueError, match="QUANTILES"):
        ForecastModelConfig(quantiles=(0.1, 0.5, 0.9))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"num_leaves": 1},
        {"learning_rate": 0.0},
        {"n_estimators": 0},
        {"min_child_samples": 0},
        {"early_stopping_rounds": 0},
        {"validation_frac": 0.0},
        {"validation_frac": 1.0},
    ],
)
def test_config_rejects_invalid_params(kwargs):
    with pytest.raises(ValueError):
        ForecastModelConfig(**kwargs)
