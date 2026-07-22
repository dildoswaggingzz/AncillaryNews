from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from shared.bess_dispatch_milp import _leg_direction, solve_cooptimized_dispatch
from shared.bess_simulator import BessConfig, run_backtest
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
        starting_soc_fraction=0.15,  # tight up-headroom -> DKK leg doesn't claim all the power
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
        starting_soc_fraction=0.15,
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


# --- no data / empty window -------------------------------------------------------


def test_cooptimized_no_data_returns_empty_result_not_error():
    config = BessConfig(strategy="cooptimized")
    result = solve_cooptimized_dispatch(
        "DK1", BASE_TIME, BASE_TIME + timedelta(hours=5), config, [], {}, {}, []
    )
    assert result.ticks == []
    assert result.total_revenue_dkk == 0.0
    assert result.total_revenue_all_dkk == 0.0
