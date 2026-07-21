"""
M6 P3: quantile LightGBM forecast model for FCR-D DK2 capacity price
(docs/forecast-model-design.md).

Produces per-(product, tau) quantile forecasts and evaluates them exactly the way
`docs/forecast-baseline-results.md` was produced -- against `shared.baselines`'s
own `pinball_loss`, `Fold`/`walk_forward_folds`/`trailing_folds`, and
`WalkForwardResult` -- so a model number and a baseline number sit in the same
table and mean the same thing (design §5's reuse mandate). This module does not
reimplement any of those; it imports them.

**No scikit-learn.** LightGBM's `sklearn`-flavoured API (`LGBMRegressor`) requires
scikit-learn to be importable and raises at construction time if it is not --
verified live in this environment, where scikit-learn is absent. Rather than add
a *third* dependency the design never asked for, this module uses LightGBM's
native `Dataset`/`Booster` API (`lgb.Dataset`, `lgb.train`) throughout, which has
no such requirement. Only `numpy` and `lightgbm` are added to `pyproject.toml`
(design §1) -- no pandas, no scikit-learn.

**Model** (design §2): one LightGBM quantile regressor per `(product, tau)` for
`tau` in `shared.baselines.QUANTILES` -- imported, not redeclared, so a config
that drifted from the baselines' own quantile set would be a loud `ValueError`
(`ForecastModelConfig.__post_init__`) rather than a silently-void comparison.
Modest, fixed tree params (`num_leaves`, `learning_rate` low; `n_estimators` is
an upper bound, actual trees chosen by early stopping against a validation tail
of the training window) -- this is a small-data regime per design §3, not
somewhere to let a deep model memorise the fold.

**Quantile crossing** (design §2): five independent per-tau models can produce
`tau=0.9 < tau=0.5`. `ForecastQuantileModel.predict` sorts each row's predicted
quantile vector ascending after prediction (isotonic-in-tau) -- cheap,
non-optional, and asserted by `tests/test_forecast_model.py`.

**Training window -- bounded trailing, never expanding** (design §3):
`effective_train_window(fold, lookback)` mirrors `shared.baselines.
fit_conditional_climatology`'s own `lookback` parameter exactly:
`effective_start = max(fold.train_start, fold.train_end - lookback)`,
`fold.train_end` (== `fold.test_start`, by `walk_forward_folds`'s own
construction) never moves. `fold.train_start` is `walk_forward_folds`'s always-
`start`-anchored (expanding) lower bound -- this module never reads it as "the"
training start; it is only ever a floor for an early fold whose own span is
shorter than `lookback`, exactly as B2-rolling already established for the
baselines. `LOOKBACKS` declares both reported values (12mo, 18mo, design §3) --
two pre-declared lookbacks, never a swept "best".

**Features and target, and the join** (design §4): features come from
`shared.feature_store.build_features(db, zone, start, end,
horizon=FEATURE_HORIZON)` only -- `join_features_and_target` never reaches
around it to a raw table. `zone` and `mtu_start` are dropped as features (the
former constant, the latter the join key); `CATEGORICAL_FEATURES` are declared
LightGBM categoricals by column index into the feature store's own (sorted,
deterministic per design §"Schema determinism") key set. The join is on
`mtu_start == target.time`, inner: a feature row with no matching target point
is dropped, and the drop count (both directions) is logged, never silently
absorbed.

**Per-fold refitting only** (design §4/§6): `fit_quantile_model` takes an
explicit `[train_start, train_end)` and reads only `dataset.X`/`dataset.y` rows
whose time falls in that window -- mirroring `shared.baselines.
fit_seasonal_naive`/`fit_conditional_climatology`'s own `(series, train_start,
train_end)` signature convention. `run_model_walk_forward` calls it once per
fold, fresh every time; nothing here ever fits one model across the whole
series. See `tests/test_forecast_model.py`'s
`test_per_fold_refit_uses_only_the_folds_own_training_window` for the
regression test (a `lgb.Dataset` spy, not a real model fit, so the assertion is
exact and fast).
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

import lightgbm as lgb
import numpy as np

from shared.baselines import QUANTILES, Fold, WalkForwardResult, pinball_loss

logger = logging.getLogger(__name__)

# design §4: the only horizon this module ever requests from the feature store.
# Matches P1's D-1-model canonical horizon and P2's `TARGET_HORIZON` constant.
FEATURE_HORIZON = timedelta(hours=12)

# design §4: LightGBM categorical features, declared by name (resolved to a
# column index against build_features's own sorted key set in
# `join_features_and_target`, never hardcoded as a position).
CATEGORICAL_FEATURES: tuple[str, ...] = (
    "hour_of_day",
    "day_of_week",
    "month",
    "is_danish_public_holiday",
    "is_after_d1_gate",
)

# build_features's row-only keys that are not features at all (module
# docstring's "drop zone/mtu_start" -- the former constant, the latter the
# join key).
NON_FEATURE_KEYS = frozenset({"zone", "mtu_start"})

# design §3: two pre-declared lookbacks, both reported, never a swept "best".
LOOKBACKS: dict[str, timedelta] = {
    "12mo": timedelta(days=365),
    "18mo": timedelta(days=548),  # ~18 months
}

# Floors below which a fold's own training window is too small to fit anything
# meaningful -- a `ValueError`, matching `fit_seasonal_naive`/
# `fit_conditional_climatology`'s own "raise, don't return garbage" convention,
# not a silent fallback. Deployment-relevant (headline) folds have thousands of
# hourly rows even at the shorter 12-month lookback, so this floor is a
# guardrail against a misconfigured window, not something real folds are
# expected to hit.
MIN_TRAINING_ROWS = 200
MIN_VALIDATION_ROWS = 48


@dataclass(frozen=True)
class ForecastModelConfig:
    """
    Knobs for `fit_quantile_model`/`run_model_walk_forward` -- dataclass config,
    no hidden globals, matching `shared/bess_simulator.py`'s `BessConfig` and
    `shared/feature_store.py`'s `FeatureStoreConfig` convention. Defaults are
    deliberately modest (design §2: "a deep model will memorise" this small-data
    regime), fixed, and never swept against the evaluation window (design §7).
    """

    quantiles: tuple[float, ...] = QUANTILES
    categorical_features: tuple[str, ...] = CATEGORICAL_FEATURES
    num_leaves: int = 15
    learning_rate: float = 0.05
    n_estimators: int = 300
    min_child_samples: int = 30
    early_stopping_rounds: int = 20
    # Fraction of a fold's own training window held out as a **causal tail**
    # (the chronologically LAST rows, never a random split) for early stopping
    # -- still strictly inside [train_start, train_end), so this can never see
    # the test fold.
    validation_frac: float = 0.15

    def __post_init__(self):
        if self.quantiles != QUANTILES:
            raise ValueError(
                "ForecastModelConfig.quantiles must be identical to "
                "shared.baselines.QUANTILES -- design §2: 'the same metric and "
                "quantiles as P2 -- identical, or the comparison is void'"
            )
        if self.num_leaves < 2:
            raise ValueError("num_leaves must be at least 2")
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if self.n_estimators <= 0:
            raise ValueError("n_estimators must be positive")
        if self.min_child_samples <= 0:
            raise ValueError("min_child_samples must be positive")
        if self.early_stopping_rounds <= 0:
            raise ValueError("early_stopping_rounds must be positive")
        if not 0.0 < self.validation_frac < 1.0:
            raise ValueError("validation_frac must be in (0, 1)")


# --- feature/target join (design §4) ----------------------------------------


def _to_float(value: object) -> float:
    """`None` -> `NaN` (LightGBM's native missing-value handling, not a
    substituted zero); `bool` -> `1.0`/`0.0` (two of the declared categoricals,
    `is_danish_public_holiday`/`is_after_d1_gate`, are booleans in
    `build_features`'s output); everything else -> `float(value)`.
    """
    if value is None:
        return float("nan")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    return float(value)


def _feature_columns(feature_rows: list[dict]) -> tuple[str, ...]:
    """
    Deterministic, sorted feature-column order (alphabetical over the first
    row's keys, minus `NON_FEATURE_KEYS`) -- `build_features`'s own schema is
    already deterministic per `(zone, horizon)` (module docstring's "Schema
    determinism" note in `shared/feature_store.py`), so every row shares this
    same key set; sorting just fixes a stable, reviewable column order for the
    resulting `numpy` matrix.
    """
    if not feature_rows:
        return ()
    return tuple(sorted(k for k in feature_rows[0] if k not in NON_FEATURE_KEYS))


@dataclass(frozen=True)
class JoinedDataset:
    """
    `join_features_and_target`'s return value: one row per MTU present in
    BOTH `build_features`'s output and the target series (design §4's inner
    join), as parallel `numpy` arrays plus the exact column order/categorical
    indices used to build them.
    """

    times: list[datetime]
    time_epochs: np.ndarray  # float64 seconds, same row order as X/y
    X: np.ndarray
    y: np.ndarray
    columns: tuple[str, ...]
    categorical_idx: tuple[int, ...]
    dropped_no_target: int
    dropped_no_feature: int


def join_features_and_target(
    feature_rows: list[dict],
    target_series: list[tuple[datetime, float]],
    categorical_features: tuple[str, ...] = CATEGORICAL_FEATURES,
) -> JoinedDataset:
    """
    Inner-joins `build_features`'s rows to `target_series` on `mtu_start ==
    time` (design §4). An hour with a feature row but no target point is
    dropped (`dropped_no_target`); a target point with no feature row is
    dropped too (`dropped_no_feature`) -- both counts are logged, never
    silently absorbed. Declared `categorical_features` not present in the
    feature store's own schema raise `ValueError` immediately (a naming drift
    between this module and `shared/feature_store.py` must be loud, not a
    quietly-wrong column index).
    """
    columns = _feature_columns(feature_rows)
    if not feature_rows:
        return JoinedDataset(
            times=[],
            time_epochs=np.empty(0, dtype=np.float64),
            X=np.empty((0, 0), dtype=np.float64),
            y=np.empty(0, dtype=np.float64),
            columns=columns,
            categorical_idx=(),
            dropped_no_target=0,
            dropped_no_feature=len(target_series),
        )

    missing_cat = [c for c in categorical_features if c not in columns]
    if missing_cat:
        raise ValueError(
            f"declared categorical feature(s) not present in the feature store's "
            f"schema: {missing_cat}"
        )
    categorical_idx = tuple(columns.index(c) for c in categorical_features)

    target_map = dict(target_series)
    feature_times = {row["mtu_start"] for row in feature_rows}
    dropped_no_feature = sum(1 for t, _ in target_series if t not in feature_times)

    times: list[datetime] = []
    rows: list[list[float]] = []
    y: list[float] = []
    dropped_no_target = 0
    for row in sorted(feature_rows, key=lambda r: r["mtu_start"]):
        t = row["mtu_start"]
        actual = target_map.get(t)
        if actual is None:
            dropped_no_target += 1
            continue
        times.append(t)
        rows.append([_to_float(row[c]) for c in columns])
        y.append(actual)

    if dropped_no_target or dropped_no_feature:
        logger.info(
            "join_features_and_target: dropped %d feature row(s) with no matching "
            "target, %d target point(s) with no matching feature row (%d joined)",
            dropped_no_target,
            dropped_no_feature,
            len(times),
        )

    X = np.array(rows, dtype=np.float64) if rows else np.empty((0, len(columns)), dtype=np.float64)
    y_arr = np.array(y, dtype=np.float64)
    time_epochs = np.array([t.timestamp() for t in times], dtype=np.float64)
    return JoinedDataset(
        times=times,
        time_epochs=time_epochs,
        X=X,
        y=y_arr,
        columns=columns,
        categorical_idx=categorical_idx,
        dropped_no_target=dropped_no_target,
        dropped_no_feature=dropped_no_feature,
    )


# --- bounded trailing training window (design §3) ---------------------------


def effective_train_window(fold: Fold, lookback: timedelta) -> tuple[datetime, datetime]:
    """
    The bounded trailing training window for one fold (design §3):
    `effective_start = max(fold.train_start, fold.train_end - lookback)`,
    `fold.train_end` unchanged. Mirrors `shared.baselines.
    fit_conditional_climatology`'s own `lookback` semantics exactly (the
    B2-rolling baseline's approach) -- an early fold whose own
    `[fold.train_start, fold.train_end)` span is shorter than `lookback` falls
    back to that full span, by construction, not a special case. `fold.
    train_end` is never moved regardless of `lookback` -- that boundary (==
    `fold.test_start`, by `walk_forward_folds`'s construction) is what keeps
    this leak-safe.
    """
    if lookback <= timedelta(0):
        raise ValueError("lookback must be positive")
    return max(fold.train_start, fold.train_end - lookback), fold.train_end


# --- the model ----------------------------------------------------------------


@dataclass(frozen=True)
class ForecastQuantileModel:
    """
    One fitted set of per-tau LightGBM boosters (`fit_quantile_model`'s return
    value) -- `predict` is the only way this module ever turns them into a
    forecast, and it is where design §2's non-crossing fix lives.
    """

    quantiles: tuple[float, ...]
    boosters: dict[float, lgb.Booster]
    columns: tuple[str, ...]

    def predict(self, X: np.ndarray) -> np.ndarray:
        """
        `(n_rows, len(self.quantiles))` array, columns in `self.quantiles`
        order, **each row sorted ascending** (design §2): five independently
        fit per-tau models can produce `tau=0.9 < tau=0.5`x for a given row,
        which is nonsensical for a bid decision (the allocation layer, P4,
        consumes P(price > bid) and cannot use a crossed quantile function).
        Sorting each row is the isotonic-in-tau fix the design specifies --
        cheap, non-optional, asserted by
        `tests/test_forecast_model.py::test_predict_output_is_never_crossing`.
        """
        raw = np.column_stack(
            [
                self.boosters[tau].predict(X, num_iteration=self.boosters[tau].best_iteration)
                for tau in self.quantiles
            ]
        )
        return np.sort(raw, axis=1)


def fit_quantile_model(
    dataset: JoinedDataset,
    train_start: datetime,
    train_end: datetime,
    config: ForecastModelConfig | None = None,
) -> ForecastQuantileModel:
    """
    Fits one LightGBM quantile booster per `tau` in `config.quantiles`, on
    `[train_start, train_end)` of `dataset` **only** -- mirrors `shared.
    baselines.fit_seasonal_naive`/`fit_conditional_climatology`'s own explicit
    `(train_start, train_end)` signature so per-fold refitting is structural,
    not a convention a caller could forget (design §4's leak discipline;
    `run_model_walk_forward` is the only caller in this module, and it always
    passes `effective_train_window(fold, lookback)`).

    A causal validation tail -- the chronologically LAST `config.
    validation_frac` fraction of the training window's own rows, never a
    random split -- is held out for early stopping against `config.
    n_estimators`'s upper bound; still strictly inside `[train_start,
    train_end)`, so it can never see the test fold's own values.

    Raises `ValueError` if fewer than `MIN_TRAINING_ROWS` fall in the window
    -- a caller passing a window too short to fit anything meaningful must
    know that, not silently receive an undertrained model.
    """
    config = config or ForecastModelConfig()
    if train_end <= train_start:
        raise ValueError(f"train_end ({train_end}) must be after train_start ({train_start})")

    mask = (dataset.time_epochs >= train_start.timestamp()) & (
        dataset.time_epochs < train_end.timestamp()
    )
    n = int(mask.sum())
    if n < MIN_TRAINING_ROWS:
        raise ValueError(
            f"only {n} training row(s) in [{train_start}, {train_end}) -- need at "
            f"least {MIN_TRAINING_ROWS} to fit a quantile model"
        )

    X_win = dataset.X[mask]
    y_win = dataset.y[mask]

    n_val = max(MIN_VALIDATION_ROWS, int(n * config.validation_frac))
    # Always leave a meaningful training tail even at the row-count floor.
    n_val = min(n_val, n - MIN_TRAINING_ROWS // 2)
    split = n - n_val

    X_tr, y_tr = X_win[:split], y_win[:split]
    X_val, y_val = X_win[split:], y_win[split:]

    cat_idx = list(dataset.categorical_idx)
    train_ds = lgb.Dataset(X_tr, label=y_tr, categorical_feature=cat_idx, free_raw_data=False)
    val_ds = lgb.Dataset(
        X_val, label=y_val, reference=train_ds, categorical_feature=cat_idx, free_raw_data=False
    )

    boosters: dict[float, lgb.Booster] = {}
    for tau in config.quantiles:
        params = dict(
            objective="quantile",
            alpha=tau,
            num_leaves=config.num_leaves,
            learning_rate=config.learning_rate,
            min_child_samples=config.min_child_samples,
            verbosity=-1,
        )
        boosters[tau] = lgb.train(
            params,
            train_ds,
            num_boost_round=config.n_estimators,
            valid_sets=[val_ds],
            callbacks=[
                lgb.early_stopping(config.early_stopping_rounds, verbose=False),
                lgb.log_evaluation(0),
            ],
        )

    return ForecastQuantileModel(
        quantiles=config.quantiles, boosters=boosters, columns=dataset.columns
    )


# --- walk-forward evaluation harness (design §5/§6) --------------------------


def run_model_walk_forward(
    dataset: JoinedDataset,
    folds: list[Fold],
    lookback: timedelta,
    config: ForecastModelConfig | None = None,
) -> WalkForwardResult:
    """
    Design §6's headline harness for one lookback: for each fold, fits a
    fresh `ForecastQuantileModel` on `effective_train_window(fold, lookback)`
    (per-fold refit, never global -- `fit_quantile_model`), scores every
    `dataset` point whose time falls in `[fold.test_start, fold.test_end)`
    via the fitted model's non-crossing `.predict`, and pools `shared.
    baselines.pinball_loss` into a `shared.baselines.WalkForwardResult` --
    same metric, same shape as every baseline's own result, so the model's
    numbers sit in the same table as the bar (design §5's reuse mandate).
    The loop itself necessarily differs from `shared.baselines.
    run_walk_forward` (this model needs a feature matrix per point, not a
    `{time: value}` series lookup), but every piece it calls into --
    `Fold`, `pinball_loss`, `WalkForwardResult` -- is imported, not
    reimplemented.

    Raises `ValueError` if `folds` is empty, matching `run_walk_forward`'s own
    convention. Raises `AssertionError` if any fold's `test_start` precedes
    its own `train_end` -- structurally impossible from `walk_forward_folds`
    itself, but asserted directly here too (design §6: "a test asserts no
    test fold precedes its training data").
    """
    config = config or ForecastModelConfig()
    if not folds:
        raise ValueError("no folds to evaluate")

    pooled_losses: dict[float, list[float]] = defaultdict(list)
    per_fold: list[dict[float, float]] = []

    for fold in folds:
        if fold.test_start < fold.train_end:
            raise AssertionError(
                f"fold's test_start ({fold.test_start}) precedes its own train_end "
                f"({fold.train_end}) -- walk-forward invariant violated"
            )
        train_start, train_end = effective_train_window(fold, lookback)
        model = fit_quantile_model(dataset, train_start, train_end, config)

        test_mask = (dataset.time_epochs >= fold.test_start.timestamp()) & (
            dataset.time_epochs < fold.test_end.timestamp()
        )
        idx = np.where(test_mask)[0]
        fold_losses: dict[float, list[float]] = defaultdict(list)
        if len(idx):
            preds = model.predict(dataset.X[idx])
            for row_i, data_i in enumerate(idx):
                actual = float(dataset.y[data_i])
                for tau_i, tau in enumerate(config.quantiles):
                    loss = pinball_loss(actual, float(preds[row_i, tau_i]), tau)
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
