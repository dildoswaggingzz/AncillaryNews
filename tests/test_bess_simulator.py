from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shared.bess_simulator import (
    BessConfig,
    _causal_zscore,
    _value_at_or_before,
    run_backtest,
)

BASE_TIME = datetime(2026, 7, 16, tzinfo=UTC)


def _price_rows(
    values: list[float | None], start: datetime = BASE_TIME, hours: float = 1.0
) -> list[dict]:
    return [{"time": start + timedelta(hours=i * hours), "value": v} for i, v in enumerate(values)]


def _db_with_series(
    day_ahead: list[dict], fcr: list[dict] | None = None, afrr: list[dict] | None = None
):
    """Builds a MagicMock DatabaseManager whose fetch_series_values returns the given series
    per market, matching shared.db_manager.DatabaseManager.fetch_series_values's signature."""
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        if market == "day_ahead":
            return day_ahead
        if market == "FCR":
            return fcr or []
        if market == "aFRR_capacity":
            return afrr or []
        raise AssertionError(f"unexpected market {market!r} requested")

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
    # commit split evenly across 2 markets: 0.2 MW each, 1-hour tick.
    assert first_tick.capacity_revenue_by_market["FCR"] == pytest.approx(50.0 * 0.2 * 1.0)
    assert first_tick.capacity_revenue_by_market["aFRR_capacity"] == pytest.approx(30.0 * 0.2 * 1.0)
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
