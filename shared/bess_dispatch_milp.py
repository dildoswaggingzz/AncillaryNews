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
distinction and §6 for the multi-market energy stack (day-ahead + imbalance,
P3; intraday, P4b).

Exposed through the *existing* `run_backtest` entry point as
`BessConfig(strategy="cooptimized")` -- `run_backtest` still owns every DB
call (this module is pure: no DB, no network, so it is unit-testable with
synthetic series) and passes the already-fetched series in.

**The single joint LP (docs/bess-cooptimizer-design.md §4/§4.2, P4 final
design).** Periods `t = 0..T-1` over the day-ahead price timeline (same
period/dt_hours convention as the threshold engine). Decision variables,
all >= 0: `ch[e, t]`/`dis[e, t]` (grid charge/discharge power, MW, one pair
per configured energy market `e` -- `BessConfig.energy_markets`, e.g.
`"day_ahead"` and, as of P3, `"imbalance"`), `soc[t]` (state of charge,
MWh), and one `cap[m, t]` per configured capacity leg `m` (MW committed
that period, regardless of currency). There is exactly ONE LP, ONE shared
power/SoC/headroom budget, covering EVERY energy market and EVERY capacity
leg together, no matter which currency each is denominated in:

    soc[t+1] = soc[t] + eta * sum(ch[e, t] for e) * dt[t]
                       - sum(dis[e, t] for e) * dt[t] / eta

    sum(ch[e, t] + dis[e, t] for e) + sum(cap[m, t] for m) <= power_mw

and the no-double-selling headroom bound -- committed up-reserve must be
deliverable out of currently stored energy for `activation_endurance_hours`
(`T_act`, an *energy-endurance* duration, not a ramp time -- a BESS ramps in
seconds; see `BessConfig.activation_endurance_hours`'s docstring), and
committed down-reserve must have room to absorb. **The reference rule this
implements -- "subtract committed net position before offering capacity" --
means the reserve must stay deliverable for the *whole* period, not just at
its start**: `soc` moves monotonically within a period (net charge/discharge
power is constant over `[t, t+1)`), so binding the headroom bound at *both*
the start-of-period SoC (`soc[t]`) and the end-of-period SoC (`soc[t+1]`,
i.e. after that period's own committed energy flows have been applied) is
sufficient to guarantee deliverability throughout the whole period -- a
single start-only bound leaves a residual within-period double-sell:

    sum(cap[m, t] for m in up_legs)   * T_act <= (soc[t]   - soc_min) * eta
    sum(cap[m, t] for m in up_legs)   * T_act <= (soc[t+1] - soc_min) * eta
    sum(cap[m, t] for m in down_legs) * T_act <= (soc_max - soc[t])   / eta
    sum(cap[m, t] for m in down_legs) * T_act <= (soc_max - soc[t+1]) / eta

`up_legs`/`down_legs` here span EVERY currency's legs together -- a DKK
up-reserve and a EUR up-reserve compete for the exact same physical
up-headroom, since both draw on the same stored energy. A leg's direction
(`_leg_direction`) is resolved from its product string: `"up"`/`"down"` are
directional (e.g. DK2's FCR-D pair, aFRR capacity); `"price"` is
**symmetric** (FCR-N/DK1's single FCR price, FFR) -- one `cap[m, t]`
variable obligates *both* the up- and down-headroom sums at once, but is
paid for (and counted against the power budget) exactly once, matching the
physical reality of a single symmetric reserve band. A rolling 24-hour cap
on discharge energy is added when `config.max_cycles_per_day` is set,
summed across every energy market's discharge (a physical duty-cycle limit
on the battery, not a per-market one), mirroring the threshold engine's
cycle cap.

No binary variable is needed to forbid simultaneous charge/discharge on any
one energy market: `round_trip_efficiency < 1` makes any `ch[e, t] > 0 and
dis[e, t] > 0` for the SAME `e` strictly revenue-losing (paying the
round-trip loss for no price gain), so the LP relaxation's optimum never
does it (docs/bess-cooptimizer-design.md §3). **This argument does NOT
extend across DIFFERENT energy markets**: with >= 2 energy markets, the pure
LP relaxation can set `ch[e1, t] > 0` AND `dis[e2, t] > 0` in the same
period (buy the cheap market, sell the dear one) with a net-zero SoC change
-- an unphysical "pass-through" bounded only by `power_mw` (the shared power
budget above still holds), not by any stored energy at all, since the two
flows' SoC effects cancel. A single-inverter BESS cannot charge and
discharge at once, so when `len(config.energy_markets) > 1` this module adds
one binary `is_dis[t] in {0, 1}` per period and two big-M constraints
(`M = power_mw`, since every `ch`/`dis` variable is already bounded by the
shared power budget): `sum(ch[e,t] for e) <= power_mw * (1 - is_dis[t])` and
`sum(dis[e,t] for e) <= power_mw * is_dis[t]`. This forbids simultaneous
cross-market charge+discharge while still letting the LP freely ROUTE a
charge period's flow, or a discharge period's flow, across whichever
energy markets pay best that period. The single-energy-market case (the
default, `energy_markets = ("day_ahead",)`) stays a pure LP -- no binary --
since the round-trip-efficiency argument above is sufficient there; only
`len(energy_markets) > 1` becomes a MILP. That MILP is solved with **HiGHS**
(one binary per period is far too slow for CBC's branch-and-bound over a
month/quarter -- tens of seconds to minutes -- whereas HiGHS solves it in
~1s); the pure-LP single-market path keeps PuLP's bundled CBC unchanged.
Both are deterministic for a fixed model, so `save_bess_run`'s reproducibility
contract is unaffected either way.

**Multiple dispatchable energy markets (P3, docs/bess-cooptimizer-design.md
§6).** Day-ahead alone (`BessConfig.energy_markets = ("day_ahead",)`, the
default) is the single-energy-market case. Adding `"imbalance"` lets the LP
route discharge to whichever of {day-ahead, imbalance} pays more and charge
to whichever costs less, each period, subject to the ONE shared power/SoC
budget above -- this models a BESS as the *controllable* asset it is:
unlike a wind farm (which merely settles a forecast-error deviation against
the imbalance price), a battery *chooses* its imbalance exposure, so it
belongs alongside day-ahead as a second dispatchable price, not a passive
settlement stream. `BessTick.arbitrage_revenue_dkk` remains the TOTAL
energy revenue across every configured energy market that tick (the
field's meaning is unchanged: "energy arbitrage revenue"); a per-market
split is not persisted (`capacity_revenue_by_market`'s dict shape is a
*capacity-leg* concept and is deliberately not reused for this).

**Post vs. pre foresight (P3, docs/bess-cooptimizer-design.md §5).** Every
price series this module optimises against -- every energy market, every
capacity leg, and (P4) activation -- has two roles: a **schedule** price
(what the LP's objective is built from, i.e. what decides
`ch`/`dis`/`cap`) and a **settlement** price (what the resulting FIXED
schedule's revenue is actually reported at, on every `BessTick`).
`BessConfig.foresight == "perfect"` (the default) passes the same
actual/realised series for both roles, so nothing changes from P1/P2's
behaviour byte-for-byte. `foresight == "forecast"` (pre mode) has
`run_backtest` build a causal lag-24h-persistence forecast of each
schedulable series (`shared/bess_simulator.py:_lag24h_forecast`) and pass
it in as the `schedule_*` parameters below -- so the LP schedules against
what was *expected*, and every tick's reported revenue is
`Σ actual_price · scheduled_flow`, the realistic "you bid on your
forecast, you get paid what happened" evaluation. The post − pre gap is the
monetary value of forecast skill, and is guaranteed `>= 0`: the fixed
forecast-driven schedule is itself one feasible schedule the
perfect-foresight problem could also have chosen (identical physical
constraints either way), so its actual-settled value can never exceed the
perfect-foresight optimum (tests/test_bess_dispatch_milp.py's `pre <= post`
gate).

**Currency discipline -- one joint LP, pegged in the objective (P4 final
design, docs/bess-cooptimizer-design.md §4/§4.2).** Two distinct concerns
the earlier phases conflated:

- **Reporting** stays per-currency and native, exactly as it always has:
  `capacity_revenue_by_market`/`capacity_revenue_dkk`/`_eur` bucket each
  leg's revenue by its OWN registry-declared currency, never converted;
  `afrr_activation_revenue_eur` stays EUR-native. `arbitrage_revenue_dkk`
  is the one field that DOES combine currencies -- energy revenue in a
  DKK-denominated market is added as-is, energy revenue in a
  EUR-denominated market is converted at the fixed
  `shared.units.DKK_PER_EUR` peg first -- a reporting-boundary conversion
  identical in spirit to `BacktestResult.total_revenue_all_dkk`'s §4.1 peg,
  never a market-rate assumption (the peg is a POLICY-held ERM II rate, not
  a floating variable -- see §4's "why this is not the bug §2 guards
  against").
- **The objective** is the one thing that changed: it expresses EVERY term
  -- every energy market, every capacity leg (any currency), and (P4)
  activation -- in a single common currency (DKK), converting any EUR term
  at the same fixed peg (`_peg_factor`). All of them compete for the ONE
  shared power/SoC/headroom budget on equal footing, inside one LP.

> **Design evolution (recorded, not hidden).** P1-P3 used a **two-solve
> decomposition** (one LP per currency, sharing the physical trajectory)
> specifically to keep the peg *out* of the objective. A later P4a
> iteration moved the energy leg to EUR and exposed the flaw: whichever
> currency doesn't own the energy leg gets its capacity **crowded to ~0**
> (an artifact of solving greedily in sequence, not a true optimum -- DK1,
> all-DKK-capacity, lost roughly half its co-optimized total to this in
> testing). This module now solves the **single joint pegged LP** instead:
> the true global optimum, with no crowd-out artifact in either currency,
> and simpler (no leftover-bounds bookkeeping, no primary/secondary split,
> no per-run currency switch). `tests/test_bess_dispatch_milp.py` includes
> a regression proving a DKK capacity leg is no longer starved by an EUR
> (or any other) leg's presence.

**Activation joins the objective (P4).** Earlier phases treated aFRR
activation revenue as a pure reporting bonus, never influencing the LP's
own decisions (since it wasn't in either solve's objective). The single
joint LP now includes it directly: `activation_price · cap[aFRR_capacity
leg(s), t] · participation_rate · dt[t]`, peg-converted to DKK like every
other term (the ingested `aFRR_energy` activation price is EUR-native),
summed over every configured `"aFRR_capacity"` leg regardless of direction
(mirroring the threshold engine's own `commit_per_group_this_tick.get(
"aFRR_capacity", 0.0)` convention -- up and down legs' committed MW both
count). This lets the LP correctly value committing more aFRR_capacity for
the activation upside it unlocks, not just its own capacity price. Reported
`afrr_activation_revenue_eur` is still computed at SETTLEMENT prices,
unconverted (EUR-native, exactly as before) -- only the objective's
internal accounting uses the peg.

Solved with PuLP's bundled CBC backend (`pulp.PULP_CBC_CMD`) -- a pure LP
(no integer variables) for the single-energy-market case, or a MILP with one
binary per period (the cross-market simultaneous-charge/discharge guard
above) when `len(config.energy_markets) > 1`. Either way CBC's solve is
deterministic for a fixed model, preserving `save_bess_run`'s
reproducibility contract (the same persisted `config` re-solves to the same
dispatch).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

import pulp

from shared.bess_simulator import BacktestResult, BessConfig, BessTick, _value_at_or_before
from shared.units import DKK_PER_EUR, currency_for

# Numeric tolerance for classifying a period's action from `ch`/`dis` (which
# should never both be meaningfully positive at the LP optimum -- see module
# docstring's "no binary needed" paragraph -- but a simplex solve can leave
# a variable at a tiny nonzero residual well below solver tolerance) and for
# clipping degenerate near-zero bounds elsewhere.
_EPS = 1e-6


def _peg_factor(currency: str) -> float:
    """
    DKK-equivalent conversion factor for one currency, at the fixed ERM II
    peg (module docstring's currency-discipline section) -- `1.0` for DKK
    (already native, no conversion needed), `shared.units.DKK_PER_EUR` for
    EUR. Used ONLY inside the single joint LP's objective, to let energy
    markets and capacity legs of different currencies compete for the same
    shared budget on equal footing; every REPORTING bucket
    (`capacity_revenue_dkk`/`_eur`, `afrr_activation_revenue_eur`) stays
    native and unconverted, never calling this. Raises `ValueError` for
    anything other than `"DKK"`/`"EUR"` -- every currency reaching this
    point should already have been validated by `run_backtest`'s registry
    check.
    """
    if currency == "DKK":
        return 1.0
    if currency == "EUR":
        return DKK_PER_EUR
    raise ValueError(f"unknown currency {currency!r} -- expected 'DKK' or 'EUR'")


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
    Unaffected by P3 (multi-market energy, post/pre foresight) or P4 (the
    co-optimizer's single joint pegged LP) -- the threshold engine this
    function replays stays DKK day-ahead-only (its `price_market`/
    `price_product` path is untouched by any of these phases) and has no
    schedule/settlement split, no currency peg, and no LP-decomposition
    concept at all; this function only ever reads that threshold trace's
    own `capacity_revenue_by_market` and the capacity legs' own settlement
    prices, neither of which the co-optimizer's internal objective design
    touches.

    For each tick, per configured leg, the maximum MW that leg could
    honestly have committed is bounded by the same headroom rule, evaluated
    at whichever of the tick's start-of-period SoC (`prev_soc` -- the
    previous tick's `soc_mwh`, or `config.starting_soc_fraction *
    capacity_mwh` for the very first tick) and end-of-period SoC
    (`tick.soc_mwh`) is TIGHTER for that direction (mirroring the LP's own
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
    energy_currency: dict[str, str] | None = None,
    schedule_energy_series_by_market: dict[str, list[tuple[datetime, float]]] | None = None,
    schedule_capacity_series_by_leg: dict[str, list[tuple[datetime, float]]] | None = None,
    schedule_activation_price_series: list[tuple[datetime, float]] | None = None,
) -> BacktestResult:
    """
    Solves the single-joint-LP co-optimized dispatch (module docstring) over
    already-fetched series and returns a `BacktestResult` identical in
    shape to the threshold engine's -- one `BessTick` per `price_series`
    point, same fields, so `save_bess_run` and the dashboard consume it
    unchanged. Pure: no DB access, no network -- every input is an
    in-memory series, so this function is unit-testable with synthetic
    data (see `tests/test_bess_dispatch_milp.py`).

    `price_series` still drives the tick timeline (`times`/`dt`/`T`) and
    the `day_ahead_price` tick field, exactly as in P1/P2 -- unchanged.

    `energy_series_by_market`/`energy_currency` (P3/P4): every energy
    market's actual/settlement series and its registry currency, keyed by
    market name (`config.energy_markets`). Both default to `None`, in which
    case (backward-compatible with every P1/P2 call site, including this
    module's own tests) they are built as
    `{config.energy_markets[0]: price_series}` / `{config.energy_markets[0]:
    "DKK"}` -- only valid when `energy_markets` has exactly one entry (a
    caller configuring more than one energy market MUST pass both
    explicitly; there is no way to guess which series/currency is which
    from `price_series` alone).

    `schedule_energy_series_by_market`/`schedule_capacity_series_by_leg`/
    `schedule_activation_price_series` (P3/P4, post/pre foresight -- module
    docstring): the series the LP's OBJECTIVE is built from (what decides
    `ch`/`dis`/`cap`), as opposed to `energy_series_by_market`/
    `capacity_series_by_leg`/`activation_price_series` (the SETTLEMENT
    series every tick's reported revenue is computed from, once the
    schedule is fixed). All default to `None`, meaning "same as settlement"
    -- `BessConfig.foresight == "perfect"`'s behaviour, and byte-identical
    to omitting them entirely (tests/test_bess_dispatch_milp.py covers this
    explicitly).

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

    # --- energy markets (module docstring) --------------------------------
    if energy_series_by_market is None:
        if len(config.energy_markets) != 1:
            raise ValueError(
                "energy_series_by_market must be provided explicitly when "
                f"config.energy_markets has more than one entry (got {config.energy_markets!r})"
            )
        energy_series_by_market = {config.energy_markets[0]: price_series}
        if energy_currency is None:
            energy_currency = {config.energy_markets[0]: "DKK"}
    _assert_energy_markets_match(config.energy_markets, energy_series_by_market)
    if energy_currency is None:
        raise ValueError(
            "energy_currency must be provided explicitly alongside an explicit "
            "energy_series_by_market"
        )
    if set(energy_currency) != set(config.energy_markets):
        raise ValueError(
            f"energy_currency's keys {sorted(energy_currency)!r} must exactly match "
            f"config.energy_markets {sorted(config.energy_markets)!r}"
        )
    energy_markets = list(config.energy_markets)
    energy_peg: dict[str, float] = {m: _peg_factor(energy_currency[m]) for m in energy_markets}

    # --- schedule vs. settlement (module docstring) -- perfect mode (the
    # default) makes both identical, so nothing below changes from P1/P2's
    # behaviour. ---
    if schedule_energy_series_by_market is None:
        schedule_energy_series_by_market = energy_series_by_market
    if schedule_capacity_series_by_leg is None:
        schedule_capacity_series_by_leg = capacity_series_by_leg
    if schedule_activation_price_series is None:
        schedule_activation_price_series = activation_price_series

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
    leg_peg: dict[str, float] = {key: _peg_factor(leg_currency[key]) for key in leg_keys}

    # Per-leg, per-period clearing price -- SETTLEMENT (what every tick's
    # reported revenue, and `zero_price_periods_by_leg`, is computed from)
    # and SCHEDULE (what the LP's objective is built from, i.e. what
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

    up_legs = [k for k in leg_keys if leg_direction[k] in ("up", "symmetric")]
    down_legs = [k for k in leg_keys if leg_direction[k] in ("down", "symmetric")]
    aFRR_capacity_legs = [k for k in leg_keys if k.split(":", 1)[0] == "aFRR_capacity"]

    activation_settlement_price_at_t = [
        _value_at_or_before(activation_price_series, t) for t in times
    ]
    activation_schedule_price_at_t = [
        _value_at_or_before(schedule_activation_price_series, t) for t in times
    ]

    # ---------------------------------------------------------------
    # The single joint LP: every energy market and every capacity leg
    # (any currency) share ONE power/SoC/headroom budget (module docstring).
    # ---------------------------------------------------------------
    prob = pulp.LpProblem("bess_cooptimized", pulp.LpMaximize)

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

    # Cross-market simultaneous charge/discharge guard (module docstring's
    # "no binary needed" paragraph): only needed with >= 2 energy markets --
    # the single-market case relies on round_trip_efficiency < 1 alone, and
    # stays a pure LP (no binary variables at all).
    multi_energy_market = len(energy_markets) > 1
    is_dis: list[pulp.LpVariable] | None = None
    if multi_energy_market:
        is_dis = [pulp.LpVariable(f"is_dis_{i}", cat="Binary") for i in range(T)]

    cap: dict[str, list[pulp.LpVariable]] = {}
    for key in leg_keys:
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
        cap[key] = variables

    for i in range(T):
        total_ch_i = pulp.lpSum(ch[m][i] for m in energy_markets)
        total_dis_i = pulp.lpSum(dis[m][i] for m in energy_markets)
        # SoC balance, split round-trip efficiency, summed across every
        # energy market (module docstring).
        prob += soc[i + 1] == soc[i] + eta * total_ch_i * dt[i] - (total_dis_i * dt[i]) / eta
        # ONE shared power budget across every energy market and EVERY
        # capacity leg, any currency.
        prob += (
            total_ch_i + total_dis_i + pulp.lpSum(cap[k][i] for k in leg_keys) <= config.power_mw
        )
        # No-double-selling headroom, bound at BOTH the start-of-period SoC
        # (soc[i], before this period's own energy flows) and the
        # end-of-period SoC (soc[i+1], after them) -- see module docstring:
        # SoC moves monotonically within a period at constant power, so
        # binding both endpoints guarantees deliverability throughout the
        # whole period, closing the residual within-period double-sell a
        # start-only bound leaves open. `up_legs`/`down_legs` span every
        # currency together -- one shared physical headroom.
        prob += pulp.lpSum(cap[k][i] for k in up_legs) * t_act <= (soc[i] - soc_min) * eta
        prob += pulp.lpSum(cap[k][i] for k in up_legs) * t_act <= (soc[i + 1] - soc_min) * eta
        prob += pulp.lpSum(cap[k][i] for k in down_legs) * t_act <= (soc_max - soc[i]) / eta
        prob += pulp.lpSum(cap[k][i] for k in down_legs) * t_act <= (soc_max - soc[i + 1]) / eta
        if multi_energy_market:
            # Forbid buying into one energy market and selling into another
            # in the SAME period (module docstring's cross-market
            # pass-through guard) -- big-M = power_mw, since both totals are
            # already bounded by the shared power budget above. Still lets
            # the LP freely route a charge (or discharge) period's flow
            # across whichever markets pay best that period.
            prob += total_ch_i <= config.power_mw * (1 - is_dis[i])
            prob += total_dis_i <= config.power_mw * is_dis[i]

    if cap_mwh_per_window is not None:
        for i in range(T):
            prob += (
                pulp.lpSum(dis[m][j] * dt[j] for m in energy_markets for j in windows[i])
                <= cap_mwh_per_window
            )

    # Objective: every term expressed in DKK-equivalent at the fixed peg
    # (module docstring's currency-discipline section) -- energy, capacity
    # (any currency), and (P4) activation all compete for the one shared
    # budget on equal footing.
    objective = pulp.lpSum(
        energy_peg[m] * (energy_schedule_price_at_t[m][i] or 0.0) * (dis[m][i] - ch[m][i]) * dt[i]
        for m in energy_markets
        for i in range(T)
    )
    objective += pulp.lpSum(
        leg_peg[key] * (leg_schedule_price_at_t[key][i] or 0.0) * cap[key][i] * dt[i]
        for key in leg_keys
        for i in range(T)
    )
    if aFRR_capacity_legs:
        eur_peg = _peg_factor("EUR")  # activation is always EUR-native (registry)
        objective += pulp.lpSum(
            eur_peg
            * (activation_schedule_price_at_t[i] or 0.0)
            * cap[key][i]
            * config.afrr_activation_participation_rate
            * dt[i]
            for key in aFRR_capacity_legs
            for i in range(T)
        )
    prob += objective

    # Solver choice (module docstring's "no binary" section). The single-
    # market case is a pure LP -- solved with PuLP's bundled, zero-config CBC,
    # deterministic and unchanged from P1-P4 (so every already-persisted
    # single-market run reproduces bit-for-bit). The multi-market case adds
    # one binary per period (the anti-pass-through complementarity above),
    # making it a MILP that CBC's branch-and-bound is slow on (tens of
    # seconds for a month, worse for a quarter); HiGHS solves the same MILP
    # in ~1s. HiGHS is used ONLY for that MILP path -- it is likewise
    # deterministic for a fixed model, and multi-market runs are never part
    # of the persisted morning-brief set (day-ahead only), so this split
    # solver choice changes no existing persisted numbers.
    solver = pulp.HiGHS(msg=False) if multi_energy_market else pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)
    if pulp.LpStatus[status] != "Optimal":
        raise RuntimeError(
            f"BESS co-optimizer did not reach an optimal solution "
            f"(status={pulp.LpStatus[status]!r}) for zone={zone!r}, window="
            f"[{start_time}, {end_time}]"
        )

    ch_star = {m: [pulp.value(v) or 0.0 for v in ch[m]] for m in energy_markets}
    dis_star = {m: [pulp.value(v) or 0.0 for v in dis[m]] for m in energy_markets}
    soc_star = [starting_soc] + [pulp.value(v) or 0.0 for v in soc[1:]]
    cap_star: dict[str, list[float]] = {k: [pulp.value(v) or 0.0 for v in cap[k]] for k in leg_keys}

    # ---------------------------------------------------------------
    # Walk the solution into one BessTick per period. Every reported
    # revenue figure re-values the FIXED schedule (ch_star/dis_star/
    # cap_star) at SETTLEMENT prices -- in perfect mode settlement ==
    # schedule, so this is a no-op re-valuation. Capacity/activation
    # buckets stay NATIVE per-currency (never converted); only
    # `arbitrage_revenue_dkk` combines currencies, via the peg, at this
    # reporting boundary (module docstring).
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
            energy_peg[m]
            * (energy_settlement_price_at_t[m][i] or 0.0)
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

        for key in leg_keys:
            mw = cap_star[key][i]
            capacity_reserved_mw += mw
            revenue = (leg_settlement_price_at_t[key][i] or 0.0) * mw * dt[i]
            capacity_revenue_by_market[key] = revenue
            if leg_currency[key] == "DKK":
                capacity_revenue_dkk_tick += revenue
            else:
                capacity_revenue_eur_tick += revenue
            if key.split(":", 1)[0] == "aFRR_capacity":
                afrr_committed_mw += mw

        cumulative_capacity_dkk += capacity_revenue_dkk_tick
        cumulative_capacity_eur += capacity_revenue_eur_tick

        # aFRR activation revenue (module docstring): now part of the
        # objective (at SCHEDULE prices, peg-converted), but still REPORTED
        # at SETTLEMENT prices, unconverted (EUR-native, exactly as before).
        activation_price = activation_settlement_price_at_t[i] if afrr_committed_mw else None
        afrr_activation_revenue = (
            activation_price * afrr_committed_mw * config.afrr_activation_participation_rate * dt[i]
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
