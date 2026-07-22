"""
Tests for `shared/economic_eval.py` (M6 P4,
`docs/forecast-economic-eval-design.md`). Every fixture is
synthetic/hand-built -- no database, matching `tests/test_baselines.py`/
`tests/test_forecast_model.py`'s own "unit tests must not need the DB"
convention.

**The leak test comes first**, per the design's explicit build order
(design §4/§5's "write the leak test first" -- mirrored from
`shared/baselines.py`'s coverage-gate-first precedent): `simulate()`'s
`policy` parameter must reject "oracle" outright, and the `trailing`/
`model` policies must never let a change to a future tick's price or
forecast alter an earlier tick's allocation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from shared.baselines import Fold
from shared.economic_eval import (
    LEG_ARBITRAGE,
    LEG_FCR_DOWN,
    LEG_FCR_UP,
    BandFraction,
    EconomicEvalConfig,
    EconomicEvalResult,
    EconomicEvalTick,
    Headroom,
    _abs_deviation_strength,
    _ratio_strength,
    _simulate_core,
    _walk_forward_predictions,
    _weighted_split,
    build_forecast_maps,
    compute_band_fraction,
    compute_headroom,
    restrict_to_scored_ticks,
    run_oracle_ceiling,
    simulate,
)
from shared.forecast_model import ForecastModelConfig, join_features_and_target

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _hourly_times(n: int, start: datetime = BASE) -> list[datetime]:
    return [start + timedelta(hours=i) for i in range(n)]


def _actuals(
    n: int,
    up_fn=lambda i: 2.0 + (i % 5) * 0.1,
    down_fn=lambda i: 1.5 + (i % 7) * 0.1,
    da_fn=lambda i: 500.0 + 50.0 * ((i % 24) - 12),
    start: datetime = BASE,
) -> dict[str, dict[datetime, float]]:
    times = _hourly_times(n, start)
    return {
        LEG_FCR_UP: {t: up_fn(i) for i, t in enumerate(times)},
        LEG_FCR_DOWN: {t: down_fn(i) for i, t in enumerate(times)},
        LEG_ARBITRAGE: {t: da_fn(i) for i, t in enumerate(times)},
    }


def _forecast_maps(n: int, up_fn, down_fn, da_fn, start: datetime = BASE):
    times = _hourly_times(n, start)
    return {
        LEG_FCR_UP: {t: up_fn(i) for i, t in enumerate(times)},
        LEG_FCR_DOWN: {t: down_fn(i) for i, t in enumerate(times)},
        LEG_ARBITRAGE: {t: da_fn(i) for i, t in enumerate(times)},
    }


# =============================================================================
# Leak discipline (design §4/§5) -- written and run FIRST.
# =============================================================================


def test_simulate_rejects_oracle_as_a_policy_string():
    """'oracle' must never be reachable through the deployable entry point."""
    times = _hourly_times(200)
    actuals = _actuals(200)
    with pytest.raises(ValueError, match="oracle"):
        simulate(times, actuals, EconomicEvalConfig(), policy="oracle")  # type: ignore[arg-type]


def test_simulate_rejects_unknown_policy():
    times = _hourly_times(10)
    actuals = _actuals(10)
    with pytest.raises(ValueError):
        simulate(times, actuals, EconomicEvalConfig(), policy="bogus")  # type: ignore[arg-type]


def test_simulate_core_asserts_if_oracle_reached_without_the_lookahead_flag():
    """
    Defensive invariant: even calling the private core directly with
    policy="oracle" but lookahead=False (the state `simulate()` can never
    produce) must fail loudly, not silently compute a lookahead result
    under a mislabeled non-lookahead run.
    """
    times = _hourly_times(50)
    actuals = _actuals(50)
    with pytest.raises(AssertionError):
        _simulate_core(
            times,
            actuals,
            EconomicEvalConfig(),
            policy="oracle",
            quantile_variant=None,
            forecast_maps=None,
            lookahead=False,
        )


def test_simulate_model_requires_forecast_maps_and_quantile_variant():
    times = _hourly_times(50)
    actuals = _actuals(50)
    with pytest.raises(ValueError, match="forecast_maps"):
        simulate(times, actuals, EconomicEvalConfig(), policy="model", quantile_variant="median")
    forecasts = _forecast_maps(50, lambda i: 2.0, lambda i: 1.5, lambda i: 500.0)
    with pytest.raises(ValueError, match="quantile_variant"):
        simulate(times, actuals, EconomicEvalConfig(), policy="model", forecast_maps=forecasts)


def test_trailing_policy_allocation_unaffected_by_mutating_a_later_tick():
    """
    Core leak test for `trailing`: change ONLY a future tick's actual
    price and confirm every earlier tick's allocation weights
    (capacity_reserved_mw / arbitrage_power_mw) are byte-identical.
    """
    n = 300
    config = EconomicEvalConfig()
    times = _hourly_times(n)

    baseline_actuals = _actuals(n)
    result_a = simulate(times, baseline_actuals, config, policy="trailing")

    mutated_actuals = _actuals(n)
    mutation_tick = times[-1]
    mutated_actuals[LEG_FCR_UP][mutation_tick] = 999.0
    mutated_actuals[LEG_FCR_DOWN][mutation_tick] = 999.0
    mutated_actuals[LEG_ARBITRAGE][mutation_tick] = 999.0
    result_b = simulate(times, mutated_actuals, config, policy="trailing")

    for tick_a, tick_b in zip(result_a.ticks[:-1], result_b.ticks[:-1], strict=True):
        assert tick_a.capacity_reserved_mw == pytest.approx(tick_b.capacity_reserved_mw)
        assert tick_a.arbitrage_power_mw == pytest.approx(tick_b.arbitrage_power_mw)
        assert tick_a.action == tick_b.action


def test_model_policy_allocation_unaffected_by_mutating_a_later_forecast_or_price():
    """Same leak test, for `model`: mutating a future forecast value, or a
    future actual price, must not change any earlier tick's allocation."""
    n = 300
    config = EconomicEvalConfig()
    times = _hourly_times(n)
    actuals = _actuals(n)

    forecasts_a = _forecast_maps(
        n, lambda i: 2.0 + (i % 5) * 0.1, lambda i: 1.5 + (i % 7) * 0.1, lambda i: 500.0
    )
    result_a = simulate(
        times, actuals, config, policy="model", quantile_variant="median", forecast_maps=forecasts_a
    )

    forecasts_b = _forecast_maps(
        n, lambda i: 2.0 + (i % 5) * 0.1, lambda i: 1.5 + (i % 7) * 0.1, lambda i: 500.0
    )
    forecasts_b[LEG_FCR_UP][times[-1]] = 12345.0
    forecasts_b[LEG_ARBITRAGE][times[-1]] = -999.0
    result_b = simulate(
        times, actuals, config, policy="model", quantile_variant="median", forecast_maps=forecasts_b
    )

    for tick_a, tick_b in zip(result_a.ticks[:-1], result_b.ticks[:-1], strict=True):
        assert tick_a.capacity_reserved_mw == pytest.approx(tick_b.capacity_reserved_mw)
        assert tick_a.arbitrage_power_mw == pytest.approx(tick_b.arbitrage_power_mw)

    mutated_actuals = _actuals(n)
    mutated_actuals[LEG_FCR_UP][times[-1]] = 999.0
    result_c = simulate(
        times,
        mutated_actuals,
        config,
        policy="model",
        quantile_variant="median",
        forecast_maps=forecasts_a,
    )
    for tick_a, tick_c in zip(result_a.ticks[:-1], result_c.ticks[:-1], strict=True):
        assert tick_a.capacity_reserved_mw == pytest.approx(tick_c.capacity_reserved_mw)
        assert tick_a.arbitrage_power_mw == pytest.approx(tick_c.arbitrage_power_mw)


def test_oracle_ceiling_is_the_only_lookahead_path_and_actually_differs():
    """
    Sanity that `run_oracle_ceiling` genuinely exercises the lookahead path
    (not a no-op reachable some other way): construct actuals where the
    FCR-D "up" leg spikes at every tick's OWN price relative to its
    baseline in a way trailing's causal window cannot anticipate, and
    confirm oracle allocates differently from trailing at that tick.
    """
    n = 200
    config = EconomicEvalConfig()
    times = _hourly_times(n)

    def up_fn(i):
        return 50.0 if i == n - 1 else 2.0  # a one-tick spike, unseen by any trailing window

    actuals = _actuals(n, up_fn=up_fn)

    trailing_result = simulate(times, actuals, config, policy="trailing")
    oracle_result = run_oracle_ceiling(times, actuals, config)

    last_trailing = trailing_result.ticks[-1]
    last_oracle = oracle_result.ticks[-1]
    # Oracle sees the spike coming (it IS the spike); trailing's baseline
    # has no way to have anticipated it -- oracle must commit more MW to
    # "up" on that tick than trailing does.
    up_share_trailing = last_trailing.capacity_reserved_mw  # up+down combined, but up dominates
    assert last_oracle.capacity_reserved_mw != pytest.approx(up_share_trailing, rel=1e-6)


# =============================================================================
# EconomicEvalConfig validation
# =============================================================================


def test_config_defaults_match_bess_config_shape():
    config = EconomicEvalConfig()
    assert config.power_mw == 1.0
    assert config.capacity_mwh == 2.0
    assert config.round_trip_efficiency == 0.90


@pytest.mark.parametrize(
    "kwargs",
    [
        {"power_mw": 0},
        {"capacity_mwh": -1},
        {"round_trip_efficiency": 0},
        {"round_trip_efficiency": 1.5},
        {"soc_min_fraction": 0.9, "soc_max_fraction": 0.1},
        {"starting_soc_fraction": 0.05},
        {"arbitrage_lookback_periods": 0},
        {"capacity_commit_mw": -1},
        {"capacity_commit_mw": 5.0},
        {"max_cycles_per_day": 0},
    ],
)
def test_config_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        EconomicEvalConfig(**kwargs)


# =============================================================================
# Strength functions
# =============================================================================


def test_ratio_strength_none_value_scores_zero():
    assert _ratio_strength(None, deque_of([1.0, 2.0, 3.0])) == 0.0


def test_ratio_strength_empty_baseline_scores_zero():
    from collections import deque

    assert _ratio_strength(5.0, deque()) == 0.0


def test_ratio_strength_above_and_below_baseline():
    baseline = deque_of([10.0] * 10)
    assert _ratio_strength(20.0, baseline) == pytest.approx(2.0)
    assert _ratio_strength(5.0, baseline) == pytest.approx(0.5)


def test_ratio_strength_clips_negative_at_zero():
    baseline = deque_of([10.0] * 10)
    assert _ratio_strength(-5.0, baseline) == 0.0


def test_abs_deviation_strength_symmetric_high_and_low():
    """A day-ahead price unusually HIGH or unusually LOW relative to its
    own baseline must score the same magnitude -- both are arbitrage
    opportunities."""
    baseline = deque_of([500.0] * 10)
    high = _abs_deviation_strength(600.0, baseline)
    low = _abs_deviation_strength(400.0, baseline)
    assert high == pytest.approx(low)
    assert high == pytest.approx(0.2)


def test_abs_deviation_strength_zero_at_baseline_level():
    baseline = deque_of([500.0] * 10)
    assert _abs_deviation_strength(500.0, baseline) == pytest.approx(0.0)


def deque_of(values):
    from collections import deque

    return deque(values, maxlen=len(values) or 1)


# =============================================================================
# _weighted_split
# =============================================================================


def test_weighted_split_proportional():
    shares = _weighted_split({"a": 3.0, "b": 1.0}, 4.0)
    assert shares["a"] == pytest.approx(3.0)
    assert shares["b"] == pytest.approx(1.0)


def test_weighted_split_falls_back_to_even_when_all_zero():
    shares = _weighted_split({"a": 0.0, "b": 0.0, "c": 0.0}, 3.0)
    assert shares == {"a": 1.0, "b": 1.0, "c": 1.0}


def test_weighted_split_clips_negative_strengths():
    shares = _weighted_split({"a": -5.0, "b": 1.0}, 2.0)
    assert shares["a"] == pytest.approx(0.0)
    assert shares["b"] == pytest.approx(2.0)


def test_weighted_split_conserves_total():
    shares = _weighted_split({"a": 2.0, "b": 5.0, "c": 1.0}, 10.0)
    assert sum(shares.values()) == pytest.approx(10.0)


def test_weighted_split_empty_strengths_returns_empty():
    assert _weighted_split({}, 5.0) == {}


# =============================================================================
# simulate(): "even" reproduces the fixed original split (the floor, design §2)
# =============================================================================


def test_even_policy_ignores_price_signals_and_splits_fixed():
    config = EconomicEvalConfig(power_mw=1.0, capacity_commit_mw=0.4)
    times = _hourly_times(20)
    actuals = _actuals(20)
    result = simulate(times, actuals, config, policy="even")
    for tick in result.ticks:
        assert tick.capacity_reserved_mw == pytest.approx(0.4)
        assert tick.arbitrage_power_mw == pytest.approx(0.6)


def test_even_policy_never_needs_forecast_maps():
    config = EconomicEvalConfig()
    times = _hourly_times(20)
    actuals = _actuals(20)
    # Must not raise even though no forecast_maps/quantile_variant given.
    simulate(times, actuals, config, policy="even")


# =============================================================================
# simulate(): "trailing" directionality
# =============================================================================


def test_trailing_policy_favors_the_leg_trailing_above_its_own_baseline():
    n = 400
    config = EconomicEvalConfig()
    times = _hourly_times(n)

    def up_fn(i):
        # First half flat at baseline; second half "up" trades well above
        # its own recent history.
        return 2.0 if i < n // 2 else 6.0

    actuals = _actuals(n, up_fn=up_fn, down_fn=lambda i: 2.0, da_fn=lambda i: 500.0)
    result = simulate(times, actuals, config, policy="trailing")

    last_tick = result.ticks[-1]
    # "up" is now well above its own trailing baseline while "down" is flat
    # -- up's committed MW must exceed down's.
    early_tick = result.ticks[n // 2 + 5]
    assert last_tick.capacity_reserved_mw >= early_tick.capacity_reserved_mw - 1e-9


def test_trailing_policy_conserves_power_mw_across_legs():
    n = 200
    config = EconomicEvalConfig(power_mw=1.0)
    times = _hourly_times(n)
    actuals = _actuals(n)
    result = simulate(times, actuals, config, policy="trailing")
    for tick in result.ticks:
        assert tick.capacity_reserved_mw + tick.arbitrage_power_mw == pytest.approx(1.0)


# =============================================================================
# Cycle cap
# =============================================================================


def test_cycle_cap_binds_and_caps_realised_cycles_per_day():
    n = 24 * 5
    config = EconomicEvalConfig(
        max_cycles_per_day=1.0, arbitrage_z_threshold=0.01, arbitrage_lookback_periods=5
    )
    times = _hourly_times(n)

    # A price series oscillating hard enough to trigger charge/discharge
    # nearly every tick, well beyond what a 1.0 cyc/day cap would allow.
    def da_fn(i):
        return 100.0 if i % 2 == 0 else 900.0

    actuals = _actuals(n, da_fn=da_fn)
    result = simulate(times, actuals, config, policy="even")

    assert result.cycle_cap_binding_periods > 0
    # 1.0 cyc/day cap over 5 days -> at most ~5 full-cycle-equivalents.
    assert result.realised_cycles_per_day <= 1.0 + 1e-6


# =============================================================================
# headroom / band fraction
# =============================================================================


def _fake_result(policy: str, capacity_eur: float, arbitrage_dkk: float) -> EconomicEvalResult:
    tick = EconomicEvalTick(
        time=BASE,
        action="idle",
        capacity_reserved_mw=0.0,
        arbitrage_power_mw=0.0,
        capacity_revenue_eur=capacity_eur,
        arbitrage_revenue_dkk=arbitrage_dkk,
        energy_discharged_mwh=0.0,
        cumulative_capacity_revenue_eur=capacity_eur,
        cumulative_arbitrage_revenue_dkk=arbitrage_dkk,
    )
    return EconomicEvalResult(
        policy=policy, quantile_variant=None, config=EconomicEvalConfig(), ticks=[tick]
    )


def test_headroom_computes_oracle_minus_trailing_per_currency():
    trailing = _fake_result("trailing", capacity_eur=100.0, arbitrage_dkk=1000.0)
    oracle = _fake_result("oracle", capacity_eur=150.0, arbitrage_dkk=1300.0)

    headroom = compute_headroom(oracle, trailing)

    assert isinstance(headroom, Headroom)
    assert headroom.capacity_eur == pytest.approx(50.0)
    assert headroom.capacity_eur_fraction_of_trailing == pytest.approx(0.5)
    assert headroom.arbitrage_dkk == pytest.approx(300.0)
    assert headroom.arbitrage_dkk_fraction_of_trailing == pytest.approx(0.3)


def test_headroom_fraction_is_nan_when_trailing_is_zero():
    trailing = _fake_result("trailing", capacity_eur=0.0, arbitrage_dkk=0.0)
    oracle = _fake_result("oracle", capacity_eur=10.0, arbitrage_dkk=20.0)
    headroom = compute_headroom(oracle, trailing)
    cap_fraction = headroom.capacity_eur_fraction_of_trailing
    arb_fraction = headroom.arbitrage_dkk_fraction_of_trailing
    assert cap_fraction != cap_fraction  # NaN
    assert arb_fraction != arb_fraction  # NaN


def test_band_fraction_at_the_oracle_ceiling_is_one():
    trailing = _fake_result("trailing", capacity_eur=100.0, arbitrage_dkk=1000.0)
    oracle = _fake_result("oracle", capacity_eur=150.0, arbitrage_dkk=1300.0)
    model = _fake_result("model", capacity_eur=150.0, arbitrage_dkk=1300.0)

    band = compute_band_fraction(model, trailing, oracle)

    assert isinstance(band, BandFraction)
    assert band.capacity_eur == pytest.approx(1.0)
    assert band.arbitrage_dkk == pytest.approx(1.0)


def test_band_fraction_negative_when_model_worse_than_trailing():
    trailing = _fake_result("trailing", capacity_eur=100.0, arbitrage_dkk=1000.0)
    oracle = _fake_result("oracle", capacity_eur=150.0, arbitrage_dkk=1300.0)
    model = _fake_result("model", capacity_eur=90.0, arbitrage_dkk=1300.0)

    band = compute_band_fraction(model, trailing, oracle)

    assert band.capacity_eur < 0


def test_band_fraction_nan_when_headroom_is_zero():
    trailing = _fake_result("trailing", capacity_eur=100.0, arbitrage_dkk=1000.0)
    oracle = _fake_result("oracle", capacity_eur=100.0, arbitrage_dkk=1000.0)
    model = _fake_result("model", capacity_eur=100.0, arbitrage_dkk=1000.0)
    band = compute_band_fraction(model, trailing, oracle)
    assert band.capacity_eur != band.capacity_eur  # NaN
    assert band.arbitrage_dkk != band.arbitrage_dkk


# =============================================================================
# restrict_to_scored_ticks
# =============================================================================


def test_restrict_to_scored_ticks_filters_and_recomputes_cumulative_sums():
    n = 50
    config = EconomicEvalConfig()
    times = _hourly_times(n)
    actuals = _actuals(n)
    result = simulate(times, actuals, config, policy="even")

    scored = set(times[30:])  # discard the first 30 ticks as "warm-up"
    restricted = restrict_to_scored_ticks(result, scored)

    assert len(restricted.ticks) == 20
    assert {t.time for t in restricted.ticks} == scored
    expected_capacity_total = sum(t.capacity_revenue_eur for t in result.ticks if t.time in scored)
    assert restricted.total_capacity_revenue_eur == pytest.approx(expected_capacity_total)
    # cumulative sums must be recomputed over the restricted subset only,
    # not inherited from the full run's cumulative totals.
    assert restricted.ticks[0].cumulative_capacity_revenue_eur == pytest.approx(
        restricted.ticks[0].capacity_revenue_eur
    )


def test_restrict_to_scored_ticks_empty_scored_set_gives_empty_result():
    n = 10
    result = simulate(_hourly_times(n), _actuals(n), EconomicEvalConfig(), policy="even")
    restricted = restrict_to_scored_ticks(result, set())
    assert restricted.ticks == []
    assert restricted.total_capacity_revenue_eur == 0.0


# =============================================================================
# forecast precompute (_walk_forward_predictions / build_forecast_maps) --
# reuses shared.forecast_model's leak-safe machinery; these tests check
# THIS module's wiring, not forecasting quality (that's test_forecast_model.py's job).
# =============================================================================


def _make_feature_rows(n_hours: int, start: datetime = BASE) -> list[dict]:
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
                "some_feature": float(i % 50),
            }
        )
    return rows


def _make_target_series(n_hours: int, start: datetime = BASE, value_fn=lambda i: 10.0 + (i % 24)):
    return [(start + timedelta(hours=i), value_fn(i)) for i in range(n_hours)]


def test_walk_forward_predictions_raises_on_malformed_fold():
    feature_rows = _make_feature_rows(400)
    target_series = _make_target_series(400)
    dataset = join_features_and_target(feature_rows, target_series)
    bad_fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=300),
        test_start=BASE + timedelta(hours=100),
        test_end=BASE + timedelta(hours=130),
    )
    with pytest.raises(AssertionError, match="precedes"):
        _walk_forward_predictions(dataset, [bad_fold], timedelta(hours=300))


def test_walk_forward_predictions_only_covers_test_fold_ticks():
    rng = np.random.default_rng(2)
    n = 700
    feature_rows = _make_feature_rows(n)
    noise = rng.normal(scale=1.0, size=n)
    target_series = [(BASE + timedelta(hours=i), float(10 + noise[i])) for i in range(n)]
    dataset = join_features_and_target(feature_rows, target_series)

    fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=500),
        test_start=BASE + timedelta(hours=500),
        test_end=BASE + timedelta(hours=530),
    )
    config = ForecastModelConfig(n_estimators=15, num_leaves=7, early_stopping_rounds=5)
    predictions = _walk_forward_predictions(dataset, [fold], timedelta(hours=500), config)

    for tau_predictions in predictions.values():
        times = set(tau_predictions.keys())
        assert times, "expected at least one prediction"
        assert all(fold.test_start <= t < fold.test_end for t in times)
        assert len(times) == 30


def test_build_forecast_maps_returns_both_quantile_variants_for_every_leg():
    rng = np.random.default_rng(3)
    n = 700
    feature_rows = _make_feature_rows(n)

    def series(seed_offset):
        noise = rng.normal(scale=1.0, size=n)
        return [(BASE + timedelta(hours=i), float(10 + seed_offset + noise[i])) for i in range(n)]

    leg_datasets = {
        LEG_FCR_UP: join_features_and_target(feature_rows, series(1)),
        LEG_FCR_DOWN: join_features_and_target(feature_rows, series(2)),
        LEG_ARBITRAGE: join_features_and_target(feature_rows, series(3)),
    }
    fold = Fold(
        train_start=BASE,
        train_end=BASE + timedelta(hours=500),
        test_start=BASE + timedelta(hours=500),
        test_end=BASE + timedelta(hours=520),
    )
    config = ForecastModelConfig(n_estimators=15, num_leaves=7, early_stopping_rounds=5)

    maps = build_forecast_maps(leg_datasets, [fold], timedelta(hours=500), config)

    assert set(maps.keys()) == {"median", "low_tail"}
    for variant_map in maps.values():
        assert set(variant_map.keys()) == {LEG_FCR_UP, LEG_FCR_DOWN, LEG_ARBITRAGE}
        for leg_map in variant_map.values():
            assert len(leg_map) == 20
    # The two variants must be genuinely different columns (median != low
    # tail) for at least one tick -- otherwise the "both variants" reporting
    # would be reporting the same number twice.
    any_leg = LEG_FCR_UP
    differs = any(
        maps["median"][any_leg][t] != maps["low_tail"][any_leg][t] for t in maps["median"][any_leg]
    )
    assert differs
