from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shared.bess_dispatch_milp import (
    _leg_direction,
    phantom_capacity_revenue,
    solve_cooptimized_dispatch,
)
from shared.bess_simulator import BacktestResult, BessConfig, BessTick, run_backtest
from shared.units import DKK_PER_EUR

BASE_TIME = datetime(2026, 7, 16, tzinfo=UTC)


def _series(values: list[float], start: datetime = BASE_TIME, hours: float = 1.0):
    """Builds a `list[tuple[datetime, float]]` series -- the format
    `solve_cooptimized_dispatch` itself consumes (already fetched/parsed),
    letting these tests call it directly with synthetic data and no DB."""
    return [(start + timedelta(hours=i * hours), v) for i, v in enumerate(values)]


def _price_rows(values: list[float | None], start: datetime = BASE_TIME, hours: float = 1.0):
    """`run_backtest`-facing row format (see `tests/test_bess_simulator.py`'s
    identical helper) -- used by the tests below that exercise `run_backtest`
    end-to-end (via a mocked `DatabaseManager`) to compare the two
    strategies on identical series."""
    return [{"time": start + timedelta(hours=i * hours), "value": v} for i, v in enumerate(values)]


def _db_with_series(
    day_ahead: list[dict],
    fcr: list[dict] | None = None,
    fcr_down: list[dict] | None = None,
    afrr: list[dict] | None = None,
    activation: list[dict] | None = None,
    imbalance: list[dict] | None = None,
):
    db = MagicMock()

    def fetch_series_values(
        market, zone, product, limit=None, time_from=None, time_to=None, history=False
    ):
        if market == "day_ahead":
            return day_ahead
        if market == "FCR" and product == "price":
            return fcr or []
        if market == "FCR" and product == "down":
            return fcr_down or []
        if market == "aFRR_capacity" and product == "up":
            return afrr or []
        if market == "aFRR_energy":
            return activation or []
        if market == "imbalance" and product == "imbalance_price":
            return imbalance or []
        raise AssertionError(f"unexpected market/product {market!r}/{product!r} requested")

    db.fetch_series_values.side_effect = fetch_series_values
    return db


# --- _leg_direction ------------------------------------------------------------


def test_leg_direction_resolves_up_down_and_symmetric():
    assert _leg_direction("aFRR_capacity", "up") == "up"
    assert _leg_direction("FCR", "down") == "down"
    assert _leg_direction("FCR", "price") == "symmetric"


def test_leg_direction_rejects_unrecognized_product():
    with pytest.raises(ValueError, match="cannot resolve"):
        _leg_direction("FCR", "volume")


# --- no-double-selling regression ------------------------------------------------
#
# The threshold engine subtracts only *power* for a capacity commitment,
# never energy/SoC (shared/bess_simulator.py module docstring §0 point 1),
# so it happily books up-capacity revenue in the same tick it discharges the
# battery down to soc_min for arbitrage -- MW it could not actually have
# delivered. This window forces exactly that: a long run of high day-ahead
# prices drains the battery to soc_min via arbitrage while a constant,
# always-available aFRR_capacity price keeps "clearing" every tick
# regardless.


def _double_selling_window():
    noisy_baseline = [98.0, 102.0, 99.0, 101.0, 100.0, 100.0]
    values = noisy_baseline + [500.0] * 6
    day_ahead = _price_rows(values)
    afrr = _price_rows([100.0] * len(values))
    activation = _price_rows([0.0] * len(values))
    return values, day_ahead, afrr, activation


def test_threshold_double_sells_capacity_while_soc_drained():
    """Sanity-checks the defect exists in the threshold engine on this
    window (a precondition for the regression test below actually
    regressing anything): some tick has the battery at soc_min *and* a
    positive aFRR_capacity capacity-revenue booking that same tick."""
    values, day_ahead, afrr, activation = _double_selling_window()
    db = _db_with_series(day_ahead, afrr=afrr, activation=activation)
    config = BessConfig(
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.5,
        capacity_commit_mw=0.3,
        capacity_markets=(("aFRR_capacity", "up"),),
    )
    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config)

    soc_min_fraction = config.soc_min_fraction
    double_sold = [
        t
        for t in result.ticks
        if t.soc_fraction <= soc_min_fraction + 1e-6
        and t.capacity_revenue_by_market.get("aFRR_capacity:up", 0.0) > 0
    ]
    assert double_sold, "expected the threshold engine to double-sell on this window"


def test_cooptimizer_never_double_sells_headroom_is_respected():
    """Same series/config as the sanity check above, `strategy` swapped to
    `"cooptimized"` -- every tick's committed up-capacity MW must respect
    the no-double-selling headroom bound against the *previous* tick's SoC
    (the SoC available before that tick's own charge/discharge), and SoC
    itself must stay within the usable band at every tick."""
    values, day_ahead, afrr, activation = _double_selling_window()
    db = _db_with_series(day_ahead, afrr=afrr, activation=activation)
    config = BessConfig(
        arbitrage_lookback_periods=6,
        arbitrage_z_threshold=0.5,
        capacity_commit_mw=0.3,
        capacity_markets=(("aFRR_capacity", "up"),),
        strategy="cooptimized",
    )
    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config)

    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    eta = config.round_trip_efficiency**0.5
    t_act = config.activation_endurance_hours
    starting_soc = config.starting_soc_fraction * config.capacity_mwh

    prev_soc = starting_soc
    for tick in result.ticks:
        assert soc_min - 1e-6 <= tick.soc_mwh <= soc_max + 1e-6

        committed_mw = tick.capacity_revenue_by_market.get("aFRR_capacity:up", 0.0) / (100.0 * 1.0)
        up_headroom = (prev_soc - soc_min) * eta
        assert committed_mw * t_act <= up_headroom + 1e-6
        prev_soc = tick.soc_mwh


def test_cooptimizer_headroom_holds_within_period_not_just_at_its_start():
    """Regression for a residual within-period double-sell: binding the
    no-double-selling headroom bound only against the *start*-of-period SoC
    lets the LP discharge to `soc_min` for arbitrage in the same period it
    books up-reserve sized off the higher start-of-period SoC -- the
    reserve was never actually deliverable by the time that period's
    committed arbitrage flow has run (the reference rule, "subtract
    committed net position before offering capacity", requires
    deliverability *throughout* the period, not just at its opening
    instant). SoC moves monotonically within a period at constant power, so
    binding the headroom bound at both `soc[t]` and `soc[t+1]` closes this.

    This window forces exactly that: a day-ahead price spike in period 1
    makes discharging to `soc_min` overwhelmingly attractive, while a
    constant, always-available aFRR_capacity up price makes committing
    up-reserve that same period attractive too, with `power_mw` large
    enough that the shared power budget never binds (so nothing else stops
    the LP from doing both at once). Before the fix (headroom bound only
    at `soc[t]`), this test's assertion on the *end*-of-period bound
    failed: period 1 booked ~6.07 MW of up-reserve while ending that same
    period at exactly `soc_min` -- reserve sized off the pre-discharge SoC
    (1.8 MWh) that was no longer backed by any stored energy by the time
    the period's own discharge had run. After the fix, period 1 books 0 MW
    (there is no energy left to back it at end-of-period), which is what
    the assertion below requires.
    """
    price_series = _series([0.0, 5000.0, 0.0])  # spike in period 1 -> full discharge that period
    afrr_series = _series([100.0] * 3)
    config = BessConfig(
        power_mw=100.0,  # power ceiling never binds -- isolates the headroom bug
        capacity_mwh=2.0,
        round_trip_efficiency=0.9,
        soc_min_fraction=0.1,
        soc_max_fraction=0.9,
        starting_soc_fraction=0.5,
        capacity_commit_mw=0.0,
        capacity_markets=(("aFRR_capacity", "up"),),
        max_cycles_per_day=None,
        strategy="cooptimized",
        activation_endurance_hours=0.25,
    )
    result = solve_cooptimized_dispatch(
        "DK1",
        BASE_TIME,
        BASE_TIME + timedelta(hours=3),
        config,
        price_series,
        {"aFRR_capacity:up": afrr_series},
        {"aFRR_capacity:up": "DKK"},
        [],
    )

    soc_min = config.soc_min_fraction * config.capacity_mwh
    eta = config.round_trip_efficiency**0.5
    t_act = config.activation_endurance_hours

    # Sanity: period 1 really is the discharge-to-soc_min tick this test
    # needs to exercise the bug.
    assert result.ticks[1].action == "discharge"
    assert result.ticks[1].soc_mwh == pytest.approx(soc_min, abs=1e-6)

    for tick in result.ticks:
        committed_mw = tick.capacity_revenue_by_market.get("aFRR_capacity:up", 0.0) / (100.0 * 1.0)
        # END-of-period bound: committed reserve must be deliverable out of
        # the SoC that is actually left AFTER this period's own arbitrage
        # flow -- not just out of the SoC the period started with.
        assert committed_mw * t_act <= (tick.soc_mwh - soc_min) * eta + 1e-6


# --- perfect-foresight >= threshold ------------------------------------------


@pytest.mark.parametrize(
    "values, capacity_kwargs",
    [
        # Window A: pure arbitrage, no capacity legs -- the LP is a true
        # optimum over the exact same battery physics the causal z-score
        # heuristic uses, so it can never do worse.
        (
            [98.0, 102.0, 99.0, 101.0, 100.0, 100.0]
            + [1.0]
            + [98.0, 102.0, 99.0, 101.0, 100.0, 100.0]
            + [500.0],
            {"capacity_commit_mw": 0.0, "capacity_markets": ()},
        ),
        # Window B: arbitrage + a steady, always-clearing aFRR_capacity
        # commitment where the battery's SoC never gets anywhere near
        # soc_min/soc_max (ample capacity_mwh) -- no double-selling occurs
        # in the threshold run either, so this is a fair apples-to-apples
        # comparison of the same feasible commitment level.
        (
            [98.0, 102.0, 99.0, 101.0, 100.0, 100.0] * 3,
            {
                "capacity_commit_mw": 0.1,
                "capacity_markets": (("aFRR_capacity", "up"),),
                "capacity_mwh": 10.0,
            },
        ),
    ],
)
def test_perfect_foresight_revenue_at_least_matches_threshold(values, capacity_kwargs):
    afrr = _price_rows([50.0] * len(values)) if capacity_kwargs["capacity_markets"] else None
    activation = _price_rows([0.0] * len(values)) if capacity_kwargs["capacity_markets"] else None
    day_ahead = _price_rows(values)

    totals = {}
    for strategy in ("threshold", "cooptimized"):
        db = _db_with_series(day_ahead, afrr=afrr, activation=activation)
        config = BessConfig(
            arbitrage_lookback_periods=6,
            arbitrage_z_threshold=0.5,
            strategy=strategy,
            **capacity_kwargs,
        )
        result = run_backtest(
            db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config
        )
        totals[strategy] = result.total_revenue_all_dkk

    assert totals["cooptimized"] >= totals["threshold"] - 1e-6


# --- LP vs. brute force ----------------------------------------------------------


def test_lp_matches_exhaustive_search_on_a_tiny_hand_computable_window():
    """3 hourly periods, prices [30, 200, 10], power_mw=1 MW, capacity_mwh=1
    MWh (soc band 0.1-0.9), starting SoC 0.5, round_trip_efficiency=1.0
    (loss-free, so hand-computable): the only rational move is to charge up
    to the full 0.4 MWh of headroom below soc_max in period 0 (cost 30/MWh),
    discharge the resulting 0.8 MWh available down to soc_min in period 1
    (revenue 200/MWh, the expensive tick), and idle in period 2 (soc is
    already at the floor, and there is no period 3 to sell into) --
    net = -30*0.4 + 200*0.8 = 148.0 exactly.

    Cross-checked here against an actual exhaustive grid search over net
    flow per period (fine enough, 0.05 MW steps, to land exactly on the
    hand-computed optimum) -- this is the "LP optimum equals exhaustive
    search" validation gate, not just the hand derivation.
    """
    prices = [30.0, 200.0, 10.0]
    price_series = _series(prices)
    config = BessConfig(
        power_mw=1.0,
        capacity_mwh=1.0,
        round_trip_efficiency=1.0,
        soc_min_fraction=0.1,
        soc_max_fraction=0.9,
        starting_soc_fraction=0.5,
        capacity_commit_mw=0.0,
        capacity_markets=(),
        max_cycles_per_day=None,
        strategy="cooptimized",
    )
    result = solve_cooptimized_dispatch(
        "DK1", BASE_TIME, BASE_TIME + timedelta(hours=3), config, price_series, {}, {}, []
    )

    assert result.total_arbitrage_revenue_dkk == pytest.approx(148.0, abs=1e-6)

    # Exhaustive grid search: net flow f_t per period (positive = discharge,
    # negative = charge), stepped finely enough that +-0.4/+-0.8 (the hand-
    # computed optimum) land exactly on the grid.
    soc_min, soc_max, start = 0.1, 0.9, 0.5
    step = 0.05
    levels = [round(-1.0 + step * i, 10) for i in range(int(2.0 / step) + 1)]
    best_revenue = float("-inf")
    for f0 in levels:
        soc0 = start - f0
        if not (soc_min - 1e-9 <= soc0 <= soc_max + 1e-9):
            continue
        for f1 in levels:
            soc1 = soc0 - f1
            if not (soc_min - 1e-9 <= soc1 <= soc_max + 1e-9):
                continue
            for f2 in levels:
                soc2 = soc1 - f2
                if not (soc_min - 1e-9 <= soc2 <= soc_max + 1e-9):
                    continue
                revenue = prices[0] * f0 + prices[1] * f1 + prices[2] * f2
                if revenue > best_revenue:
                    best_revenue = revenue

    assert best_revenue == pytest.approx(148.0, abs=1e-6)
    assert result.total_arbitrage_revenue_dkk == pytest.approx(best_revenue, abs=1e-3)


# --- symmetric FCR reserves both up AND down headroom simultaneously ------------


def test_symmetric_leg_binds_on_the_tighter_of_up_or_down_headroom():
    """A single symmetric (`product="price"`) leg with a starting SoC close
    to `soc_max` -- down-headroom is deliberately made much tighter than
    up-headroom, so the commitment the LP settles on must be governed by
    the *down* side, proving both sides are enforced (not just up, which a
    bug that only checked one direction would still pass with soc near
    soc_min instead)."""
    price_series = _series([0.0])  # no arbitrage incentive -- isolate the reserve decision
    fcr_series = _series([10_000.0])  # deliberately huge -- LP wants as much as it can get
    config = BessConfig(
        power_mw=100.0,  # power ceiling never binds -- only headroom does
        capacity_mwh=2.0,
        round_trip_efficiency=0.81,
        soc_min_fraction=0.1,
        soc_max_fraction=0.9,
        starting_soc_fraction=0.85,  # soc=1.7, close to soc_max=1.8
        capacity_commit_mw=0.0,
        capacity_markets=(("FCR", "price"),),
        max_cycles_per_day=None,
        strategy="cooptimized",
        activation_endurance_hours=0.25,
    )
    result = solve_cooptimized_dispatch(
        "DK1",
        BASE_TIME,
        BASE_TIME + timedelta(hours=1),
        config,
        price_series,
        {"FCR:price": fcr_series},
        {"FCR:price": "DKK"},
        [],
    )

    eta = config.round_trip_efficiency**0.5
    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    starting_soc = config.starting_soc_fraction * config.capacity_mwh

    down_headroom = (soc_max - starting_soc) / eta
    up_headroom = (starting_soc - soc_min) * eta
    assert down_headroom < up_headroom  # down side is the tighter constraint here

    expected_cap = down_headroom / config.activation_endurance_hours
    tick = result.ticks[0]
    committed_mw = tick.capacity_revenue_by_market["FCR:price"] / 10_000.0
    assert committed_mw == pytest.approx(expected_cap, rel=1e-4)
    # And it is NOT bound by the (much larger) up-headroom allowance alone --
    # a same-direction-only bug would have let it commit far more than this.
    assert committed_mw < up_headroom / config.activation_endurance_hours


# --- currency non-mixing ----------------------------------------------------------


def test_dk2_mixed_currency_stack_reports_separate_buckets():
    """DK2's aFRR_capacity is DKK, its FCR-D `"down"` leg is EUR (see
    `shared/units.py`'s registry-backed resolution) -- both configured
    together must land in separate, never-summed revenue buckets."""
    day_ahead = _price_rows([0.0] * 6)  # no arbitrage incentive -- isolate capacity legs
    fcr_down = _price_rows([20.0] * 6)  # EUR
    afrr = _price_rows([5.0] * 6)  # DKK
    activation = _price_rows([2.0] * 6)
    db = _db_with_series(day_ahead, fcr_down=fcr_down, afrr=afrr, activation=activation)
    config = BessConfig(
        strategy="cooptimized",
        capacity_markets=(("FCR", "down"), ("aFRR_capacity", "up")),
        capacity_mwh=50.0,
        power_mw=100.0,
        starting_soc_fraction=0.85,
    )
    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=6), config)

    assert result.currencies_present == {"DKK", "EUR"}
    assert result.total_capacity_revenue_dkk > 0
    assert result.total_capacity_revenue_eur > 0
    # No tick's revenue-by-market dict ever attributes a DKK leg's money to
    # the EUR leg or vice versa.
    for tick in result.ticks:
        assert set(tick.capacity_revenue_by_market) == {"FCR:down", "aFRR_capacity:up"}


def test_combined_total_math_matches_hand_computed_conversion():
    day_ahead = _price_rows([0.0] * 6)
    fcr_down = _price_rows([20.0] * 6)
    afrr = _price_rows([5.0] * 6)
    activation = _price_rows([2.0] * 6)
    db = _db_with_series(day_ahead, fcr_down=fcr_down, afrr=afrr, activation=activation)
    config = BessConfig(
        strategy="cooptimized",
        capacity_markets=(("FCR", "down"), ("aFRR_capacity", "up")),
        capacity_mwh=50.0,
        power_mw=100.0,
        starting_soc_fraction=0.85,
    )
    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=6), config)

    assert result.currencies_present == {"DKK", "EUR"}
    expected_all_dkk = (
        result.total_arbitrage_revenue_dkk + result.total_capacity_revenue_dkk
    ) + (result.total_capacity_revenue_eur + result.total_afrr_activation_revenue_eur) * DKK_PER_EUR
    expected_all_eur = (
        result.total_arbitrage_revenue_dkk + result.total_capacity_revenue_dkk
    ) / DKK_PER_EUR + (result.total_capacity_revenue_eur + result.total_afrr_activation_revenue_eur)

    assert result.total_revenue_all_dkk == pytest.approx(expected_all_dkk, rel=1e-9)
    assert result.total_revenue_all_eur == pytest.approx(expected_all_eur, rel=1e-9)
    assert DKK_PER_EUR == pytest.approx(7.46)


# --- phantom_capacity_revenue (P2 headline diagnostic) --------------------------
#
# Hand-constructed threshold-*like* `BacktestResult`/`BessTick` traces --
# `phantom_capacity_revenue` only reads `soc_mwh`/`capacity_revenue_by_market`/
# the cumulative capacity totals, so these are built directly rather than via
# `run_backtest`, keeping the expected numbers exactly hand-computable.

_PHANTOM_CONFIG = BessConfig(
    capacity_mwh=2.0,
    round_trip_efficiency=0.9,
    soc_min_fraction=0.1,
    soc_max_fraction=0.9,
    starting_soc_fraction=0.5,  # starting_soc = 1.0
    activation_endurance_hours=0.25,
    capacity_markets=(("aFRR_capacity", "up"),),
)


def _single_tick_result(
    zone: str,
    soc_mwh: float,
    capacity_revenue_by_market: dict[str, float],
    config: BessConfig = _PHANTOM_CONFIG,
) -> BacktestResult:
    capacity_revenue_dkk = sum(
        v for k, v in capacity_revenue_by_market.items() if k.split(":", 1)[0] == "aFRR_capacity"
    )
    capacity_revenue_eur = sum(
        v for k, v in capacity_revenue_by_market.items() if k.split(":", 1)[0] == "FCR"
    )
    tick = BessTick(
        time=BASE_TIME,
        soc_mwh=soc_mwh,
        soc_fraction=soc_mwh / config.capacity_mwh,
        action="discharge",
        day_ahead_price=100.0,
        energy_discharged_mwh=0.0,
        arbitrage_revenue_dkk=0.0,
        capacity_reserved_mw=0.0,
        capacity_revenue_dkk=capacity_revenue_dkk,
        capacity_revenue_by_market=dict(capacity_revenue_by_market),
        cumulative_arbitrage_revenue_dkk=0.0,
        cumulative_capacity_revenue_dkk=capacity_revenue_dkk,
        cumulative_total_revenue_dkk=capacity_revenue_dkk,
        capacity_revenue_eur=capacity_revenue_eur,
        cumulative_capacity_revenue_eur=capacity_revenue_eur,
    )
    return BacktestResult(
        zone=zone,
        start_time=BASE_TIME,
        end_time=BASE_TIME + timedelta(hours=1),
        config=config,
        ticks=[tick],
    )


def test_phantom_capacity_revenue_exact_amount_when_committed_beyond_headroom():
    """Starting SoC 1.0 (mid-band), tick ends at soc_min (0.2) -- a full
    discharge that period. `feasible_up_mw` against the tighter of
    start/end SoC is `min(1.0, 0.2) = 0.2 = soc_min`, so feasible up-reserve
    is exactly 0 -- the committed 5.0 MW is *entirely* phantom."""
    result = _single_tick_result("DK1", soc_mwh=0.2, capacity_revenue_by_market={
        "aFRR_capacity:up": 500.0  # 5.0 MW committed @ 100 DKK/MW/h, dt=1h
    })
    diagnostic = phantom_capacity_revenue(
        result, _PHANTOM_CONFIG, {"aFRR_capacity:up": [(BASE_TIME, 100.0)]}
    )
    assert diagnostic["phantom_capacity_revenue_dkk"] == pytest.approx(500.0)
    assert diagnostic["phantom_fraction_dkk"] == pytest.approx(1.0)
    assert diagnostic["phantom_capacity_revenue_eur"] == 0.0
    assert diagnostic["phantom_fraction_eur"] == 0.0


def test_phantom_capacity_revenue_zero_on_a_feasible_trace():
    """SoC stays at its starting 1.0 MWh (no discharge that tick) -- ample
    headroom (~3.04 MW) for the modest 0.5 MW committed, so nothing is
    phantom."""
    result = _single_tick_result(
        "DK1", soc_mwh=1.0, capacity_revenue_by_market={"aFRR_capacity:up": 50.0}
    )
    diagnostic = phantom_capacity_revenue(
        result, _PHANTOM_CONFIG, {"aFRR_capacity:up": [(BASE_TIME, 100.0)]}
    )
    assert diagnostic["phantom_capacity_revenue_dkk"] == pytest.approx(0.0, abs=1e-9)
    assert diagnostic["phantom_fraction_dkk"] == pytest.approx(0.0, abs=1e-9)


def test_phantom_capacity_revenue_buckets_dk2_mixed_currency_stack_separately():
    """A DK2-style stack: `aFRR_capacity:up` (DKK) fully phantom (same setup
    as the first test) alongside `FCR:down` (EUR), also over-committed but
    by a different, independently hand-computed amount -- each currency's
    phantom total must land in its own bucket, matching
    `shared/units.py:currency_for`'s DK2 resolution (aFRR_capacity DKK,
    FCR EUR)."""
    result = _single_tick_result(
        "DK2",
        soc_mwh=0.2,
        capacity_revenue_by_market={
            "aFRR_capacity:up": 500.0,  # 5.0 MW @ 100 DKK/MW/h -- feasible_up=0, all phantom
            "FCR:down": 200.0,  # 10.0 MW @ 20 EUR/MW/h -- feasible_down=3.373..., partly phantom
        },
    )
    diagnostic = phantom_capacity_revenue(
        result,
        _PHANTOM_CONFIG,
        {
            "aFRR_capacity:up": [(BASE_TIME, 100.0)],
            "FCR:down": [(BASE_TIME, 20.0)],
        },
    )
    assert diagnostic["phantom_capacity_revenue_dkk"] == pytest.approx(500.0)
    assert diagnostic["phantom_fraction_dkk"] == pytest.approx(1.0)
    # feasible_down_mw = (soc_max - max(prev_soc, tick_soc)) / eta / t_act
    #                  = (1.8 - 1.0) / sqrt(0.9) / 0.25 = 3.3730961708462717
    # phantom_mw = 10.0 - 3.3730961708462717 = 6.626903829153728
    # phantom_revenue = phantom_mw * 20.0 EUR/MW/h * 1h
    assert diagnostic["phantom_capacity_revenue_eur"] == pytest.approx(132.53807658307457)
    assert diagnostic["phantom_fraction_eur"] == pytest.approx(132.53807658307457 / 200.0)


# --- P3 Part A: imbalance as a second dispatchable energy market ---------------


def test_imbalance_discharge_beats_day_ahead_only_when_imbalance_price_is_higher():
    """2 hourly periods: flat/tied prices at hour 0, imbalance far above
    day-ahead at hour 1 (500 vs. 10). A day_ahead-only co-optimized run can
    only ever sell into day-ahead; a run with `energy_markets=("day_ahead",
    "imbalance")` must route hour 1's discharge into imbalance instead
    (the shared power budget means it picks the higher-paying market, not
    both), so its total energy revenue strictly exceeds the day_ahead-only
    run over the identical window."""
    day_ahead_values = [10.0, 10.0]
    imbalance_values = [10.0, 500.0]
    day_ahead = _price_rows(day_ahead_values)
    imbalance = _price_rows(imbalance_values)

    base_kwargs = dict(
        strategy="cooptimized",
        capacity_commit_mw=0.0,
        capacity_markets=(),
        power_mw=1.0,
        capacity_mwh=10.0,
        starting_soc_fraction=0.5,
    )

    db_da_only = _db_with_series(day_ahead)
    config_da_only = BessConfig(**base_kwargs)
    result_da_only = run_backtest(
        db_da_only, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=2), config_da_only
    )

    db_two_markets = _db_with_series(day_ahead, imbalance=imbalance)
    config_two_markets = BessConfig(**base_kwargs, energy_markets=("day_ahead", "imbalance"))
    result_two_markets = run_backtest(
        db_two_markets, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=2), config_two_markets
    )

    assert (
        result_two_markets.total_arbitrage_revenue_dkk
        > result_da_only.total_arbitrage_revenue_dkk
    )
    # Hour 1 (index 1) must have been settled at the higher imbalance price,
    # not day-ahead's -- the discharge revenue there can only be explained
    # by having sold into imbalance.
    hour1 = result_two_markets.ticks[1]
    assert hour1.action == "discharge"
    assert hour1.arbitrage_revenue_dkk > hour1.energy_discharged_mwh * day_ahead_values[1] + 1e-6


def test_soc_feasible_with_two_energy_markets():
    """SoC must stay within the usable band at every tick when the LP is
    routing flows across two energy markets simultaneously (day-ahead and
    imbalance alternately the more attractive one)."""
    day_ahead = _price_rows([10.0, 500.0, 5.0, 800.0, 20.0, 1.0])
    imbalance = _price_rows([500.0, 10.0, 900.0, 4.0, 3.0, 700.0])
    db = _db_with_series(day_ahead, imbalance=imbalance)
    config = BessConfig(
        strategy="cooptimized",
        capacity_commit_mw=0.0,
        capacity_markets=(),
        power_mw=2.0,
        capacity_mwh=3.0,
        starting_soc_fraction=0.5,
        energy_markets=("day_ahead", "imbalance"),
        max_cycles_per_day=None,
    )
    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=6), config)

    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    assert len(result.ticks) == 6
    for tick in result.ticks:
        assert soc_min - 1e-6 <= tick.soc_mwh <= soc_max + 1e-6


def test_day_ahead_only_energy_markets_reproduces_prior_p1_p2_behaviour():
    """`energy_markets=("day_ahead",)` (the default) must be behaviourally
    identical to `solve_cooptimized_dispatch` before this field existed --
    calling it via `run_backtest`'s new energy-markets wiring must match
    calling it directly with the old (pre-P3) single-series call
    convention, tick for tick."""
    prices = [30.0, 200.0, 10.0]
    price_series = _series(prices)
    config = BessConfig(
        power_mw=1.0,
        capacity_mwh=1.0,
        round_trip_efficiency=1.0,
        soc_min_fraction=0.1,
        soc_max_fraction=0.9,
        starting_soc_fraction=0.5,
        capacity_commit_mw=0.0,
        capacity_markets=(),
        max_cycles_per_day=None,
        strategy="cooptimized",
    )

    # Old (pre-P3) call convention: no energy_series_by_market at all.
    result_old_style = solve_cooptimized_dispatch(
        "DK1", BASE_TIME, BASE_TIME + timedelta(hours=3), config, price_series, {}, {}, []
    )
    # New explicit equivalent.
    result_explicit = solve_cooptimized_dispatch(
        "DK1",
        BASE_TIME,
        BASE_TIME + timedelta(hours=3),
        config,
        price_series,
        {},
        {},
        [],
        energy_series_by_market={"day_ahead": price_series},
        energy_currency={"day_ahead": "DKK"},
    )

    assert result_old_style.total_arbitrage_revenue_dkk == pytest.approx(148.0, abs=1e-6)
    assert result_explicit.total_arbitrage_revenue_dkk == pytest.approx(148.0, abs=1e-6)
    for t1, t2 in zip(result_old_style.ticks, result_explicit.ticks, strict=True):
        assert t1.soc_mwh == pytest.approx(t2.soc_mwh)
        assert t1.action == t2.action
        assert t1.arbitrage_revenue_dkk == pytest.approx(t2.arbitrage_revenue_dkk)


# --- P3 Part B: post (perfect) vs. pre (forecast) foresight ---------------------


def test_pre_leq_post_on_multiple_windows():
    """Two windows, each with a genuine price excursion at hour >= 24 whose
    lag-24h source (the same hour, the day before) does NOT show the
    excursion -- the forecast schedule genuinely misses it, so the
    perfect-foresight ("post") total captures real extra revenue the
    forecast-driven ("pre") schedule cannot: `pre <= post` must hold, and
    should hold with a real (not merely trivial-equality) gap."""
    values_spike = [100.0] * 30 + [1000.0] + [100.0] * 17  # spike at hour 30
    values_trough = [200.0] * 40 + [5.0] + [200.0] * 7  # trough at hour 40

    gaps = []
    for values in (values_spike, values_trough):
        day_ahead = _price_rows(values)
        db = _db_with_series(day_ahead)
        config = BessConfig(strategy="cooptimized", capacity_commit_mw=0.0, capacity_markets=())
        perfect = run_backtest(
            db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=len(values)), config
        )
        forecast = run_backtest(
            db,
            "DK1",
            BASE_TIME,
            BASE_TIME + timedelta(hours=len(values)),
            replace(config, foresight="forecast"),
        )
        assert forecast.total_revenue_all_dkk <= perfect.total_revenue_all_dkk + 1e-6
        gaps.append(perfect.total_revenue_all_dkk - forecast.total_revenue_all_dkk)

    assert any(gap > 1e-6 for gap in gaps)


def test_perfect_foresight_explicit_schedule_matches_omitted_schedule():
    """`foresight="perfect"` means schedule == settlement -- passing the
    SAME series explicitly as `schedule_energy_series_by_market`/
    `schedule_capacity_series_by_leg` must give byte-identical results to
    omitting them (the `None`-defaults-to-settlement path)."""
    prices = [30.0, 200.0, 10.0]
    price_series = _series(prices)
    config = BessConfig(
        power_mw=1.0,
        capacity_mwh=1.0,
        round_trip_efficiency=1.0,
        soc_min_fraction=0.1,
        soc_max_fraction=0.9,
        starting_soc_fraction=0.5,
        capacity_commit_mw=0.0,
        capacity_markets=(),
        max_cycles_per_day=None,
        strategy="cooptimized",
        foresight="perfect",
    )

    result_omitted = solve_cooptimized_dispatch(
        "DK1", BASE_TIME, BASE_TIME + timedelta(hours=3), config, price_series, {}, {}, []
    )
    result_explicit = solve_cooptimized_dispatch(
        "DK1",
        BASE_TIME,
        BASE_TIME + timedelta(hours=3),
        config,
        price_series,
        {},
        {},
        [],
        energy_series_by_market={"day_ahead": price_series},
        energy_currency={"day_ahead": "DKK"},
        schedule_energy_series_by_market={"day_ahead": price_series},
        schedule_capacity_series_by_leg={},
    )

    assert result_omitted.total_arbitrage_revenue_dkk == pytest.approx(
        result_explicit.total_arbitrage_revenue_dkk
    )
    for t1, t2 in zip(result_omitted.ticks, result_explicit.ticks, strict=True):
        assert t1.soc_mwh == pytest.approx(t2.soc_mwh)
        assert t1.action == t2.action
        assert t1.arbitrage_revenue_dkk == pytest.approx(t2.arbitrage_revenue_dkk)


def test_foresight_rejects_invalid_value():
    with pytest.raises(ValueError, match="foresight"):
        BessConfig(foresight="oracle")


def test_energy_markets_rejects_unknown_market():
    with pytest.raises(ValueError, match="energy market"):
        BessConfig(energy_markets=("day_ahead", "FCR"))


def test_energy_markets_rejects_excluded_market():
    with pytest.raises(ValueError, match="not eligible"):
        BessConfig(energy_markets=("mFRR_capacity",))


def test_energy_markets_rejects_empty():
    with pytest.raises(ValueError, match="energy_markets"):
        BessConfig(energy_markets=())


def test_energy_markets_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicates"):
        BessConfig(energy_markets=("day_ahead", "day_ahead"))


# --- P4: single joint pegged LP -- no currency crowd-out ------------------------


def test_joint_lp_does_not_crowd_out_eur_capacity_when_energy_is_dkk():
    """The key P4 regression: a single period where day-ahead (DKK) arbitrage
    and a EUR capacity leg (FCR:down) both want the SAME scarce MW of power.
    Per-MW-per-hour, the EUR leg is worth far more once peg-converted (50
    EUR/MW/h * 7.46 = 373 DKK-equiv) than the arbitrage sale (100 DKK/MWh),
    so the TRUE joint optimum commits the whole 1 MW to capacity and does
    NOT discharge at all.

    This is designed to FAIL on a greedy energy-first decomposition (the
    superseded two-solve design, docs/bess-cooptimizer-design.md §4's
    "design evolution" note): that design decides the DKK energy leg first,
    with no visibility into the EUR leg's value at all, so it would happily
    discharge the full 1 MW for the 100 DKK sale (selling already-stored,
    'free' energy at any positive price is always attractive in isolation)
    and leave ZERO leftover power for the EUR capacity leg -- exactly the
    crowd-out artifact the joint LP replaces. Asserts both that EUR capacity
    revenue is nonzero (not crowded to ~0) AND that the joint total beats
    the greedy energy-first baseline (100 DKK) outright.
    """
    day_ahead = _price_rows([100.0])
    fcr_down = _price_rows([50.0])  # EUR
    db = _db_with_series(day_ahead, fcr_down=fcr_down)
    config = BessConfig(
        strategy="cooptimized",
        capacity_markets=(("FCR", "down"),),
        capacity_commit_mw=0.0,
        power_mw=1.0,
        capacity_mwh=10.0,
        starting_soc_fraction=0.5,
        max_cycles_per_day=None,
    )
    result = run_backtest(db, "DK2", BASE_TIME, BASE_TIME + timedelta(hours=1), config)

    tick = result.ticks[0]
    greedy_energy_first_dkk = 100.0 * 1.0 * 1.0  # full discharge, 0 capacity

    assert result.total_capacity_revenue_eur > 0, "EUR capacity crowded to ~0 -- the P4 bug"
    assert tick.capacity_revenue_by_market["FCR:down"] == pytest.approx(50.0, rel=1e-6)
    assert result.total_revenue_all_dkk == pytest.approx(50.0 * DKK_PER_EUR, rel=1e-6)
    assert result.total_revenue_all_dkk > greedy_energy_first_dkk


def test_total_revenue_all_eur_equals_total_revenue_all_dkk_over_peg():
    """`total_revenue_all_eur == total_revenue_all_dkk / DKK_PER_EUR` is a
    pure algebraic identity of the two properties' formulas (both are the
    SAME underlying figure, just which side of the peg conversion is
    applied) -- holds regardless of which currencies the energy/capacity
    legs are denominated in, since both properties are computed from the
    same four `BacktestResult` totals either way."""
    day_ahead = _price_rows([100.0] * 6)
    afrr = _price_rows([5.0] * 6)
    db = _db_with_series(day_ahead, afrr=afrr)
    config = BessConfig(
        strategy="cooptimized",
        capacity_markets=(("aFRR_capacity", "up"),),
        capacity_mwh=5.0,
        power_mw=2.0,
        starting_soc_fraction=0.5,
        max_cycles_per_day=None,
    )
    result = run_backtest(db, "DK1", BASE_TIME, BASE_TIME + timedelta(hours=6), config)

    assert result.total_revenue_all_dkk != 0.0
    assert result.total_revenue_all_eur == pytest.approx(
        result.total_revenue_all_dkk / DKK_PER_EUR, rel=1e-9
    )


# --- no data / empty window -------------------------------------------------------


def test_cooptimized_no_data_returns_empty_result_not_error():
    config = BessConfig(strategy="cooptimized")
    result = solve_cooptimized_dispatch(
        "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config, [], {}, {}, []
    )
    assert result.ticks == []
    assert result.total_revenue_dkk == 0.0
    assert result.total_revenue_all_dkk == 0.0
