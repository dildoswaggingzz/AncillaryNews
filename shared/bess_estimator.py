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
    full_cycle_equivalents, cycle_cap_was_binding}`

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
        config = _with_cycle_cap(base_config, max_cycles_per_day)
        for zone in zones:
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
                }
            )
    return summaries
