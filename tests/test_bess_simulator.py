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
    day_ahead: list[dict],
    fcr: list[dict] | None = None,
    afrr: list[dict] | None = None,
    fcr_up: list[dict] | None = None,
    fcr_down: list[dict] | None = None,
    activation: list[dict] | None = None,
):
    """Builds a MagicMock DatabaseManager whose fetch_series_values returns the given series
    per (market, product), matching shared.db_manager.DatabaseManager.fetch_series_values's
    signature. `fcr` answers ("FCR", "price"); `fcr_up`/`fcr_down` answer ("FCR", "up")/
    ("FCR", "down") (FCR-D legs); `afrr` answers ("aFRR_capacity", "up"); `activation`
    answers ("aFRR_energy", "activation_price")."""
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
    assert first_tick.capacity_revenue_by_market["FCR:up"] != first_tick.capacity_revenue_by_market[
        "FCR:down"
    ]


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
