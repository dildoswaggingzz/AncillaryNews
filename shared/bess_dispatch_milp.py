"""
Perfect-foresight ("post") linear-program co-optimizer for BESS dispatch.

`shared/bess_simulator.py:run_backtest`'s `"threshold"` strategy computes
energy arbitrage, capacity reservation, and aFRR activation as three
**independent** revenue streams and sums them -- a real battery has *one*
power rating and *one* state-of-charge, and every market competes for the
same MW and the same MWh (see that module's docstring §0 for the exact
defects this causes, most importantly *double-selling*: booking capacity
payments for MW the battery has no stored energy left to actually deliver).
This module fixes that by solving **one linear program per backtest
window**, over *actual* historical prices (a perfect-foresight oracle, not a
deployable policy -- see `docs/bess-cooptimizer-design.md` §5 for the
post/pre distinction; only "post" is built here, P1).

Exposed through the *existing* `run_backtest` entry point as
`BessConfig(strategy="cooptimized")` -- `run_backtest` still owns every DB
call (this module is pure: no DB, no network, so it is unit-testable with
synthetic series) and passes the already-fetched series in.

**The LP** (docs/bess-cooptimizer-design.md §2). Periods `t = 0..T-1` over
the day-ahead price timeline (same period/dt_hours convention as the
threshold engine). Decision variables, all >= 0: `ch[t]`/`dis[t]` (grid
charge/discharge power, MW), `soc[t]` (state of charge, MWh), and one
`cap[m, t]` per configured capacity leg `m` (MW committed that period).
Constraints: SoC balance with split round-trip efficiency
(`leg_efficiency = round_trip_efficiency ** 0.5`); the usable SoC band;
ONE shared power budget `ch[t] + dis[t] + sum(cap[m, t] for m) <=
power_mw`; and the no-double-selling headroom bound -- committed up-reserve
must be deliverable out of currently stored energy for
`activation_endurance_hours` (`T_act`, an *energy-endurance* duration, not a
ramp time -- a BESS ramps in seconds; see `BessConfig.activation_endurance_hours`'s
docstring), and committed down-reserve must have room to absorb. **The
reference rule this implements -- "subtract committed net position before
offering capacity" -- means the reserve must stay deliverable for the
*whole* period, not just at its start**: `soc` moves monotonically within a
period (charge/discharge power is constant over `[t, t+1)`), so binding the
headroom bound at *both* the start-of-period SoC (`soc[t]`) and the
end-of-period SoC (`soc[t+1]`, i.e. after that period's own committed
arbitrage flow has been applied) is sufficient to guarantee deliverability
throughout the whole period -- a single start-only bound leaves a residual
within-period double-sell (the arbitrage leg discharges toward `soc_min`
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
is set, mirroring the threshold engine's cycle cap (arbitrage discharge
only -- capacity commitments are not "cycled" in this estimate, same as
the threshold engine).

No binary variable is needed to forbid simultaneous charge/discharge:
`round_trip_efficiency < 1` makes any `ch[t] > 0 and dis[t] > 0` strictly
revenue-losing (paying the round-trip loss for no price gain), so the LP
relaxation's optimum never does it (docs/bess-cooptimizer-design.md §3).

**Currency decomposition (docs/bess-cooptimizer-design.md §4) -- P1's
choice.** A single scalar LP objective can never contain both a DKK term
and a EUR term without implicitly asserting some exchange rate between
them -- exactly the unit-mixing bug `shared/units.py` and the threshold
engine's per-currency buckets exist to prevent. Day-ahead arbitrage is
unconditionally DKK (the only `price_market` this module reads is
DKK-denominated per `shared/datasets.py`'s registry), so it can never be
combined with a EUR capacity term in one objective either. This module
therefore solves **two separate LPs that share the battery's physical
trajectory only through the DKK energy leg**, not two independent
optimizations of the same physical battery:

1.  **Solve 1 (DKK)** -- variables `ch`, `dis`, `soc`, and `cap[m, t]` for
    every DKK-currency leg. Objective: arbitrage revenue + DKK capacity
    revenue. Subject to the full power budget and headroom bounds (using
    only the DKK legs, since EUR legs do not exist in this solve at all).
    This solve's `ch`/`dis`/`soc` trajectory is authoritative for every
    tick's `action`/`soc_mwh`/`energy_discharged_mwh`/
    `arbitrage_revenue_dkk` fields -- there is exactly one physical
    trajectory reported, never two competing ones.
2.  **Solve 2 (EUR)**, only built if any EUR-currency leg is configured --
    variables `cap[m, t]` for every EUR-currency leg *only*. `ch`, `dis`,
    and `soc` are no longer variables here: they are the fixed numeric
    values Solve 1 already committed to, so Solve 2's power-budget and
    headroom constraints use *leftover* numeric bounds (`power_mw` minus
    Solve 1's `ch[t] + dis[t] + sum(DKK cap[m, t])`, and the DKK legs'
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
    trajectory, exactly like a same-currency leg would have to.

This is a deliberate, documented simplification, not the only valid
decomposition (docs/bess-cooptimizer-design.md §4 leaves the exact choice
to P1): Solve 1 does not "know about" the EUR legs' revenue potential when
deciding how much headroom/power to leave for arbitrage vs. its own DKK
legs, so the combined result is not necessarily the *global* joint optimum
across both currencies -- but it is always feasible (no double-selling,
ever, across the whole stack, since Solve 2's bounds are literally what's
left over after Solve 1's real commitments) and it never mixes currency
magnitudes. **aFRR activation revenue** (module docstring's aFRR_energy
price, always EUR per the registry, see
`shared/bess_simulator.py` module docstring §3) is **not** optimized in
either solve's objective -- like the threshold engine, it is a derived
bonus computed *after* solving, from whichever solve committed the
`"aFRR_capacity"` leg(s) (that market's own currency, resolved via
`leg_currency`, determines which solve that is), so it never has to be
weighed against a DKK or EUR capacity price inside an objective either.

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


def solve_cooptimized_dispatch(
    zone: str,
    start_time: datetime,
    end_time: datetime,
    config: BessConfig,
    price_series: list[tuple[datetime, float]],
    capacity_series_by_leg: dict[str, list[tuple[datetime, float]]],
    leg_currency: dict[str, str],
    activation_price_series: list[tuple[datetime, float]],
) -> BacktestResult:
    """
    Solves the perfect-foresight co-optimized dispatch LP (module
    docstring) over already-fetched series and returns a `BacktestResult`
    identical in shape to the threshold engine's -- one `BessTick` per
    `price_series` point, same fields, so `save_bess_run` and the dashboard
    consume it unchanged. Pure: no DB access, no network -- every input is
    an in-memory series, so this function is unit-testable with synthetic
    data (see `tests/test_bess_dispatch_milp.py`).

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

    leg_keys = list(capacity_series_by_leg.keys())
    leg_direction: dict[str, Literal["up", "down", "symmetric"]] = {
        key: _leg_direction(*key.split(":", 1)) for key in leg_keys
    }
    # Per-leg, per-period clearing price (None where no price is available
    # that period) -- computed once, reused by both solves' objectives and
    # by the zero-price-period accounting below.
    leg_price_at_t: dict[str, list[float | None]] = {
        key: [_value_at_or_before(capacity_series_by_leg[key], t) for t in times]
        for key in leg_keys
    }

    zero_price_periods_by_leg: dict[str, int] = defaultdict(int)
    for key in leg_keys:
        for price in leg_price_at_t[key]:
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
    # Solve 1: arbitrage (DKK) + DKK-currency capacity legs.
    # ---------------------------------------------------------------
    prob1 = pulp.LpProblem("bess_cooptimized_dkk", pulp.LpMaximize)

    ch = [pulp.LpVariable(f"ch_{i}", lowBound=0) for i in range(T)]
    dis = [pulp.LpVariable(f"dis_{i}", lowBound=0) for i in range(T)]
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
            if leg_price_at_t[key][i] is None:
                # No clearing price known this period -- never offer this
                # leg then (module docstring: pinned to 0 rather than left
                # to an arbitrary zero-objective-coefficient vertex, for a
                # deterministic, reproducible solve).
                var.upBound = 0
            variables.append(var)
        cap_dkk[key] = variables

    for i in range(T):
        # SoC balance, split round-trip efficiency (module docstring).
        prob1 += soc[i + 1] == soc[i] + eta * ch[i] * dt[i] - (dis[i] * dt[i]) / eta
        # One shared power budget across arbitrage and every DKK leg.
        prob1 += (
            ch[i] + dis[i] + pulp.lpSum(cap_dkk[k][i] for k in dkk_keys) <= config.power_mw
        )
        # No-double-selling headroom, bound at BOTH the start-of-period SoC
        # (soc[i], before this period's own charge/discharge) and the
        # end-of-period SoC (soc[i+1], after it) -- see module docstring:
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
            prob1 += pulp.lpSum(dis[j] * dt[j] for j in windows[i]) <= cap_mwh_per_window

    objective1 = pulp.lpSum(prices[i] * (dis[i] - ch[i]) * dt[i] for i in range(T))
    for key in dkk_keys:
        objective1 += pulp.lpSum(
            (leg_price_at_t[key][i] or 0.0) * cap_dkk[key][i] * dt[i] for i in range(T)
        )
    prob1 += objective1

    status1 = prob1.solve(pulp.PULP_CBC_CMD(msg=False))
    if pulp.LpStatus[status1] != "Optimal":
        raise RuntimeError(
            f"BESS co-optimizer Solve 1 (DKK) did not reach an optimal solution "
            f"(status={pulp.LpStatus[status1]!r}) for zone={zone!r}, window="
            f"[{start_time}, {end_time}]"
        )

    ch_star = [pulp.value(v) or 0.0 for v in ch]
    dis_star = [pulp.value(v) or 0.0 for v in dis]
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
                if leg_price_at_t[key][i] is None:
                    var.upBound = 0
                variables.append(var)
            cap_eur[key] = variables

        for i in range(T):
            dkk_committed = sum(cap_dkk_star[k][i] for k in dkk_keys)
            power_leftover = max(config.power_mw - ch_star[i] - dis_star[i] - dkk_committed, 0.0)
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
            (leg_price_at_t[key][i] or 0.0) * cap_eur[key][i] * dt[i]
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
    # Walk the combined solution into one BessTick per period.
    # ---------------------------------------------------------------
    ticks: list[BessTick] = []
    cumulative_arbitrage = 0.0
    cumulative_capacity_dkk = 0.0
    cumulative_capacity_eur = 0.0
    cumulative_afrr_activation = 0.0

    for i in range(T):
        ch_i, dis_i = ch_star[i], dis_star[i]
        if dis_i > _EPS and dis_i >= ch_i:
            action = "discharge"
        elif ch_i > _EPS:
            action = "charge"
        else:
            action = "idle"

        energy_discharged_mwh = dis_i * dt[i] if action == "discharge" else 0.0
        arbitrage_revenue = prices[i] * (dis_i - ch_i) * dt[i]
        cumulative_arbitrage += arbitrage_revenue

        capacity_revenue_by_market: dict[str, float] = {}
        capacity_reserved_mw = 0.0
        capacity_revenue_dkk_tick = 0.0
        capacity_revenue_eur_tick = 0.0
        afrr_committed_mw = 0.0

        for key in dkk_keys:
            mw = cap_dkk_star[key][i]
            capacity_reserved_mw += mw
            revenue = (leg_price_at_t[key][i] or 0.0) * mw * dt[i]
            capacity_revenue_by_market[key] = revenue
            capacity_revenue_dkk_tick += revenue
            if key.split(":", 1)[0] == "aFRR_capacity":
                afrr_committed_mw += mw
        for key in eur_keys:
            mw = cap_eur_star[key][i]
            capacity_reserved_mw += mw
            revenue = (leg_price_at_t[key][i] or 0.0) * mw * dt[i]
            capacity_revenue_by_market[key] = revenue
            capacity_revenue_eur_tick += revenue
            if key.split(":", 1)[0] == "aFRR_capacity":
                afrr_committed_mw += mw

        cumulative_capacity_dkk += capacity_revenue_dkk_tick
        cumulative_capacity_eur += capacity_revenue_eur_tick

        # aFRR activation revenue (module docstring): a derived bonus from
        # whichever solve committed the aFRR_capacity leg(s), never itself
        # part of either solve's objective.
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
            rolling_discharged = sum(dis_star[j] * dt[j] for j in windows[i])
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
