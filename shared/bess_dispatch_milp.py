"""
Perfect-foresight ("post") and forecast-driven ("pre") linear-program
co-optimizer for BESS dispatch.

`shared/bess_simulator.py:run_backtest`'s `"threshold"` strategy computes
energy arbitrage, capacity reservation, and aFRR activation as three
**independent** revenue streams and sums them -- a real battery has *one*
power rating and *one* state-of-charge, and every market competes for the
same MW and the same MWh (see that module's docstring §0 for the exact
defects this causes, most importantly *double-selling*: booking capacity
payments for MW the battery has no stored energy left to actually deliver).
This module fixes that by solving **one linear program per backtest
window** -- see `docs/bess-cooptimizer-design.md` §5 for the post/pre
distinction (both built here, as of P3) and §6 for the multi-market energy
stack (day-ahead + imbalance, as of P3).

Exposed through the *existing* `run_backtest` entry point as
`BessConfig(strategy="cooptimized")` -- `run_backtest` still owns every DB
call (this module is pure: no DB, no network, so it is unit-testable with
synthetic series) and passes the already-fetched series in.

**The LP** (docs/bess-cooptimizer-design.md §2/§6). Periods `t = 0..T-1`
over the day-ahead price timeline (same period/dt_hours convention as the
threshold engine). Decision variables, all >= 0: `ch[e, t]`/`dis[e, t]`
(grid charge/discharge power, MW, **one pair per configured energy market**
`e` -- `BessConfig.energy_markets`, e.g. `"day_ahead"` and, as of P3,
`"imbalance"`), `soc[t]` (state of charge, MWh), and one `cap[m, t]` per
configured capacity leg `m` (MW committed that period). Constraints: SoC
balance with split round-trip efficiency (`leg_efficiency =
round_trip_efficiency ** 0.5`), now summed across every energy market:

    soc[t+1] = soc[t] + eta * sum(ch[e, t] for e) * dt[t]
                       - sum(dis[e, t] for e) * dt[t] / eta

the usable SoC band; ONE shared power budget across every energy market and
every capacity leg: `sum(ch[e, t] + dis[e, t] for e) + sum(cap[m, t] for m)
<= power_mw`; and the no-double-selling headroom bound -- committed
up-reserve must be deliverable out of currently stored energy for
`activation_endurance_hours` (`T_act`, an *energy-endurance* duration, not a
ramp time -- a BESS ramps in seconds; see `BessConfig.activation_endurance_hours`'s
docstring), and committed down-reserve must have room to absorb. **The
reference rule this implements -- "subtract committed net position before
offering capacity" -- means the reserve must stay deliverable for the
*whole* period, not just at its start**: `soc` moves monotonically within a
period (net charge/discharge power is constant over `[t, t+1)`), so binding
the headroom bound at *both* the start-of-period SoC (`soc[t]`) and the
end-of-period SoC (`soc[t+1]`, i.e. after that period's own committed
energy flows have been applied) is sufficient to guarantee deliverability
throughout the whole period -- a single start-only bound leaves a residual
within-period double-sell (the energy legs discharge toward `soc_min`
*during* the period while the reserve was sized off the higher start-of-period
SoC):

    sum(cap[m, t] for m in up_legs)   * T_act <= (soc[t]   - soc_min) * eta
    sum(cap[m, t] for m in up_legs)   * T_act <= (soc[t+1] - soc_min) * eta
    sum(cap[m, t] for m in down_legs) * T_act <= (soc_max - soc[t])   / eta
    sum(cap[m, t] for m in down_legs) * T_act <= (soc_max - soc[t+1]) / eta

A leg's direction (`_leg_direction`) is resolved from its product string:
`"up"`/`"down"` are directional (e.g. DK2's FCR-D pair, aFRR capacity);
`"price"` is **symmetric** (FCR-N/DK1's single FCR price, FFR) -- one
`cap[m, t]` variable obligates *both* the up- and down-headroom sums at
once, but is paid for (and counted against the power budget) exactly once,
matching the physical reality of a single symmetric reserve band. A rolling
24-hour cap on discharge energy is added when `config.max_cycles_per_day`
is set, summed across every energy market's discharge (a physical
duty-cycle limit on the battery, not a per-market one), mirroring the
threshold engine's cycle cap.

No binary variable is needed to forbid simultaneous charge/discharge on any
one energy market: `round_trip_efficiency < 1` makes any `ch[e, t] > 0 and
dis[e, t] > 0` for the SAME `e` strictly revenue-losing (paying the
round-trip loss for no price gain), so the LP relaxation's optimum never
does it (docs/bess-cooptimizer-design.md §3). Simultaneously charging on
one market and discharging on another in the same period is never optimal
either, by an identical argument (it nets out to a smaller, loss-incurring
version of just doing the more profitable one directly) -- but nothing
*forbids* the solver from momentarily representing it that way at a
degenerate tie; only the NET flow (and hence `action`/`energy_discharged_mwh`,
derived from the summed flows) is ever reported.

**Multiple dispatchable energy markets (P3, docs/bess-cooptimizer-design.md
§6).** Day-ahead alone (`BessConfig.energy_markets = ("day_ahead",)`, the
default) reproduces P1/P2's single-energy-market behaviour exactly
(regression-tested). Adding `"imbalance"` lets the LP route discharge to
whichever of {day-ahead, imbalance} pays more and charge to whichever costs
less, each period, subject to the ONE shared power/SoC budget above -- this
models a BESS as the *controllable* asset it is: unlike a wind farm (which
merely settles a forecast-error deviation against the imbalance price), a
battery *chooses* its imbalance exposure, so it belongs alongside day-ahead
as a second dispatchable price, not a passive settlement stream (the design
doc's earlier "passive-settlement" lean, its §9, is superseded -- see its
§6). `imbalance` is DKK/MWh, the SAME currency as day-ahead
(`shared/bess_simulator.py`'s `ENERGY_MARKET_PRODUCT`), so every energy
market lives entirely inside Solve 1 below -- no new currency handling.
`BessTick.arbitrage_revenue_dkk` remains the TOTAL energy revenue across
every configured energy market that tick (the field's meaning is
unchanged: "energy arbitrage revenue"); a per-market split is not
persisted (`capacity_revenue_by_market`'s dict shape is a *capacity-leg*
concept and is deliberately not reused for this -- a future, explicitly
separate field if ever needed).

**Post vs. pre foresight (P3, docs/bess-cooptimizer-design.md §5).** Every
price series this module optimises against -- every energy market and
every capacity leg -- has two roles: a **schedule** price (what the LP's
objective is built from, i.e. what decides `ch`/`dis`/`cap`) and a
**settlement** price (what the resulting FIXED schedule's revenue is
actually reported at, on every `BessTick`). `BessConfig.foresight ==
"perfect"` (the default) passes the same actual/realised series for both
roles, so nothing changes from P1/P2's behaviour byte-for-byte.
`foresight == "forecast"` (pre mode) has `run_backtest` build a causal
lag-24h-persistence forecast of each schedulable series
(`shared/bess_simulator.py:_lag24h_forecast`) and pass it in as the
`schedule_*` parameters below, while the `energy_series_by_market`/
`capacity_series_by_leg` parameters keep carrying the actual/settlement
series throughout -- so the LP schedules against what was *expected*, and
every tick's reported revenue is `Σ actual_price · scheduled_flow`, the
realistic "you bid on your forecast, you get paid what happened"
evaluation. The post − pre gap is the monetary value of forecast skill,
and is guaranteed `>= 0` (`foresight="forecast"` <= `foresight="perfect"`
on the same window, tests/test_bess_dispatch_milp.py's `pre <= post`
gate): the fixed forecast-driven schedule is itself one feasible schedule
the perfect-foresight problem could also have chosen (identical physical
constraints either way), so its actual-settled value can never exceed the
perfect-foresight optimum. **aFRR activation revenue is the one exception,
by design, not oversight**: it is never part of either solve's objective
in the first place (see the currency-decomposition section below), so
there is nothing for a "schedule" price to influence -- it is always
computed from whatever `aFRR_capacity` commitment resulted (decided for
other, DKK/EUR-capacity-revenue reasons) and the SAME `activation_price_series`
parameter in both foresight modes; a `schedule_activation_price_series`
parameter would be a pure no-op, so this module deliberately doesn't add
one.

**Currency decomposition (docs/bess-cooptimizer-design.md §4) -- P1's
choice, unchanged by P3.** A single scalar LP objective can never contain
both a DKK term and a EUR term without implicitly asserting some exchange
rate between them -- exactly the unit-mixing bug `shared/units.py` and the
threshold engine's per-currency buckets exist to prevent. Every configured
energy market is unconditionally DKK (day-ahead and imbalance both are, per
`shared/datasets.py`'s registry), so none of them can ever be combined with
a EUR capacity term in one objective either. This module therefore solves
**two separate LPs that share the battery's physical trajectory only
through the DKK energy legs**, not two independent optimizations of the
same physical battery:

1.  **Solve 1 (DKK)** -- variables `ch[e, t]`/`dis[e, t]` for every energy
    market, `soc`, and `cap[m, t]` for every DKK-currency capacity leg.
    Objective: total energy revenue (across every energy market, at
    SCHEDULE prices) + DKK capacity revenue (at SCHEDULE prices). Subject
    to the full power budget and headroom bounds (using only the DKK legs,
    since EUR legs do not exist in this solve at all). This solve's
    `ch`/`dis`/`soc` trajectory is authoritative for every tick's
    `action`/`soc_mwh`/`energy_discharged_mwh` fields -- there is exactly
    one physical trajectory reported, never two competing ones;
    `arbitrage_revenue_dkk` is then computed by re-valuing that FIXED
    trajectory at SETTLEMENT prices (perfect mode: identical to the
    schedule prices, so this is a no-op re-valuation).
2.  **Solve 2 (EUR)**, only built if any EUR-currency leg is configured --
    variables `cap[m, t]` for every EUR-currency leg *only*. `ch`, `dis`,
    and `soc` are no longer variables here: they are the fixed numeric
    values Solve 1 already committed to, so Solve 2's power-budget and
    headroom constraints use *leftover* numeric bounds (`power_mw` minus
    Solve 1's total energy flow across every market, and the DKK legs'
    already-claimed share of headroom subtracted out) -- plain numbers
    derived from Solve 1, not LP variables, so **no DKK quantity is ever a
    decision variable, coefficient, or comparison operand in Solve 2's
    model, and no EUR quantity ever appears in Solve 1's** -- the two
    currencies' price signals never occupy the same objective or the same
    side of any comparison. `_assert_currency_partition` checks this
    partition holds before either solve is built. Same start/end-of-period
    reasoning as Solve 1's headroom bound applies here too: the leftover
    headroom allowance for period `t` is computed at *both*
    `soc_star[t]` and `soc_star[t+1]` and the **tighter (minimum)** of the
    two is what Solve 2 gets to use -- an EUR leg's own commitment must
    stay deliverable throughout the period against Solve 1's already-fixed
    trajectory, exactly like a same-currency leg would have to. Solve 2's
    objective is likewise built from SCHEDULE prices, and its committed
    `cap_eur` is re-valued at SETTLEMENT prices for reporting.

This is a deliberate, documented simplification, not the only valid
decomposition (docs/bess-cooptimizer-design.md §4 leaves the exact choice
to P1): Solve 1 does not "know about" the EUR legs' revenue potential when
deciding how much headroom/power to leave for energy dispatch vs. its own
DKK legs, so the combined result is not necessarily the *global* joint
optimum across both currencies -- but it is always feasible (no
double-selling, ever, across the whole stack, since Solve 2's bounds are
literally what's left over after Solve 1's real commitments) and it never
mixes currency magnitudes.

Solved with PuLP's bundled CBC backend (`pulp.PULP_CBC_CMD`) -- a pure LP
(no integer variables), so CBC's simplex solve is deterministic for a fixed
model, preserving `save_bess_run`'s reproducibility contract (the same
persisted `config` re-solves to the same dispatch).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

import pulp

from shared.bess_simulator import BacktestResult, BessConfig, BessTick, _value_at_or_before
from shared.units import currency_for

# Numeric tolerance for classifying a period's action from `ch`/`dis` (which
# should never both be meaningfully positive at the LP optimum -- see module
# docstring's "no binary needed" paragraph -- but a simplex solve can leave
# a variable at a tiny nonzero residual well below solver tolerance) and for
# clipping Solve 2's leftover bounds (docstring's currency-decomposition
# section) to zero rather than a hair-negative float.
_EPS = 1e-6


def _leg_direction(market: str, product: str) -> Literal["up", "down", "symmetric"]:
    """
    Resolves a configured capacity leg's reserve direction from its product
    string (module docstring): `"up"`/`"down"` are the directional legs
    (DK2's FCR-D pair, `aFRR_capacity`'s up/down auctions); `"price"` is the
    symmetric band every other configured FCR-type product uses today
    (DK1's single FCR price, DK2's FCR-N, FFR -- see `shared/datasets.py`'s
    `fcr_dk1`/`fcr_dk2`/`ffr_dk2` entries, all of which register their
    single symmetric-band product as `"price"`). Raises `ValueError` for
    any other product string rather than silently guessing a direction --
    an unrecognised product on a *capacity* leg is a configuration mistake
    the LP should refuse to build a model for, not quietly misclassify.
    """
    if product == "up":
        return "up"
    if product == "down":
        return "down"
    if product == "price":
        return "symmetric"
    raise ValueError(
        f"cannot resolve capacity-reservation direction for leg (market={market!r}, "
        f"product={product!r}) -- expected product 'up', 'down', or 'price'"
    )


def _period_dt_hours(times: list[datetime]) -> list[float]:
    """
    Period duration in hours for each tick: the gap to the next tick, or the
    gap from the previous tick for the last one (falls back to 1 hour for a
    single-tick window) -- identical convention to
    `shared/bess_simulator.py:run_backtest`'s per-tick `dt_hours` (its
    lines ~695-705), duplicated here (not imported) since it is a tiny, pure
    calculation and this module intentionally has no other dependency on
    that function's per-tick loop.
    """
    n = len(times)
    dt: list[float] = []
    for i in range(n):
        if i + 1 < n:
            d = (times[i + 1] - times[i]).total_seconds() / 3600.0
        elif i > 0:
            d = (times[i] - times[i - 1]).total_seconds() / 3600.0
        else:
            d = 1.0
        dt.append(max(d, 0.0))
    return dt


def _rolling_24h_window_indices(times: list[datetime]) -> list[list[int]]:
    """
    For each index `t`, the list of indices `j <= t` whose `times[j]` falls
    within `(times[t] - 24h, times[t]]` -- the rolling window
    `max_cycles_per_day`'s cycle cap sums discharge energy over (mirrors
    `shared/bess_simulator.py:run_backtest`'s `discharge_window` deque, but
    computed once up front here since the LP needs every period's window as
    a single constraint's term list, not a per-tick running deque). Two-
    pointer: `times` is ascending (guaranteed by `_fetch_series`), so the
    window's lower bound only ever moves forward as `t` increases.
    """
    n = len(times)
    lo = 0
    windows: list[list[int]] = []
    for t in range(n):
        cutoff = times[t] - timedelta(hours=24)
        while times[lo] <= cutoff:
            lo += 1
        windows.append(list(range(lo, t + 1)))
    return windows


def _assert_currency_partition(
    dkk_keys: list[str], eur_keys: list[str], leg_currency: dict[str, str | None]
) -> None:
    """
    Structural sanity check for the currency decomposition (module
    docstring): every configured leg is in exactly one of `dkk_keys`/
    `eur_keys`, each resolving to the currency its own key claims. This is
    what actually guarantees "no EUR/DKK magnitude comparison ever
    happens" -- not a runtime check on the LPs' numeric solutions (which
    would be too late), but a guarantee that Solve 1 and Solve 2 are built
    from disjoint variable sets in the first place, so there is no code
    path in which a DKK-leg coefficient and a EUR-leg coefficient could
    ever land in the same `pulp.lpSum`.
    """
    assert set(dkk_keys).isdisjoint(eur_keys), "a capacity leg cannot be both DKK and EUR"
    assert set(dkk_keys) | set(eur_keys) == set(leg_currency), (
        "every configured capacity leg must resolve to exactly DKK or EUR "
        "(run_backtest's ValueError-on-unknown-currency check should have caught this earlier)"
    )
    assert all(leg_currency[k] == "DKK" for k in dkk_keys)
    assert all(leg_currency[k] == "EUR" for k in eur_keys)


def _assert_energy_markets_match(
    energy_markets: tuple[str, ...], energy_series_by_market: dict[str, list]
) -> None:
    """
    `energy_series_by_market`'s keys must be exactly `config.energy_markets`
    -- a caller (in practice only `run_backtest`, or a test) passing a
    series dict that doesn't match the configured markets is a genuine
    caller bug (a market the LP will build variables for with no price
    series, or a fetched series the LP will never look at), so this fails
    loud rather than silently ignoring the mismatch either direction.
    """
    assert set(energy_series_by_market.keys()) == set(energy_markets), (
        f"energy_series_by_market's keys {sorted(energy_series_by_market.keys())!r} must "
        f"exactly match config.energy_markets {sorted(energy_markets)!r}"
    )


def phantom_capacity_revenue(
    result: BacktestResult,
    config: BessConfig,
    capacity_series_by_leg: dict[str, list[tuple[datetime, float]]],
) -> dict[str, float]:
    """
    P2's headline overstatement diagnostic (docs/bess-cooptimizer-design.md
    §7 P2 row / §8): replays a **threshold-strategy** `BacktestResult` trace
    (`shared/bess_simulator.py`'s module docstring §0 -- that engine books
    capacity revenue unconditionally, with no requirement that the
    committed MW is actually deliverable out of stored energy) and measures
    how much of its booked capacity revenue was **infeasible** under the
    SAME both-endpoint no-double-selling headroom rule this module's
    co-optimizer enforces (see the module docstring's headroom equations
    above). This is computed purely from the threshold trace itself, never
    from the co-optimizer's own dispatch or strategy choices -- it isolates
    exactly the double-selling defect and nothing else, which is what makes
    it the clean, unconfounded "how wrong is the number we currently
    publish" figure (as opposed to a threshold-vs-cooptimized revenue
    delta, which is *also* confounded by the co-optimizer's different,
    better arbitrage timing -- see docs/bess-cooptimizer-design.md §8's
    corrected framing: a lower co-optimized total on a double-selling
    window is not a co-optimizer regression, and this function is what
    separates "phantom revenue removed" from "real revenue foregone").
    Unaffected by P3 (multi-market energy, post/pre foresight) -- the
    threshold engine this function replays stays day_ahead-only and has no
    schedule/settlement split at all.

    For each tick, per configured leg, the maximum MW that leg could
    honestly have committed is bounded by the same headroom rule, evaluated
    at whichever of the tick's start-of-period SoC (`prev_soc` -- the
    previous tick's `soc_mwh`, or `config.starting_soc_fraction *
    capacity_mwh` for the very first tick) and end-of-period SoC
    (`tick.soc_mwh`) is TIGHTER for that direction (mirroring Solve 1's own
    both-endpoint bound, not just a start-of-period check):

        feasible_up_mw   = max((min(prev_soc, tick.soc_mwh) - soc_min) * eta / T_act, 0)
        feasible_down_mw = max((soc_max - max(prev_soc, tick.soc_mwh)) / eta / T_act, 0)

    A symmetric (`"price"`) leg is bound by the tighter of the two
    (`min(feasible_up_mw, feasible_down_mw)`), matching `_leg_direction`'s
    "obligates both sides at once" treatment elsewhere in this module.

    **Known simplification -- per-leg, not per-direction-group.** When
    multiple legs share a direction that tick (e.g. DK2's `aFRR_capacity:up`
    and `FCR:up` both drawing on the same physical up-headroom), each leg's
    feasible bound is computed independently against the *full* headroom,
    not a joint bound split across every leg contending for it that
    period. This can UNDERSTATE the true phantom total when several
    same-direction legs are each individually within the full headroom but
    jointly exceed it -- a conservative (understating, not overstating)
    simplification, called out explicitly rather than silently assumed
    away; a joint per-direction-group accounting is future work if a real
    run's numbers ever call for it.

    A leg's committed MW isn't stored directly on `BessTick` (only its
    revenue is, in `capacity_revenue_by_market`) -- recovered here as
    `revenue / (clearing_price * dt)`, `clearing_price` looked up the
    identical way `run_backtest`/`solve_cooptimized_dispatch` do
    (`_value_at_or_before` against `capacity_series_by_leg`, the same
    already-fetched series `run_backtest` would pass to the co-optimizer)
    and `dt` the tick's own period duration (`_period_dt_hours`, this
    module's convention throughout). A leg with no price that tick, or a
    zero clearing price, is skipped (guards the divide-by-zero -- its
    revenue, and therefore any phantom contribution, is necessarily 0
    either way, so there is nothing to attribute).

    Returns:

        {"phantom_capacity_revenue_dkk", "phantom_capacity_revenue_eur",
         "phantom_fraction_dkk", "phantom_fraction_eur"}

    the DKK/EUR phantom-revenue totals and each as a fraction of that
    currency's *actual* reported `result.total_capacity_revenue_dkk` /
    `_eur` (0.0, not NaN, when that total itself is 0 -- there was no
    revenue booked in that currency at all, so nothing to overstate).
    Currency is resolved per leg via `shared.units.currency_for(market,
    result.zone, product)`; a leg that doesn't resolve to DKK or EUR raises
    `ValueError` -- the same fail-loud posture `run_backtest` takes on an
    unlabelled capacity leg, rather than silently dropping it from either
    bucket.
    """
    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    eta = config.round_trip_efficiency**0.5
    t_act = config.activation_endurance_hours

    times = [tick.time for tick in result.ticks]
    dt = _period_dt_hours(times)

    phantom_dkk = 0.0
    phantom_eur = 0.0
    prev_soc = config.starting_soc_fraction * config.capacity_mwh

    for i, tick in enumerate(result.ticks):
        tick_soc = tick.soc_mwh
        feasible_up_mw = max((min(prev_soc, tick_soc) - soc_min) * eta / t_act, 0.0)
        feasible_down_mw = max((soc_max - max(prev_soc, tick_soc)) / eta / t_act, 0.0)

        for key, revenue in tick.capacity_revenue_by_market.items():
            market, product = key.split(":", 1)
            direction = _leg_direction(market, product)
            clearing_price = _value_at_or_before(capacity_series_by_leg.get(key, []), tick.time)
            if not clearing_price or dt[i] <= 0:
                # No price (or a real 0 price, or a zero-length period) --
                # revenue is necessarily 0 either way, nothing to divide by
                # or attribute.
                continue

            committed_mw = revenue / (clearing_price * dt[i])
            if direction == "up":
                feasible_mw = feasible_up_mw
            elif direction == "down":
                feasible_mw = feasible_down_mw
            else:
                feasible_mw = min(feasible_up_mw, feasible_down_mw)

            phantom_mw = max(committed_mw - feasible_mw, 0.0)
            phantom_revenue = phantom_mw * clearing_price * dt[i]

            currency = currency_for(market, result.zone, product)
            if currency == "DKK":
                phantom_dkk += phantom_revenue
            elif currency == "EUR":
                phantom_eur += phantom_revenue
            else:
                raise ValueError(
                    f"no DKK/EUR currency resolved for capacity leg {key!r} in zone "
                    f"{result.zone!r} -- add `unit=` to the SeriesConfig in shared/datasets.py"
                )

        prev_soc = tick_soc

    total_dkk = result.total_capacity_revenue_dkk
    total_eur = result.total_capacity_revenue_eur
    return {
        "phantom_capacity_revenue_dkk": phantom_dkk,
        "phantom_capacity_revenue_eur": phantom_eur,
        "phantom_fraction_dkk": (phantom_dkk / total_dkk) if total_dkk else 0.0,
        "phantom_fraction_eur": (phantom_eur / total_eur) if total_eur else 0.0,
    }


def solve_cooptimized_dispatch(
    zone: str,
    start_time: datetime,
    end_time: datetime,
    config: BessConfig,
    price_series: list[tuple[datetime, float]],
    capacity_series_by_leg: dict[str, list[tuple[datetime, float]]],
    leg_currency: dict[str, str],
    activation_price_series: list[tuple[datetime, float]],
    energy_series_by_market: dict[str, list[tuple[datetime, float]]] | None = None,
    schedule_energy_series_by_market: dict[str, list[tuple[datetime, float]]] | None = None,
    schedule_capacity_series_by_leg: dict[str, list[tuple[datetime, float]]] | None = None,
) -> BacktestResult:
    """
    Solves the co-optimized dispatch LP (module docstring) over
    already-fetched series and returns a `BacktestResult` identical in
    shape to the threshold engine's -- one `BessTick` per `price_series`
    point, same fields, so `save_bess_run` and the dashboard consume it
    unchanged. Pure: no DB access, no network -- every input is an
    in-memory series, so this function is unit-testable with synthetic
    data (see `tests/test_bess_dispatch_milp.py`).

    `price_series` still drives the tick timeline (`times`/`dt`/`T`) and
    the `day_ahead_price` tick field, exactly as in P1/P2 -- unchanged.

    `energy_series_by_market` (P3): every energy market's actual/settlement
    series, keyed by market name (`config.energy_markets`). Defaults to
    `None`, in which case (backward-compatible with every P1/P2 call site,
    including this module's own tests) it is built as
    `{config.energy_markets[0]: price_series}` -- only valid when
    `energy_markets` has exactly one entry; a caller configuring more than
    one energy market MUST pass this explicitly (there is no way to guess
    which series is which from `price_series` alone).

    `schedule_energy_series_by_market`/`schedule_capacity_series_by_leg`
    (P3, post/pre foresight -- module docstring): the series each solve's
    OBJECTIVE is built from (what decides `ch`/`dis`/`cap`), as opposed to
    `energy_series_by_market`/`capacity_series_by_leg` (the SETTLEMENT
    series every tick's reported revenue is computed from, once the
    schedule is fixed). Both default to `None`, meaning "same as
    settlement" -- `BessConfig.foresight == "perfect"`'s behaviour, and
    byte-identical to omitting them entirely (tests/test_bess_dispatch_milp.py
    covers this explicitly).

    `capacity_allocation`/`capacity_allocation_fell_back_to_even` are
    threshold-only concepts (that strategy's fixed-split allocator, module
    docstring's decomposition section) -- always `False` here, since the LP
    decides each leg's commitment level directly, never via a group/even
    split.
    """
    if not price_series:
        # No day-ahead data in the window at all -- same "no data" signal
        # the threshold engine returns (an empty tick list, not an error).
        return BacktestResult(
            zone=zone,
            start_time=start_time,
            end_time=end_time,
            config=config,
            ticks=[],
            zero_price_periods_by_leg={},
            capacity_allocation_fell_back_to_even=False,
        )

    times = [t for t, _ in price_series]
    prices = [p for _, p in price_series]
    T = len(times)
    dt = _period_dt_hours(times)

    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    starting_soc = config.starting_soc_fraction * config.capacity_mwh
    eta = config.round_trip_efficiency**0.5
    t_act = config.activation_endurance_hours
    cap_mwh_per_window = (
        config.capacity_mwh * config.max_cycles_per_day
        if config.max_cycles_per_day is not None
        else None
    )
    windows = _rolling_24h_window_indices(times) if cap_mwh_per_window is not None else None

    # --- P3: energy markets (module docstring) --------------------------
    if energy_series_by_market is None:
        if len(config.energy_markets) != 1:
            raise ValueError(
                "energy_series_by_market must be provided explicitly when "
                f"config.energy_markets has more than one entry (got {config.energy_markets!r})"
            )
        energy_series_by_market = {config.energy_markets[0]: price_series}
    _assert_energy_markets_match(config.energy_markets, energy_series_by_market)
    energy_markets = list(config.energy_markets)

    # --- P3: schedule vs. settlement (module docstring) -- perfect mode
    # (the default) makes both identical, so nothing below changes from
    # P1/P2's behaviour. ---
    if schedule_energy_series_by_market is None:
        schedule_energy_series_by_market = energy_series_by_market
    if schedule_capacity_series_by_leg is None:
        schedule_capacity_series_by_leg = capacity_series_by_leg

    energy_settlement_price_at_t: dict[str, list[float | None]] = {
        m: [_value_at_or_before(energy_series_by_market[m], t) for t in times]
        for m in energy_markets
    }
    energy_schedule_price_at_t: dict[str, list[float | None]] = {
        m: [_value_at_or_before(schedule_energy_series_by_market[m], t) for t in times]
        for m in energy_markets
    }

    leg_keys = list(capacity_series_by_leg.keys())
    leg_direction: dict[str, Literal["up", "down", "symmetric"]] = {
        key: _leg_direction(*key.split(":", 1)) for key in leg_keys
    }
    # Per-leg, per-period clearing price -- SETTLEMENT (what every tick's
    # reported revenue, and `zero_price_periods_by_leg`, is computed from)
    # and SCHEDULE (what each solve's objective is built from, i.e. what
    # decides `cap`) are kept separate; perfect mode makes them identical.
    leg_settlement_price_at_t: dict[str, list[float | None]] = {
        key: [_value_at_or_before(capacity_series_by_leg[key], t) for t in times]
        for key in leg_keys
    }
    leg_schedule_price_at_t: dict[str, list[float | None]] = {
        key: [_value_at_or_before(schedule_capacity_series_by_leg[key], t) for t in times]
        for key in leg_keys
    }

    zero_price_periods_by_leg: dict[str, int] = defaultdict(int)
    for key in leg_keys:
        for price in leg_settlement_price_at_t[key]:
            if price == 0.0:
                zero_price_periods_by_leg[key] += 1

    dkk_keys = [k for k in leg_keys if leg_currency[k] == "DKK"]
    eur_keys = [k for k in leg_keys if leg_currency[k] == "EUR"]
    _assert_currency_partition(dkk_keys, eur_keys, leg_currency)

    dkk_up = [k for k in dkk_keys if leg_direction[k] in ("up", "symmetric")]
    dkk_down = [k for k in dkk_keys if leg_direction[k] in ("down", "symmetric")]
    eur_up = [k for k in eur_keys if leg_direction[k] in ("up", "symmetric")]
    eur_down = [k for k in eur_keys if leg_direction[k] in ("down", "symmetric")]

    # ---------------------------------------------------------------
    # Solve 1: energy dispatch (every configured energy market, DKK) +
    # DKK-currency capacity legs.
    # ---------------------------------------------------------------
    prob1 = pulp.LpProblem("bess_cooptimized_dkk", pulp.LpMaximize)

    ch: dict[str, list[pulp.LpVariable]] = {}
    dis: dict[str, list[pulp.LpVariable]] = {}
    for m in energy_markets:
        ch_vars = []
        dis_vars = []
        for i in range(T):
            ch_var = pulp.LpVariable(f"ch_{m}_{i}", lowBound=0)
            dis_var = pulp.LpVariable(f"dis_{m}_{i}", lowBound=0)
            if energy_schedule_price_at_t[m][i] is None:
                # No SCHEDULE price known this period for this energy
                # market -- never dispatch against it then (mirrors the
                # capacity legs' None-price handling below): pinned to 0
                # rather than left to an arbitrary zero-objective-
                # coefficient vertex, for a deterministic, reproducible
                # solve.
                ch_var.upBound = 0
                dis_var.upBound = 0
            ch_vars.append(ch_var)
            dis_vars.append(dis_var)
        ch[m] = ch_vars
        dis[m] = dis_vars

    # soc[0] is the fixed starting SoC (a plain number, not a variable);
    # soc[1..T] are the LP's SoC-at-end-of-period variables, bounded to the
    # usable band throughout.
    soc: list[float | pulp.LpVariable] = [starting_soc] + [
        pulp.LpVariable(f"soc_{i}", lowBound=soc_min, upBound=soc_max) for i in range(1, T + 1)
    ]

    cap_dkk: dict[str, list[pulp.LpVariable]] = {}
    for key in dkk_keys:
        variables = []
        for i in range(T):
            var = pulp.LpVariable(f"cap_{key.replace(':', '_')}_{i}", lowBound=0)
            if leg_schedule_price_at_t[key][i] is None:
                # No SCHEDULE price known this period -- never offer this
                # leg then (module docstring: pinned to 0 rather than left
                # to an arbitrary zero-objective-coefficient vertex, for a
                # deterministic, reproducible solve).
                var.upBound = 0
            variables.append(var)
        cap_dkk[key] = variables

    for i in range(T):
        total_ch_i = pulp.lpSum(ch[m][i] for m in energy_markets)
        total_dis_i = pulp.lpSum(dis[m][i] for m in energy_markets)
        # SoC balance, split round-trip efficiency, summed across every
        # energy market (module docstring).
        prob1 += soc[i + 1] == soc[i] + eta * total_ch_i * dt[i] - (total_dis_i * dt[i]) / eta
        # One shared power budget across every energy market and every DKK
        # leg.
        prob1 += (
            total_ch_i + total_dis_i + pulp.lpSum(cap_dkk[k][i] for k in dkk_keys)
            <= config.power_mw
        )
        # No-double-selling headroom, bound at BOTH the start-of-period SoC
        # (soc[i], before this period's own energy flows) and the
        # end-of-period SoC (soc[i+1], after them) -- see module docstring:
        # SoC moves monotonically within a period at constant power, so
        # binding both endpoints guarantees deliverability throughout the
        # whole period, closing the residual within-period double-sell a
        # start-only bound leaves open.
        prob1 += pulp.lpSum(cap_dkk[k][i] for k in dkk_up) * t_act <= (soc[i] - soc_min) * eta
        prob1 += pulp.lpSum(cap_dkk[k][i] for k in dkk_up) * t_act <= (soc[i + 1] - soc_min) * eta
        prob1 += pulp.lpSum(cap_dkk[k][i] for k in dkk_down) * t_act <= (soc_max - soc[i]) / eta
        prob1 += (
            pulp.lpSum(cap_dkk[k][i] for k in dkk_down) * t_act <= (soc_max - soc[i + 1]) / eta
        )

    if cap_mwh_per_window is not None:
        for i in range(T):
            prob1 += (
                pulp.lpSum(dis[m][j] * dt[j] for m in energy_markets for j in windows[i])
                <= cap_mwh_per_window
            )

    objective1 = pulp.lpSum(
        (energy_schedule_price_at_t[m][i] or 0.0) * (dis[m][i] - ch[m][i]) * dt[i]
        for m in energy_markets
        for i in range(T)
    )
    for key in dkk_keys:
        objective1 += pulp.lpSum(
            (leg_schedule_price_at_t[key][i] or 0.0) * cap_dkk[key][i] * dt[i] for i in range(T)
        )
    prob1 += objective1

    status1 = prob1.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status1] != "Optimal":
        raise RuntimeError(
            f"BESS co-optimizer Solve 1 (DKK) did not reach an optimal solution "
            f"(status={pulp.LpStatus[status1]!r}) for zone={zone!r}, window="
            f"[{start_time}, {end_time}]"
        )

    ch_star = {m: [pulp.value(v) or 0.0 for v in ch[m]] for m in energy_markets}
    dis_star = {m: [pulp.value(v) or 0.0 for v in dis[m]] for m in energy_markets}
    soc_star = [starting_soc] + [pulp.value(v) or 0.0 for v in soc[1:]]
    cap_dkk_star = {k: [pulp.value(v) or 0.0 for v in cap_dkk[k]] for k in dkk_keys}

    # ---------------------------------------------------------------
    # Solve 2 (only if any EUR-currency leg is configured): EUR-currency
    # capacity legs only, fit into the leftover power/headroom Solve 1's
    # already-fixed trajectory did not claim (module docstring). `ch_star`/
    # `dis_star`/`soc_star`/`cap_dkk_star` are plain numbers here, never LP
    # variables or objective terms -- no DKK quantity participates in this
    # solve's model at all.
    # ---------------------------------------------------------------
    cap_eur_star: dict[str, list[float]] = {}
    if eur_keys:
        prob2 = pulp.LpProblem("bess_cooptimized_eur", pulp.LpMaximize)
        cap_eur: dict[str, list[pulp.LpVariable]] = {}
        for key in eur_keys:
            variables = []
            for i in range(T):
                var = pulp.LpVariable(f"cap_{key.replace(':', '_')}_{i}", lowBound=0)
                if leg_schedule_price_at_t[key][i] is None:
                    var.upBound = 0
                variables.append(var)
            cap_eur[key] = variables

        for i in range(T):
            total_energy_flow_i = sum(ch_star[m][i] + dis_star[m][i] for m in energy_markets)
            dkk_committed = sum(cap_dkk_star[k][i] for k in dkk_keys)
            power_leftover = max(
                config.power_mw - total_energy_flow_i - dkk_committed, 0.0
            )
            prob2 += pulp.lpSum(cap_eur[k][i] for k in eur_keys) <= power_leftover

            # Leftover headroom, same start/end-of-period reasoning as
            # Solve 1 (module docstring): compute the allowance at both
            # soc_star[i] (start) and soc_star[i+1] (end) and take the
            # TIGHTER (minimum) of the two -- an EUR leg must stay
            # deliverable throughout the period against Solve 1's
            # already-fixed trajectory, not just at the period's start.
            dkk_up_committed = sum(cap_dkk_star[k][i] for k in dkk_up)
            up_headroom_leftover = min(
                max((soc_star[i] - soc_min) * eta - dkk_up_committed * t_act, 0.0),
                max((soc_star[i + 1] - soc_min) * eta - dkk_up_committed * t_act, 0.0),
            )
            prob2 += pulp.lpSum(cap_eur[k][i] for k in eur_up) * t_act <= up_headroom_leftover

            dkk_down_committed = sum(cap_dkk_star[k][i] for k in dkk_down)
            down_headroom_leftover = min(
                max((soc_max - soc_star[i]) / eta - dkk_down_committed * t_act, 0.0),
                max((soc_max - soc_star[i + 1]) / eta - dkk_down_committed * t_act, 0.0),
            )
            prob2 += pulp.lpSum(cap_eur[k][i] for k in eur_down) * t_act <= down_headroom_leftover

        objective2 = pulp.lpSum(
            (leg_schedule_price_at_t[key][i] or 0.0) * cap_eur[key][i] * dt[i]
            for key in eur_keys
            for i in range(T)
        )
        prob2 += objective2

        status2 = prob2.solve(pulp.PULP_CBC_CMD(msg=False))
        if pulp.LpStatus[status2] != "Optimal":
            raise RuntimeError(
                f"BESS co-optimizer Solve 2 (EUR) did not reach an optimal solution "
                f"(status={pulp.LpStatus[status2]!r}) for zone={zone!r}, window="
                f"[{start_time}, {end_time}]"
            )
        cap_eur_star = {k: [pulp.value(v) or 0.0 for v in cap_eur[k]] for k in eur_keys}
    else:
        cap_eur_star = {}

    # ---------------------------------------------------------------
    # Walk the combined solution into one BessTick per period. Every
    # reported revenue figure re-values the FIXED schedule (ch_star/
    # dis_star/cap_dkk_star/cap_eur_star) at SETTLEMENT prices (P3) --
    # in perfect mode settlement == schedule, so this is a no-op
    # re-valuation and every number is identical to P1/P2.
    # ---------------------------------------------------------------
    ticks: list[BessTick] = []
    cumulative_arbitrage = 0.0
    cumulative_capacity_dkk = 0.0
    cumulative_capacity_eur = 0.0
    cumulative_afrr_activation = 0.0

    for i in range(T):
        ch_i = sum(ch_star[m][i] for m in energy_markets)
        dis_i = sum(dis_star[m][i] for m in energy_markets)
        if dis_i > _EPS and dis_i >= ch_i:
            action = "discharge"
        elif ch_i > _EPS:
            action = "charge"
        else:
            action = "idle"

        energy_discharged_mwh = dis_i * dt[i] if action == "discharge" else 0.0
        arbitrage_revenue = sum(
            (energy_settlement_price_at_t[m][i] or 0.0)
            * (dis_star[m][i] - ch_star[m][i])
            * dt[i]
            for m in energy_markets
        )
        cumulative_arbitrage += arbitrage_revenue

        capacity_revenue_by_market: dict[str, float] = {}
        capacity_reserved_mw = 0.0
        capacity_revenue_dkk_tick = 0.0
        capacity_revenue_eur_tick = 0.0
        afrr_committed_mw = 0.0

        for key in dkk_keys:
            mw = cap_dkk_star[key][i]
            capacity_reserved_mw += mw
            revenue = (leg_settlement_price_at_t[key][i] or 0.0) * mw * dt[i]
            capacity_revenue_by_market[key] = revenue
            capacity_revenue_dkk_tick += revenue
            if key.split(":", 1)[0] == "aFRR_capacity":
                afrr_committed_mw += mw
        for key in eur_keys:
            mw = cap_eur_star[key][i]
            capacity_reserved_mw += mw
            revenue = (leg_settlement_price_at_t[key][i] or 0.0) * mw * dt[i]
            capacity_revenue_by_market[key] = revenue
            capacity_revenue_eur_tick += revenue
            if key.split(":", 1)[0] == "aFRR_capacity":
                afrr_committed_mw += mw

        cumulative_capacity_dkk += capacity_revenue_dkk_tick
        cumulative_capacity_eur += capacity_revenue_eur_tick

        # aFRR activation revenue (module docstring): a derived bonus from
        # whichever solve committed the aFRR_capacity leg(s), never itself
        # part of either solve's objective -- and, unlike every other
        # revenue figure here, never has a schedule/settlement split
        # either (module docstring's post/pre section): `activation_price_series`
        # is always the actual/settlement series, in both foresight modes.
        activation_price = (
            _value_at_or_before(activation_price_series, times[i]) if afrr_committed_mw else None
        )
        afrr_activation_revenue = (
            activation_price
            * afrr_committed_mw
            * config.afrr_activation_participation_rate
            * dt[i]
            if activation_price is not None
            else 0.0
        )
        cumulative_afrr_activation += afrr_activation_revenue

        cycle_cap_binding = False
        if cap_mwh_per_window is not None:
            rolling_discharged = sum(
                dis_star[m][j] * dt[j] for m in energy_markets for j in windows[i]
            )
            cycle_cap_binding = rolling_discharged >= cap_mwh_per_window - _EPS

        ticks.append(
            BessTick(
                time=times[i],
                soc_mwh=soc_star[i + 1],
                soc_fraction=soc_star[i + 1] / config.capacity_mwh if config.capacity_mwh else 0.0,
                action=action,
                day_ahead_price=prices[i],
                energy_discharged_mwh=energy_discharged_mwh,
                arbitrage_revenue_dkk=arbitrage_revenue,
                capacity_reserved_mw=capacity_reserved_mw,
                capacity_revenue_dkk=capacity_revenue_dkk_tick,
                capacity_revenue_by_market=capacity_revenue_by_market,
                cumulative_arbitrage_revenue_dkk=cumulative_arbitrage,
                cumulative_capacity_revenue_dkk=cumulative_capacity_dkk,
                cumulative_total_revenue_dkk=cumulative_arbitrage + cumulative_capacity_dkk,
                cycle_cap_binding=cycle_cap_binding,
                afrr_activation_revenue_eur=afrr_activation_revenue,
                cumulative_afrr_activation_revenue_eur=cumulative_afrr_activation,
                capacity_revenue_eur=capacity_revenue_eur_tick,
                cumulative_capacity_revenue_eur=cumulative_capacity_eur,
            )
        )

    return BacktestResult(
        zone=zone,
        start_time=start_time,
        end_time=end_time,
        config=config,
        ticks=ticks,
        zero_price_periods_by_leg=dict(zero_price_periods_by_leg),
        capacity_allocation_fell_back_to_even=False,
    )
