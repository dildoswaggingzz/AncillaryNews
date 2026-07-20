"""
Illustrative BESS backtest estimates for the Morning Brief (M5): "what a
representative battery would have earned in the past month", realistically
capped at ~1.5 charge/discharge cycles/day (`shared/bess_simulator.py`'s
`BessConfig.max_cycles_per_day`).

Two illustrative configs (`ILLUSTRATIVE_CONFIGS`) x two zones (confirmed
product decision: both DK1 and DK2, not one -- 4 backtests/day) are run and
persisted via the *existing* `shared.bess_simulator.run_backtest` /
`DatabaseManager.save_bess_run`, tagged `label="morning_brief"`
(init-db/05-morning-briefs.sql) so they're distinguishable from ad-hoc
`/dashboard/bess/new` runs in the run list, but otherwise identical,
independently queryable backtest runs -- no parallel persistence mechanism.
"""

from __future__ import annotations

from datetime import datetime

from shared.bess_simulator import BessConfig, run_backtest
from shared.db_manager import DatabaseManager

MORNING_BRIEF_RUN_LABEL = "morning_brief"

DEFAULT_ZONES = ("DK1", "DK2")

# Two illustrative battery sizes, deliberately labeled as "illustrative"
# everywhere they surface (README "Brainstorming" §: "clearly labelled as an
# estimate") -- not a specific real customer's asset. `max_cycles_per_day`
# is intentionally left unset here (falls back to whatever the caller of
# `run_illustrative_backtests` passes, default 1.5) so the same two configs
# can be reused with a different cap by a future caller if needed.
ILLUSTRATIVE_CONFIGS: list[tuple[str, BessConfig]] = [
    ("Small commercial (1 MW / 2 MWh)", BessConfig(power_mw=1.0, capacity_mwh=2.0)),
    ("Utility-scale (10 MW / 40 MWh)", BessConfig(power_mw=10.0, capacity_mwh=40.0)),
]


def _with_cycle_cap(config: BessConfig, max_cycles_per_day: float | None) -> BessConfig:
    """`BessConfig` is frozen -- build a copy with `max_cycles_per_day`
    overridden rather than mutating the shared `ILLUSTRATIVE_CONFIGS` entries."""
    from dataclasses import replace

    return replace(config, max_cycles_per_day=max_cycles_per_day)


# Per-zone capacity_markets override for the illustrative BESS backtests --
# a data table (Stage 4), not a hardcoded `zone != "DK2"` branch, so a new
# zone or market becomes a data change here rather than a code change. A
# zone absent from this table falls back to the base config's own
# `capacity_markets` default (see `_with_zone_capacity_markets`).
#
# DK1: FCR (DKK/MW/h) + aFRR_capacity up/down (DKK/MW/h) -- all-DKK, no
# FCR-D market (DK1 sits in the FCR Cooperation joint auction with Germany,
# a single symmetric band -- see shared/datasets.py's fcr_dk1 entry), so no
# up/down FCR legs here.
#
# DK2: FCR-N (`("FCR", "price")`, EUR/MW/h) + FCR-D up/down (EUR/MW/h) +
# aFRR_capacity up/down (DKK/MW/h) + FFR (`("FFR", "price")`, DKK/MW/h --
# shared/datasets.py's ffr_dk2 entry). Genuinely mixed-currency (FCR legs
# EUR, aFRR/FFR legs DKK) -- `shared/bess_simulator.py`'s per-currency
# buckets (module docstring §2) report `currencies_present == {"DKK", "EUR"}`
# for this stack, and callers (this module's summary dict below, the
# dashboard templates) must keep showing both totals separately, never
# combined into one number.
#
# `("aFRR_capacity", "down")` is added to *both* zones here -- previously
# omitted from this module (and `services/api/main.py`) for no stated
# reason; down-regulation capacity is genuinely BESS-addressable the same
# way up-regulation is.
ZONE_CAPACITY_MARKETS: dict[str, tuple[tuple[str, str], ...]] = {
    "DK1": (
        ("FCR", "price"),
        ("aFRR_capacity", "up"),
        ("aFRR_capacity", "down"),
    ),
    "DK2": (
        ("FCR", "price"),
        ("FCR", "up"),
        ("FCR", "down"),
        ("aFRR_capacity", "up"),
        ("aFRR_capacity", "down"),
        ("FFR", "price"),
    ),
}

# Zones whose ZONE_CAPACITY_MARKETS stack includes a market prone to
# clearing at/near 0 (FFR, currently DK2-only -- see shared/datasets.py's
# ffr_dk2 entry: prices are 0.0 today). "price_ranked" allocation
# (BessConfig.capacity_allocation) is required for these zones' illustrative
# runs -- with the default "even" split, FFR earning nothing would dilute
# FCR/aFRR's shares purely from the split, understating this zone's
# capacity revenue for a reason that has nothing to do with those markets
# themselves (see shared/bess_simulator.py's module docstring §2,
# "Allocation mode").
_PRICE_RANKED_ZONES = frozenset({"DK2"})


def _with_zone_capacity_markets(config: BessConfig, zone: str) -> BessConfig:
    """
    `BessConfig` is frozen -- build a copy with `capacity_markets` (and,
    where needed, `capacity_allocation`) set from `ZONE_CAPACITY_MARKETS`/
    `_PRICE_RANKED_ZONES` above for `zone`. A zone not present in
    `ZONE_CAPACITY_MARKETS` keeps the base config's own `capacity_markets`
    default unchanged.
    """
    from dataclasses import replace

    capacity_markets = ZONE_CAPACITY_MARKETS.get(zone, config.capacity_markets)
    capacity_allocation = (
        "price_ranked" if zone in _PRICE_RANKED_ZONES else config.capacity_allocation
    )
    return replace(
        config, capacity_markets=capacity_markets, capacity_allocation=capacity_allocation
    )


def run_illustrative_backtests(
    db: DatabaseManager,
    zones: tuple[str, ...] = DEFAULT_ZONES,
    *,
    start_time: datetime,
    end_time: datetime,
    max_cycles_per_day: float | None = 1.5,
) -> list[dict]:
    """
    Runs `run_backtest` for every (label, config) in `ILLUSTRATIVE_CONFIGS`
    x every zone in `zones` (4 runs for the default 2 configs x 2 zones),
    persists each via `db.save_bess_run(result, label="morning_brief")`, and
    returns one summary dict per run:

    `{config_label, zone, run_id, total_revenue_dkk,
    total_arbitrage_revenue_dkk, total_capacity_revenue_dkk,
    full_cycle_equivalents, cycle_cap_was_binding,
    total_afrr_activation_revenue_eur, total_capacity_revenue_eur,
    zero_price_periods_by_leg, currencies_present}`

    `total_capacity_revenue_eur` is DK2's EUR-denominated FCR capacity legs
    (`shared/bess_simulator.py`'s per-currency capacity buckets, module
    docstring §2) -- always 0.0 for DK1 (all-DKK), and for a DK2 run it's a
    genuinely separate figure from `total_capacity_revenue_dkk`, never
    summed into it or into `total_revenue_dkk`. `currencies_present`
    (`BacktestResult.currencies_present`, a `frozenset`) tells a caller
    whether that separation actually matters for this run --
    `{"DKK", "EUR"}` for DK2 (mixed FCR/EUR + aFRR+FFR/DKK), `{"DKK"}` for
    DK1 -- so a template can show the "not summable" note only when needed.

    `zero_price_periods_by_leg` (`BacktestResult.zero_price_periods_by_leg`)
    supports honest framing for a market that's currently earning nothing
    (FFR today) -- "FFR cleared at 0 for 720/720 hours in this window"
    rather than silently showing a flat zero total with no context.

    DK2 runs widen `capacity_markets` to the full stack in
    `ZONE_CAPACITY_MARKETS` (FCR-N/FCR-D/aFRR up+down/FFR) and switch
    `capacity_allocation` to `"price_ranked"` (`_PRICE_RANKED_ZONES`) so
    FFR's currently-zero price doesn't dilute the other, genuinely-earning
    groups' shares -- see `shared/bess_simulator.py`'s module docstring §2
    and `_with_zone_capacity_markets` below. DK1 gains
    `("aFRR_capacity", "down")` on top of its existing FCR/aFRR-up pair but
    keeps `capacity_allocation="even"` (no zero-price-prone market in its
    stack).

    `cycle_cap_was_binding` is `True` if the rolling-24h cycle cap
    (`BessTick.cycle_cap_binding`) was ever the limiting factor across the
    run -- feeds the brief's "capped your earnings on N of the past 30
    days"-style framing (the per-day count itself is derivable from
    `db.fetch_bess_ticks(run_id)` by the caller if needed; this summary only
    carries the boolean "did it ever bind" signal).

    A single backtest window's data-fetch or persistence failure is not
    caught here -- it propagates to the caller (`run_morning_brief` wraps
    the whole BESS-estimates stage in its own try/except per the plan's
    "one stage's failure never blocks the others" contract), so a partial
    list of runs is never silently returned as if it were complete.
    """
    summaries = []
    for label, base_config in ILLUSTRATIVE_CONFIGS:
        config_with_cap = _with_cycle_cap(base_config, max_cycles_per_day)
        for zone in zones:
            config = _with_zone_capacity_markets(config_with_cap, zone)
            result = run_backtest(db, zone, start_time, end_time, config)
            run_id = db.save_bess_run(result, label=MORNING_BRIEF_RUN_LABEL)
            summaries.append(
                {
                    "config_label": label,
                    "zone": zone,
                    "run_id": run_id,
                    "total_revenue_dkk": result.total_revenue_dkk,
                    "total_arbitrage_revenue_dkk": result.total_arbitrage_revenue_dkk,
                    "total_capacity_revenue_dkk": result.total_capacity_revenue_dkk,
                    "full_cycle_equivalents": result.full_cycle_equivalents,
                    "cycle_cap_was_binding": any(t.cycle_cap_binding for t in result.ticks),
                    "total_afrr_activation_revenue_eur": result.total_afrr_activation_revenue_eur,
                    "total_capacity_revenue_eur": result.total_capacity_revenue_eur,
                    "zero_price_periods_by_leg": result.zero_price_periods_by_leg,
                    # sorted list, not the raw frozenset -- this summary is
                    # persisted as JSONB (db.save_morning_brief), and a
                    # frozenset isn't JSON-serializable.
                    "currencies_present": sorted(result.currencies_present),
                }
            )
    return summaries
