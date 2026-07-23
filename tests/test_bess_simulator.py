from collections import deque
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shared.bess_simulator import (
    BessConfig,
    _causal_zscore,
    _lag24h_forecast,
    _leg_relative_strength,
    _value_at_or_before,
    run_backtest,
)

BASE_TIME = datetime(2026, 7, 16, tzinfo=UTC)


def _price_rows(
    values: list[float | None], start: datetime = BASE_TIME, hours: float = 1.0
) -> list[dict]:
    return [{"time": start + timedelta(hours=i * hours), "value": v} for i, v in enumerate(values)]


def _db_with_series(
    day_ahead: list[dict],
    fcr: list[dict] | None = None,
    afrr: list[dict] | None = None,
    fcr_up: list[dict] | None = None,
    fcr_down: list[dict] | None = None,
    activation: list[dict] | None = None,
    ffr: list[dict] | None = None,
):
    """Builds a MagicMock DatabaseManager whose fetch_series_values returns the given series
    per (market, product), matching shared.db_manager.DatabaseManager.fetch_series_values's
    signature. `fcr` answers ("FCR", "price"); `fcr_up`/`fcr_down` answer ("FCR", "up")/
    ("FCR", "down") (FCR-D legs); `afrr` answers ("aFRR_capacity", "up"); `activation`
    answers ("aFRR_energy", "activation_price"); `ffr` answers ("FFR", "price")."""
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        if market == "day_ahead":
            return day_ahead
        if market == "FCR" and product == "price":
            return fcr or []
        if market == "FCR" and product == "up":
            return fcr_up or []
        if market == "FCR" and product == "down":
            return fcr_down or []
        if market == "aFRR_capacity":
            return afrr or []
        if market == "aFRR_energy":
            return activation or []
        if market == "FFR" and product == "price":
            return ffr or []
        raise AssertionError(f"unexpected market/product {market!r}/{product!r} requested")

    db.fetch_series_values.side_effect = fetch_series_values
    return db


# --- BessConfig validation ---------------------------------------------------


def test_bess_config_defaults_are_sane():
    config = BessConfig()
    assert config.power_mw == 1.0
    assert config.capacity_mwh == 2.0
    assert config.soc_min_fraction == 0.10
    assert config.soc_max_fraction == 0.90
    assert config.starting_soc_fraction == 0.50
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
        {"capacity_commit_mw": -1},
        {"capacity_commit_mw": 5.0},  # exceeds default power_mw=1.0
    ],
)
def test_bess_config_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        BessConfig(**kwargs)


def test_bess_config_rejects_excluded_capacity_market():
    with pytest.raises(ValueError, match="not eligible"):
        BessConfig(capacity_markets=(("mFRR_capacity", "up"),))


def test_bess_config_rejects_excluded_price_market():
    with pytest.raises(ValueError, match="not eligible"):
        BessConfig(price_market="mFRR_EAM")


def test_bess_config_rejects_mfrr_capacity_extra():
    """Stage 3: the mFRR extra-auction market is the same domain rule as
    mFRR_capacity itself -- an extra auction doesn't change what a BESS can
    bid into."""
    with pytest.raises(ValueError, match="not eligible"):
        BessConfig(capacity_markets=(("mFRR_capacity_extra", "up"),))


# --- causal z-score -----------------------------------------------------------


def test_causal_zscore_none_with_insufficient_history():
    assert _causal_zscore([1.0, 2.0], 3.0) is None


def test_causal_zscore_none_with_zero_variance_history():
    assert _causal_zscore([5.0] * 10, 5.0) is None


def test_causal_zscore_computes_expected_value():
    history = [8.0, 9.0, 10.0, 11.0, 12.0]
    z = _causal_zscore(history, 10.0)
    assert z == pytest.approx(0.0, abs=1e-9)


def test_causal_zscore_positive_for_value_above_baseline():
    history = [8.0, 9.0, 10.0, 11.0, 12.0]
    z = _causal_zscore(history, 20.0)
    assert z > 0


# --- _value_at_or_before ------------------------------------------------------


def test_value_at_or_before_carries_forward_last_known_value():
    series = [(BASE_TIME, 1.0), (BASE_TIME + timedelta(hours=1), 2.0)]
    assert _value_at_or_before(series, BASE_TIME + timedelta(minutes=30)) == 1.0
    assert _value_at_or_before(series, BASE_TIME + timedelta(hours=2)) == 2.0


def test_value_at_or_before_none_when_no_entry_precedes_time():
    series = [(BASE_TIME + timedelta(hours=1), 1.0)]
    assert _value_at_or_before(series, BASE_TIME) is None


# --- _lag24h_forecast (O(n) two-pointer) ----------------------------------------


def _lag24h_forecast_naive_on_squared(
    actual_series: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    """The pre-fix `_lag24h_forecast` reference implementation, verbatim: one
    `_value_at_or_before` O(n) scan per point, O(n^2) overall -- kept ONLY
    here, as a slow/obviously-correct oracle the O(n) two-pointer rewrite
    must match exactly, never in production code."""
    forecast: list[tuple[datetime, float]] = []
    for t, v in actual_series:
        lag_value = _value_at_or_before(actual_series, t - timedelta(hours=24))
        forecast.append((t, lag_value if lag_value is not None else v))
    return forecast


def test_lag24h_forecast_matches_naive_on_regular_hourly_series():
    series = [(BASE_TIME + timedelta(hours=i), float(i) * 3.0 + 1.0) for i in range(72)]
    assert _lag24h_forecast(series) == _lag24h_forecast_naive_on_squared(series)


def test_lag24h_forecast_matches_naive_with_irregular_gaps():
    # Hourly for a day, then a 10-hour gap, then 15-minute MTUs -- deliberately
    # irregular (day-ahead vs. imbalance/FCR cadences genuinely differ), to
    # exercise the two-pointer's cutoff advancing correctly across gaps.
    times = (
        [BASE_TIME + timedelta(hours=i) for i in range(30)]
        + [BASE_TIME + timedelta(hours=40 + i * 0.25) for i in range(20)]
    )
    series = [(t, float(i) % 7 + 0.5) for i, t in enumerate(times)]
    assert _lag24h_forecast(series) == _lag24h_forecast_naive_on_squared(series)


def test_lag24h_forecast_matches_naive_across_a_dst_ish_wall_clock_jump():
    # A synthetic "DST-ish" jump: the wall-clock gap between two consecutive
    # points is 23h (as if an hour vanished), while every duration is still
    # computed as a plain, absolute (UTC, timezone-aware) timedelta -- exactly
    # what `t - timedelta(hours=24)` is, so the cutoff must still strictly
    # increase in lockstep with `t` regardless of the irregular gap.
    times = [BASE_TIME + timedelta(hours=i) for i in range(10)]
    times += [times[-1] + timedelta(hours=23)]  # the "jump"
    times += [times[-1] + timedelta(hours=i) for i in range(1, 20)]
    series = [(t, float(i) * 2.5) for i, t in enumerate(times)]
    assert _lag24h_forecast(series) == _lag24h_forecast_naive_on_squared(series)


def test_lag24h_forecast_cold_start_falls_back_to_own_value():
    series = [(BASE_TIME + timedelta(hours=i), 100.0 + i) for i in range(5)]
    forecast = _lag24h_forecast(series)
    # No point has a t-24h predecessor yet -- every forecast value falls back
    # to that same tick's own actual value (module docstring's cold-start
    # floor limitation, deliberately left as-is).
    assert forecast == series


def test_lag24h_forecast_empty_series():
    assert _lag24h_forecast([]) == []


# --- run_backtest: arbitrage strategy ------------------------------------------


def test_charges_on_low_price_and_discharges_on_high_price():
    # A baseline of "normal" prices around 100, then a sharp low tick, then
    # (after re-establishing baseline) a sharp high tick -- exercises both
    # sides of the threshold deterministically.
    noisy_baseline = [98.0, 102.0, 99.0, 101.0, 100.0, 100.0]
    values = noisy_baseline + [1.0] + noisy_baseline + [500.0]
    db = _db_with_series(_price_rows(values))
    config = BessConfig(arbitrage_lookback_periods=6, arbitrage_z_threshold=0.5)

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config)

    actions = [t.action for t in result.ticks]
    assert actions[6] == "charge"  # the 1.0 tick, sharply below baseline
    assert actions[13] == "discharge"  # the 500.0 tick, sharply above baseline
    assert result.ticks[6].arbitrage_revenue_dkk < 0  # paid to charge
    assert result.ticks[13].arbitrage_revenue_dkk > 0  # earned from discharging


def test_idle_when_price_within_normal_band():
    values = [100.0 + (i % 3) for i in range(20)]
    db = _db_with_series(_price_rows(values))
    config = BessConfig(arbitrage_lookback_periods=6, arbitrage_z_threshold=3.0)

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=20), config)

    assert all(t.action == "idle" for t in result.ticks)
    assert result.total_arbitrage_revenue_dkk == 0.0


def test_respects_soc_upper_bound_when_charging_repeatedly():
    # Every tick a sharp low relative to a noisy baseline -> charge every
    # time until SoC saturates at soc_max_fraction and stays there.
    values = [98.0, 102.0, 99.0, 101.0, 100.0, 100.0] + [1.0] * 10
    db = _db_with_series(_price_rows(values))
    config = BessConfig(
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.1,
        power_mw=1.0,
        capacity_mwh=1.0,
        capacity_commit_mw=0.0,
        capacity_markets=(),
    )

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config)

    soc_fractions = [t.soc_fraction for t in result.ticks]
    assert max(soc_fractions) <= config.soc_max_fraction + 1e-9
    assert soc_fractions[-1] == pytest.approx(config.soc_max_fraction, abs=1e-6)


def test_respects_power_limit_per_tick():
    values = [98.0, 102.0, 99.0, 101.0, 100.0, 100.0] + [1.0]
    db = _db_with_series(_price_rows(values))
    config = BessConfig(
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.1,
        power_mw=0.5,
        capacity_mwh=10.0,  # plenty of headroom, so power is the binding constraint
        capacity_commit_mw=0.0,
        capacity_markets=(),
    )

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config)

    charge_tick = result.ticks[-1]
    assert charge_tick.action == "charge"
    # grid energy drawn this tick cannot exceed power_mw * dt_hours (dt=1h here)
    grid_energy_drawn = -charge_tick.arbitrage_revenue_dkk / 1.0  # price was 1.0 DKK/MWh
    assert grid_energy_drawn <= config.power_mw * 1.0 + 1e-9


def test_no_ticks_when_no_day_ahead_data_in_window():
    db = _db_with_series([])
    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), BessConfig())
    assert result.ticks == []
    assert result.total_revenue_dkk == 0.0
    assert result.full_cycle_equivalents == 0.0


def test_null_day_ahead_values_are_dropped_not_crashed_on():
    rows = _price_rows([100.0, None, 100.0, None, 1.0, 100.0, 100.0])
    db = _db_with_series(rows)
    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=10), BessConfig())
    # 2 nulls dropped -> 5 ticks remain
    assert len(result.ticks) == 5


# --- run_backtest: capacity reservation revenue --------------------------------


def test_capacity_revenue_uses_committed_mw_and_clearing_price():
    day_ahead = _price_rows([100.0] * 20)  # flat -> arbitrage always idle
    fcr = _price_rows([50.0], start=BASE_TIME)
    afrr = _price_rows([30.0], start=BASE_TIME)
    db = _db_with_series(day_ahead, fcr=fcr, afrr=afrr)
    config = BessConfig(
        capacity_commit_mw=0.4, capacity_markets=(("FCR", "price"), ("aFRR_capacity", "up"))
    )

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=20), config)

    first_tick = result.ticks[0]
    # commit split evenly across 2 market groups: 0.2 MW each, 1-hour tick.
    assert first_tick.capacity_revenue_by_market["FCR:price"] == pytest.approx(50.0 * 0.2 * 1.0)
    assert first_tick.capacity_revenue_by_market["aFRR_capacity:up"] == pytest.approx(
        30.0 * 0.2 * 1.0
    )
    assert first_tick.capacity_reserved_mw == pytest.approx(0.4)
    assert result.total_capacity_revenue_dkk > 0


def test_capacity_revenue_zero_when_no_capacity_price_available():
    day_ahead = _price_rows([100.0] * 5)
    db = _db_with_series(day_ahead, fcr=[], afrr=[])
    config = BessConfig(capacity_commit_mw=0.3)

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    assert result.total_capacity_revenue_dkk == 0.0
    assert all(t.capacity_revenue_dkk == 0.0 for t in result.ticks)


def test_no_capacity_commit_means_no_capacity_revenue_even_with_prices():
    day_ahead = _price_rows([100.0] * 5)
    fcr = _price_rows([50.0])
    db = _db_with_series(day_ahead, fcr=fcr)
    config = BessConfig(capacity_commit_mw=0.0)

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    assert result.total_capacity_revenue_dkk == 0.0


def test_capacity_commit_reduces_power_available_for_arbitrage():
    values = [98.0, 102.0, 99.0, 101.0, 100.0, 100.0] + [1.0]
    db = _db_with_series(_price_rows(values), fcr=[], afrr=[])
    full_power_config = BessConfig(
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.1,
        power_mw=1.0,
        capacity_mwh=10.0,
        capacity_commit_mw=0.0,
        capacity_markets=(),
    )
    reduced_power_config = BessConfig(
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.1,
        power_mw=1.0,
        capacity_mwh=10.0,
        capacity_commit_mw=0.6,
        capacity_markets=(("FCR", "price"),),
    )

    full = run_backtest(
        db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), full_power_config
    )
    reduced = run_backtest(
        db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), reduced_power_config
    )

    full_energy = -full.ticks[-1].arbitrage_revenue_dkk
    reduced_energy = -reduced.ticks[-1].arbitrage_revenue_dkk
    assert reduced_energy < full_energy


# --- excluded markets ----------------------------------------------------------


def test_mfrr_markets_never_queried():
    db = _db_with_series(_price_rows([100.0] * 5))
    run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), BessConfig())
    queried_markets = {call.args[0] for call in db.fetch_series_values.call_args_list}
    assert "mFRR_capacity" not in queried_markets
    assert "mFRR_EAM" not in queried_markets


# --- max_cycles_per_day cycle cap -----------------------------------------------
#
# These tests force every tick to *want* to discharge, by monkeypatching
# `_causal_zscore` to always return a large positive value -- this isolates
# the cycle-cap enforcement mechanism itself (the thing under test) from the
# arbitrage strategy's z-score baseline dynamics (already covered by the
# tests above), which would otherwise make a long run of identical/near-
# identical high prices eventually collapse to zero variance (z=None,
# action=idle) purely as an artifact of the rolling lookback window, not
# anything to do with the cycle cap.


def test_max_cycles_per_day_rejects_non_positive():
    with pytest.raises(ValueError, match="max_cycles_per_day"):
        BessConfig(max_cycles_per_day=0)
    with pytest.raises(ValueError, match="max_cycles_per_day"):
        BessConfig(max_cycles_per_day=-1.0)


def test_max_cycles_per_day_none_is_unconstrained():
    BessConfig(max_cycles_per_day=None)  # must not raise


def _force_always_discharge(monkeypatch):
    """Forces `_causal_zscore` to always return a large positive value, so
    the arbitrage strategy always attempts a discharge regardless of the
    actual price series content."""
    monkeypatch.setattr("shared.bess_simulator._causal_zscore", lambda history, current: 10.0)


def test_cycle_cap_not_binding_when_max_cycles_per_day_is_none(monkeypatch):
    _force_always_discharge(monkeypatch)
    rows = _price_rows([100.0] * 30)
    db = _db_with_series(rows)
    config = BessConfig(
        power_mw=10.0,
        capacity_mwh=100000.0,  # SoC never remotely close to limiting
        capacity_commit_mw=0.0,
        capacity_markets=(),
        max_cycles_per_day=None,
        soc_min_fraction=0.0,
        soc_max_fraction=1.0,
        starting_soc_fraction=1.0,
    )

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=30), config)

    assert all(t.action == "discharge" for t in result.ticks)
    assert not any(t.cycle_cap_binding for t in result.ticks)


def test_cycle_cap_binds_when_repeated_discharges_exceed_it(monkeypatch):
    _force_always_discharge(monkeypatch)
    rows = _price_rows([100.0] * 10)
    db = _db_with_series(rows)
    max_cycles_per_day = 0.5  # 100000 * 0.5 = 50,000 MWh cap per rolling 24h
    config = BessConfig(
        power_mw=10000.0,  # each tick alone could exceed the cap
        capacity_mwh=100000.0,
        capacity_commit_mw=0.0,
        capacity_markets=(),
        max_cycles_per_day=max_cycles_per_day,
        soc_min_fraction=0.0,
        soc_max_fraction=1.0,
        starting_soc_fraction=1.0,
    )
    cap_mwh = config.capacity_mwh * max_cycles_per_day

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=10), config)

    assert any(t.cycle_cap_binding for t in result.ticks)
    # The rolling 24h window here spans the entire (10-tick) run, so total
    # discharge across the whole run must never exceed the cap.
    assert result.total_discharged_mwh <= cap_mwh + 1e-6


def test_cycle_cap_is_a_rolling_24h_window_not_a_calendar_day_reset(monkeypatch):
    _force_always_discharge(monkeypatch)
    # 27 hourly ticks: enough to fill the cap on tick 0, stay fully capped
    # through tick 23 (< 24h since tick 0), and free back up once tick 0's
    # discharge ages out of the rolling 24h window.
    rows = _price_rows([100.0] * 27)
    db = _db_with_series(rows)
    max_cycles_per_day = 0.05  # 100000 * 0.05 = 5,000 MWh cap per rolling 24h
    config = BessConfig(
        power_mw=10000.0,  # one tick's max discharge (10,000 MWh) exceeds the 5,000 MWh cap
        capacity_mwh=100000.0,  # SoC never remotely close to limiting
        capacity_commit_mw=0.0,
        capacity_markets=(),
        max_cycles_per_day=max_cycles_per_day,
        soc_min_fraction=0.0,
        soc_max_fraction=1.0,
        starting_soc_fraction=1.0,
    )
    cap_mwh = config.capacity_mwh * max_cycles_per_day

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=27), config)

    first_tick = result.ticks[0]
    assert first_tick.energy_discharged_mwh == pytest.approx(cap_mwh)
    assert first_tick.cycle_cap_binding is True  # attempted far more than the cap allowed

    # Every tick strictly less than 24h after the first discharge is fully
    # capped to zero -- the whole cap was already consumed by tick 0, and a
    # calendar-day reset would incorrectly free it up at the next midnight
    # (well before the 24h mark) instead.
    within_24h = [t for t in result.ticks[1:] if t.time < first_tick.time + timedelta(hours=24)]
    assert within_24h  # sanity: the window actually covers >1 subsequent tick
    assert all(t.energy_discharged_mwh == pytest.approx(0.0, abs=1e-9) for t in within_24h)

    # Once a tick is >= 24h after the first discharge, that first discharge
    # has aged out of the rolling window and the cap is available again.
    at_or_after_24h = [t for t in result.ticks if t.time >= first_tick.time + timedelta(hours=24)]
    assert at_or_after_24h  # sanity: the run extends past the 24h mark
    assert any(t.energy_discharged_mwh > 0 for t in at_or_after_24h)


# --- FCR-D (DK2) capacity-market keying/commit-split ----------------------


def test_afrr_activation_participation_rate_rejects_outside_unit_interval():
    with pytest.raises(ValueError, match="afrr_activation_participation_rate"):
        BessConfig(afrr_activation_participation_rate=-0.1)
    with pytest.raises(ValueError, match="afrr_activation_participation_rate"):
        BessConfig(afrr_activation_participation_rate=1.1)


def test_fcr_up_down_legs_do_not_collide_in_capacity_revenue_by_market():
    day_ahead = _price_rows([100.0] * 5)
    fcr_up = _price_rows([40.0])
    fcr_down = _price_rows([25.0])
    db = _db_with_series(day_ahead, fcr_up=fcr_up, fcr_down=fcr_down)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FCR", "up"), ("FCR", "down")),
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    first_tick = result.ticks[0]
    # Both legs share market="FCR" but distinct products -- must not collide.
    assert first_tick.capacity_revenue_by_market["FCR:up"] == pytest.approx(40.0 * 0.2 * 1.0)
    assert first_tick.capacity_revenue_by_market["FCR:down"] == pytest.approx(25.0 * 0.2 * 1.0)
    assert (
        first_tick.capacity_revenue_by_market["FCR:up"]
        != first_tick.capacity_revenue_by_market["FCR:down"]
    )


def test_commit_splits_across_market_groups_not_raw_leg_entries():
    # Regression test for the dilution bug: adding a second FCR-D leg to the
    # "FCR" group must NOT shrink aFRR_capacity's share. With 2 groups (FCR,
    # aFRR_capacity) each should get commit/2 regardless of how many legs
    # the FCR group has.
    day_ahead = _price_rows([100.0] * 5)
    fcr = _price_rows([50.0])
    fcr_up = _price_rows([40.0])
    fcr_down = _price_rows([25.0])
    afrr = _price_rows([30.0])
    db = _db_with_series(day_ahead, fcr=fcr, fcr_up=fcr_up, fcr_down=fcr_down, afrr=afrr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(
            ("FCR", "price"),
            ("FCR", "up"),
            ("FCR", "down"),
            ("aFRR_capacity", "up"),
        ),
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=5), config)
    first_tick = result.ticks[0]

    # 2 groups -> 0.2 MW/group. FCR group has 3 legs -> 0.2/3 MW each.
    commit_per_fcr_leg = 0.4 / 2 / 3
    assert first_tick.capacity_revenue_by_market["FCR:price"] == pytest.approx(
        50.0 * commit_per_fcr_leg * 1.0
    )
    assert first_tick.capacity_revenue_by_market["FCR:up"] == pytest.approx(
        40.0 * commit_per_fcr_leg * 1.0
    )
    assert first_tick.capacity_revenue_by_market["FCR:down"] == pytest.approx(
        25.0 * commit_per_fcr_leg * 1.0
    )
    # aFRR_capacity's group-level share (0.2 MW) is untouched by FCR having 3 legs.
    assert first_tick.capacity_revenue_by_market["aFRR_capacity:up"] == pytest.approx(
        30.0 * 0.2 * 1.0
    )
    assert first_tick.capacity_reserved_mw == pytest.approx(0.4)


# --- aFRR energy activation revenue ----------------------------------------


def test_activation_revenue_formula():
    day_ahead = _price_rows([100.0] * 3)
    afrr = _price_rows([30.0])
    activation = _price_rows([20.0])  # activation_price EUR/MWh
    db = _db_with_series(day_ahead, afrr=afrr, activation=activation)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("aFRR_capacity", "up"),),
        afrr_activation_participation_rate=0.5,
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    first_tick = result.ticks[0]
    # commit_per_group = 0.4 (only one group). revenue = price * commit * rate * dt_hours
    expected = 20.0 * 0.4 * 0.5 * 1.0
    assert first_tick.afrr_activation_revenue_eur == pytest.approx(expected)
    assert result.total_afrr_activation_revenue_eur > 0.0


def test_activation_revenue_zero_when_rate_is_zero():
    day_ahead = _price_rows([100.0] * 3)
    afrr = _price_rows([30.0])
    activation = _price_rows([20.0])
    db = _db_with_series(day_ahead, afrr=afrr, activation=activation)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("aFRR_capacity", "up"),),
        afrr_activation_participation_rate=0.0,
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    assert result.total_afrr_activation_revenue_eur == 0.0
    assert all(t.afrr_activation_revenue_eur == 0.0 for t in result.ticks)


def test_activation_revenue_zero_when_no_afrr_capacity_committed():
    day_ahead = _price_rows([100.0] * 3)
    activation = _price_rows([20.0])
    db = _db_with_series(day_ahead, activation=activation)
    config = BessConfig(capacity_commit_mw=0.0, capacity_markets=())

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    assert result.total_afrr_activation_revenue_eur == 0.0
    # aFRR_energy must never even be queried when aFRR_capacity isn't configured.
    queried_markets = {call.args[0] for call in db.fetch_series_values.call_args_list}
    assert "aFRR_energy" not in queried_markets


def test_activation_revenue_zero_when_no_activation_price_available():
    day_ahead = _price_rows([100.0] * 3)
    afrr = _price_rows([30.0])
    db = _db_with_series(day_ahead, afrr=afrr, activation=[])
    config = BessConfig(capacity_commit_mw=0.4, capacity_markets=(("aFRR_capacity", "up"),))

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    assert result.total_afrr_activation_revenue_eur == 0.0
    assert all(t.afrr_activation_revenue_eur == 0.0 for t in result.ticks)


def test_afrr_activation_revenue_never_appears_in_total_revenue_dkk():
    day_ahead = _price_rows([100.0] * 3)
    afrr = _price_rows([30.0])
    activation = _price_rows([9999.0])  # deliberately huge, to catch accidental mixing
    db = _db_with_series(day_ahead, afrr=afrr, activation=activation)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("aFRR_capacity", "up"),),
        afrr_activation_participation_rate=1.0,
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    assert result.total_afrr_activation_revenue_eur > 1000.0  # sanity: not accidentally zero
    # total_revenue_dkk must equal arbitrage + capacity only, never plus the (huge) EUR figure.
    assert result.total_revenue_dkk == pytest.approx(
        result.total_arbitrage_revenue_dkk + result.total_capacity_revenue_dkk
    )


# --- per-currency capacity buckets (Stage 1 correctness fix) -----------------
#
# The regression test that should have existed: a DK2-shaped config mixing
# EUR FCR (DK2's FCR price/FCR-D legs) and DKK aFRR_capacity must never sum
# them into one number -- this is the exact live defect described in
# shared/bess_simulator.py's module docstring §2.


def test_dk2_mixed_currency_capacity_buckets_never_summed():
    day_ahead = _price_rows([100.0] * 5)
    fcr = _price_rows([50.0])  # ("FCR", "price") in DK2 -> EUR/MW/h (shared/units.py)
    afrr = _price_rows([30.0])  # ("aFRR_capacity", "up") in DK2 -> DKK/MW/h
    db = _db_with_series(day_ahead, fcr=fcr, afrr=afrr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FCR", "price"), ("aFRR_capacity", "up")),
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    first_tick = result.ticks[0]
    # commit split evenly across 2 groups: 0.2 MW each, 1-hour tick.
    expected_eur_leg = 50.0 * 0.2 * 1.0
    expected_dkk_leg = 30.0 * 0.2 * 1.0

    # capacity_revenue_dkk (and its cumulative) holds ONLY the DKK (aFRR)
    # contribution -- the EUR (FCR) leg must never appear in it.
    assert first_tick.capacity_revenue_dkk == pytest.approx(expected_dkk_leg)
    assert result.total_capacity_revenue_dkk == pytest.approx(expected_dkk_leg * 5)

    # capacity_revenue_eur (and its cumulative) holds ONLY the EUR (FCR)
    # contribution.
    assert first_tick.capacity_revenue_eur == pytest.approx(expected_eur_leg)
    assert result.total_capacity_revenue_eur == pytest.approx(expected_eur_leg * 5)

    # cumulative_total_revenue_dkk (arbitrage + capacity) must exclude the
    # EUR leg entirely.
    assert result.total_revenue_dkk == pytest.approx(
        result.total_arbitrage_revenue_dkk + result.total_capacity_revenue_dkk
    )
    # sanity: the EUR figure is a real, distinct, nonzero number -- not
    # silently zeroed out or folded into the DKK total above.
    assert result.total_capacity_revenue_eur > 0.0
    assert result.total_capacity_revenue_eur != pytest.approx(result.total_capacity_revenue_dkk)

    assert result.currencies_present == frozenset({"DKK", "EUR"})


def test_dk1_backtest_capacity_revenue_numerically_unchanged():
    """
    DK1 is all-DKK (fcr_dk1 and afrr_reserves_nordic are both DKK/MW/h) --
    Stage 1's per-currency bucketing must not move a single DK1 number. This
    is the proof that Stage 1 only moved what it should (DK2's mixed-
    currency figures), never DK1's.
    """
    day_ahead = _price_rows([100.0] * 5)
    fcr = _price_rows([50.0])
    afrr = _price_rows([30.0])
    db = _db_with_series(day_ahead, fcr=fcr, afrr=afrr)
    config = BessConfig(
        capacity_commit_mw=0.4, capacity_markets=(("FCR", "price"), ("aFRR_capacity", "up"))
    )

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    # Hand-computed expected total: 2 groups, 0.2 MW/leg, 1h ticks, 5 ticks.
    expected_capacity_revenue_dkk = (50.0 * 0.2 + 30.0 * 0.2) * 1.0 * 5
    assert result.total_capacity_revenue_dkk == pytest.approx(expected_capacity_revenue_dkk)
    assert result.total_capacity_revenue_eur == 0.0
    assert result.currencies_present == frozenset({"DKK"})
    assert result.total_revenue_dkk == pytest.approx(
        result.total_arbitrage_revenue_dkk + result.total_capacity_revenue_dkk
    )


def test_currencies_present_empty_when_no_capacity_revenue():
    day_ahead = _price_rows([100.0] * 5)
    db = _db_with_series(day_ahead, fcr=[], afrr=[])
    config = BessConfig(capacity_commit_mw=0.3)

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    assert result.currencies_present == frozenset()


def test_unlabelled_capacity_leg_raises_value_error(monkeypatch):
    """
    A leg whose currency can't be resolved (a registry gap, not a "no data"
    case) must fail loud, not silently default to 0/None -- a silently
    unlabelled leg is exactly how the DKK/EUR summing defect arose.
    """
    monkeypatch.setattr("shared.bess_simulator.currency_for", lambda market, zone, product: None)
    day_ahead = _price_rows([100.0] * 3)
    fcr = _price_rows([50.0])
    db = _db_with_series(day_ahead, fcr=fcr)
    config = BessConfig(capacity_commit_mw=0.3, capacity_markets=(("FCR", "price"),))

    with pytest.raises(ValueError, match="no unit declared"):
        run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)


# --- Stage 4.1: "price_ranked" capacity allocation ---------------------------


def test_capacity_allocation_defaults_to_even():
    assert BessConfig().capacity_allocation == "even"


def test_capacity_allocation_rejects_invalid_value():
    with pytest.raises(ValueError, match="capacity_allocation"):
        BessConfig(capacity_allocation="best_effort")


def test_price_ranked_gives_zero_price_group_near_full_share_to_its_sibling():
    """
    The core allocation fix: with a zero-trailing-price FFR group alongside
    a steadily-priced aFRR_capacity group, once FFR's trailing history is
    established (all zeros), aFRR should get essentially its *entire*
    group's weight -- FFR's near-0 share is redistributed to its sibling,
    never lost to arbitrage or left diluting aFRR's share the way "even"
    would (see BessConfig.capacity_allocation's docstring).
    """
    day_ahead = _price_rows([100.0] * 10)
    afrr = _price_rows([10.0] * 10)  # flat, nonzero
    ffr = _price_rows([0.0] * 10)  # flat zero -- FFR clears at 0 today, live
    db = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
        arbitrage_lookback_periods=5,
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=10), config)

    # By the last tick, FFR's whole trailing window is zeros -- aFRR should
    # be getting essentially the full 0.4 MW commit (weight -> 1.0), not the
    # 0.2 MW an even split would give it.
    last_tick = result.ticks[-1]
    assert last_tick.capacity_revenue_by_market["aFRR_capacity:up"] == pytest.approx(
        10.0 * 0.4 * 1.0, rel=1e-6
    )
    assert last_tick.capacity_revenue_by_market["FFR:price"] == pytest.approx(0.0)


def test_price_ranked_does_not_reduce_arbitrage_revenue_vs_no_ffr_run():
    """
    The coordinator's key test: including a zero-price FFR group under
    "price_ranked" must not reduce total_arbitrage_revenue_dkk versus an
    otherwise-identical run that never configured FFR at all --
    arbitrage_power_mw depends only on capacity_commit_mw (never on
    capacity_markets' composition or allocation mode), so this should hold
    exactly, not just approximately/directionally.
    """
    values = [98.0, 102.0, 99.0, 101.0, 100.0, 100.0] + [1.0] * 5 + [500.0] * 5
    day_ahead = _price_rows(values)
    afrr = _price_rows([10.0] * len(values))
    ffr = _price_rows([0.0] * len(values))
    common_kwargs = dict(
        capacity_commit_mw=0.4,
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.1,
    )

    db_with_ffr = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    with_ffr_config = BessConfig(
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
        **common_kwargs,
    )
    with_ffr = run_backtest(
        db_with_ffr, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), with_ffr_config
    )

    db_no_ffr = _db_with_series(day_ahead, afrr=afrr)
    no_ffr_config = BessConfig(
        capacity_markets=(("aFRR_capacity", "up"),),
        **common_kwargs,
    )
    no_ffr = run_backtest(
        db_no_ffr, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), no_ffr_config
    )

    assert with_ffr.total_arbitrage_revenue_dkk == pytest.approx(no_ffr.total_arbitrage_revenue_dkk)
    # Sanity: this isn't trivially true because both runs earned nothing --
    # confirm real arbitrage activity happened in both.
    assert with_ffr.total_arbitrage_revenue_dkk != 0.0


def test_price_ranked_avoids_capacity_revenue_dilution_from_zero_price_group():
    """
    The actual bug being fixed: under "even", adding a zero-price FFR group
    dilutes aFRR_capacity's share (1/2 -> 1/3, in a 2- vs 3-group stack),
    reducing *total* capacity revenue even though FFR itself contributes
    nothing. Under "price_ranked", once FFR's trailing history reads 0,
    aFRR's capacity revenue should recover to (approximately) what it earns
    in a run that never configured FFR at all -- not stay diluted the way
    "even" would leave it.
    """
    day_ahead = _price_rows([100.0] * 10)
    afrr = _price_rows([10.0] * 10)
    ffr = _price_rows([0.0] * 10)

    db_even = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    even_config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="even",
    )
    even_result = run_backtest(
        db_even, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=10), even_config
    )

    db_ranked = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    ranked_config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
        arbitrage_lookback_periods=5,
    )
    ranked_result = run_backtest(
        db_ranked, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=10), ranked_config
    )

    # "even" dilutes aFRR to a 0.2 MW share (half of 0.4) the whole time,
    # since it never looks at price. "price_ranked" should earn strictly
    # more total DKK capacity revenue once FFR's trailing-zero history
    # kicks in (aFRR's share grows toward the full 0.4 MW).
    assert ranked_result.total_capacity_revenue_dkk > even_result.total_capacity_revenue_dkk


def test_price_ranked_falls_back_to_even_on_first_tick_with_no_trailing_history():
    """
    Every leg's trailing deque starts empty -- causally, there is no price
    signal to rank by yet on the very first tick(s) of any run, so
    price_ranked must fall back to an even split there (and report it).
    """
    day_ahead = _price_rows([100.0] * 3)
    afrr = _price_rows([10.0] * 3)
    ffr = _price_rows([5.0] * 3)
    db = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    first_tick = result.ticks[0]
    # Even split: 0.4 / 2 groups = 0.2 MW each.
    assert first_tick.capacity_revenue_by_market["aFRR_capacity:up"] == pytest.approx(
        10.0 * 0.2 * 1.0
    )
    assert first_tick.capacity_revenue_by_market["FFR:price"] == pytest.approx(5.0 * 0.2 * 1.0)
    assert result.capacity_allocation_fell_back_to_even is True


def test_price_ranked_falls_back_to_even_when_every_group_stays_at_zero():
    day_ahead = _price_rows([100.0] * 5)
    afrr = _price_rows([0.0] * 5)
    ffr = _price_rows([0.0] * 5)
    db = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    assert result.capacity_allocation_fell_back_to_even is True
    # Every tick's revenue is 0 either way (both prices are 0), but the
    # split itself should still be even (0.2 MW each) throughout, not
    # collapsed/undefined.
    for tick in result.ticks:
        assert tick.capacity_reserved_mw == pytest.approx(0.4)


def test_even_allocation_never_sets_fell_back_flag():
    day_ahead = _price_rows([100.0] * 5)
    afrr = _price_rows([0.0] * 5)
    db = _db_with_series(day_ahead, afrr=afrr)
    config = BessConfig(capacity_commit_mw=0.3, capacity_allocation="even")

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config)

    assert result.capacity_allocation_fell_back_to_even is False


def test_price_ranked_allocation_is_causal_no_lookahead():
    """
    The property most likely to be silently broken: a leg's *own* clearing
    price this tick must never influence *this* tick's allocation weight --
    only ticks strictly before it may. Construct a group (FFR) that clears
    at 0 for several ticks, then jumps to a huge price -- if price_ranked
    looked ahead, aFRR's share would collapse the instant FFR's price
    jumps; if it's correctly causal, aFRR keeps its full share for that one
    tick (FFR's trailing history is still all zeros) and only loses share
    starting the *next* tick, once FFR's jump has actually entered its
    trailing window.
    """
    n_flat = 6
    values = [0.0] * n_flat + [1000.0] * 3
    day_ahead = _price_rows([100.0] * len(values))
    afrr = _price_rows([10.0] * len(values))
    ffr = _price_rows(values)
    db = _db_with_series(day_ahead, afrr=afrr, ffr=ffr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FFR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
        arbitrage_lookback_periods=5,
    )

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config)

    jump_tick = result.ticks[n_flat]  # first tick where FFR's OWN price is 1000
    next_tick = result.ticks[n_flat + 1]  # one tick later -- the jump is now in history

    # At the jump tick itself: FFR's trailing history is still all zeros
    # (its own new price hasn't been appended yet) -- aFRR must still get
    # its full, undiluted 0.4 MW share.
    assert jump_tick.capacity_revenue_by_market["aFRR_capacity:up"] == pytest.approx(
        10.0 * 0.4 * 1.0, rel=1e-6
    )

    # One tick later, FFR's jump has entered its trailing window -- aFRR's
    # share must now be measurably smaller than the jump tick's (proving
    # the jump price *did* eventually get used, just not a tick early).
    assert (
        next_tick.capacity_revenue_by_market["aFRR_capacity:up"]
        < jump_tick.capacity_revenue_by_market["aFRR_capacity:up"]
    )


def test_price_ranked_ranks_on_relative_strength_not_raw_magnitude():
    """
    Regression test for the exact bug the coordinator found: an earlier
    version of `_group_commit_shares` ranked groups by raw trailing price,
    which silently compared magnitudes across currencies (DK2's
    aFRR_capacity, ~tens of DKK, vs. FCR, ~single-digit EUR) -- a DKK leg
    would out-rank a EUR leg purely because DKK numbers are bigger, the
    same unit-mixing bug class shared/units.py exists to catch elsewhere.

    Here, FCR (EUR) moves from a baseline of 1.0 to a recent trailing of
    2.0 (2x its own history); aFRR_capacity (DKK) moves from a baseline of
    10.0 to a recent trailing of 20.0 (also exactly 2x its own history,
    despite being 10x FCR's raw magnitude throughout). At the same
    *relative* strength, the two groups must be allocated approximately
    equal shares of capacity_commit_mw -- never the ~10x-skewed split the
    old raw-magnitude ranking would have produced.

    Numbers are exact and hand-computed (not just "roughly equal"): with
    arbitrage_lookback_periods=5 and PRICE_RANKED_BASELINE_MULTIPLIER=4
    (baseline window=20), 20 baseline ticks followed by 5 elevated ticks,
    evaluated at the very last tick (index 24): the short window (last 5
    of the 24 prior ticks) holds 1 baseline + 4 elevated values, and the
    baseline window (last 20 of the 24 prior ticks) holds 16 baseline + 4
    elevated values -- both FCR and aFRR see the exact same 1:4 and 16:4
    mixes (just scaled by a constant factor), so their ratios are
    identical: (1*1 + 4*2) / 5 = 1.8 over (16*1 + 4*2) / 20 = 1.2 -> 1.5,
    for both legs, regardless of the 10x raw-magnitude gap between them.
    """
    n_baseline = 20
    n_elevated = 5
    fcr_values = [1.0] * n_baseline + [2.0] * n_elevated
    afrr_values = [10.0] * n_baseline + [20.0] * n_elevated  # same relative move, 10x the magnitude
    day_ahead = _price_rows([100.0] * len(fcr_values))
    fcr = _price_rows(fcr_values)
    afrr = _price_rows(afrr_values)
    db = _db_with_series(day_ahead, fcr=fcr, afrr=afrr)
    config = BessConfig(
        capacity_commit_mw=0.4,
        capacity_markets=(("FCR", "price"), ("aFRR_capacity", "up")),
        capacity_allocation="price_ranked",
        arbitrage_lookback_periods=5,
    )

    result = run_backtest(
        db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=len(fcr_values)), config
    )

    last_tick = result.ticks[-1]
    fcr_revenue = last_tick.capacity_revenue_by_market["FCR:price"]
    afrr_revenue = last_tick.capacity_revenue_by_market["aFRR_capacity:up"]

    # Hand-computed: both ratios are exactly 1.5 -> equal weights -> each
    # leg's commit is 0.2 MW. Revenue = price * commit * dt_hours (dt=1h).
    expected_commit_mw = 0.2
    assert fcr_revenue == pytest.approx(2.0 * expected_commit_mw * 1.0, rel=1e-6)
    assert afrr_revenue == pytest.approx(20.0 * expected_commit_mw * 1.0, rel=1e-6)

    # The property under test, stated directly: equal relative strength ->
    # (approximately) equal MW commit, regardless of the 10x raw-price gap.
    # Revenue = price * commit, so commit_ratio = revenue_ratio / price_ratio.
    fcr_commit = fcr_revenue / 2.0
    afrr_commit = afrr_revenue / 20.0
    assert fcr_commit == pytest.approx(afrr_commit, rel=1e-6)
    assert fcr_commit == pytest.approx(expected_commit_mw, rel=1e-6)

    # Sanity against the bug this test guards against: the OLD raw-magnitude
    # ranking would have weighted aFRR (trailing ~18 DKK) roughly 10x FCR's
    # (trailing ~1.8 EUR) share purely from the raw numbers -- i.e. an MW
    # commit ratio near 10, not near 1. Assert the actual commit ratio is
    # nowhere close to that old, wrong shape.
    assert afrr_commit / fcr_commit == pytest.approx(1.0, rel=1e-3)


def test_price_ranked_leg_relative_strength_is_zero_when_own_baseline_is_zero():
    """
    A leg whose own longer-run baseline is itself 0 (FFR's real situation
    today) must rank at 0 relative strength -- never a ZeroDivisionError,
    and never treated as "infinitely strong" just because its trailing
    price is nonzero while its baseline briefly reads 0 momentum from a
    handful of leading zeros ageing out."""
    zero_baseline: deque[float] = deque([0.0, 0.0, 0.0], maxlen=20)
    nonzero_short: deque[float] = deque([5.0, 5.0], maxlen=5)
    assert _leg_relative_strength(nonzero_short, zero_baseline) == 0.0

    empty: deque[float] = deque(maxlen=5)
    assert _leg_relative_strength(empty, zero_baseline) == 0.0
    assert _leg_relative_strength(nonzero_short, empty) == 0.0


def test_leg_relative_strength_ratio_to_own_baseline():
    short = deque([2.0, 2.0], maxlen=5)
    baseline = deque([1.0, 1.0, 1.0, 1.0], maxlen=20)
    assert _leg_relative_strength(short, baseline) == pytest.approx(2.0)


def test_zero_price_periods_by_leg_counts_only_real_zero_prices():
    """
    Distinguishes a real, present price of 0 (counted) from a period with
    no price data at all (never counted -- capacity_revenue_by_market
    already treats that as a separate "no data" case)."""
    day_ahead = _price_rows([100.0] * 6)
    # 0.0, 0.0, 5.0, None (dropped before reaching run_backtest -- see
    # _fetch_series), 0.0, 5.0 -- but _price_rows(None) entries are dropped
    # by _fetch_series's null-filtering, so only real values remain in the
    # series; carrying forward via _value_at_or_before means the two
    # "missing" ticks still see the last *known* value, not None -- so to
    # exercise a genuine "no data yet" tick, start the FFR series later
    # than day_ahead's window.
    ffr = _price_rows([0.0, 0.0, 5.0, 0.0], start=BASE_TIME + timedelta(hours=2))
    db = _db_with_series(day_ahead, ffr=ffr)
    config = BessConfig(capacity_commit_mw=0.2, capacity_markets=(("FFR", "price"),))

    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=6), config)

    # Ticks 0-1 have no FFR price yet (series starts at hour 2) -- not
    # counted as zero-price periods, just "no data".
    assert result.ticks[0].capacity_revenue_by_market["FFR:price"] == 0.0
    assert result.ticks[1].capacity_revenue_by_market["FFR:price"] == 0.0
    # Ticks 2,3,5 (carrying forward tick 5's value from tick 4=0.0) clear at
    # a real 0.0; tick 4 clears at a real 5.0.
    assert result.zero_price_periods_by_leg == {"FFR:price": 3}


def test_zero_price_periods_by_leg_empty_when_no_capacity_markets():
    day_ahead = _price_rows([100.0] * 3)
    db = _db_with_series(day_ahead)
    config = BessConfig(capacity_commit_mw=0.0, capacity_markets=())

    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=3), config)

    assert result.zero_price_periods_by_leg == {}


def test_dk1_backtest_unaffected_by_capacity_allocation_field_existing():
    """
    Definition-of-done regression check: DK1's default ("even") behaviour
    must be bit-for-bit unchanged now that capacity_allocation exists --
    the two runs below (one passing the field explicitly, one relying on
    the default) must produce identical results.
    """
    day_ahead = _price_rows([100.0] * 5)
    fcr = _price_rows([50.0] * 5)
    afrr = _price_rows([30.0] * 5)
    db_a = _db_with_series(day_ahead, fcr=fcr, afrr=afrr)
    db_b = _db_with_series(day_ahead, fcr=fcr, afrr=afrr)

    config_default = BessConfig(capacity_commit_mw=0.4)
    config_explicit = BessConfig(capacity_commit_mw=0.4, capacity_allocation="even")

    result_default = run_backtest(
        db_a, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config_default
    )
    result_explicit = run_backtest(
        db_b, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config_explicit
    )

    assert result_default.total_capacity_revenue_dkk == pytest.approx(
        result_explicit.total_capacity_revenue_dkk
    )
    assert result_default.total_revenue_dkk == pytest.approx(result_explicit.total_revenue_dkk)
