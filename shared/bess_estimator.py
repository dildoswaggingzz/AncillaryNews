"""
Illustrative BESS backtest estimates for the Morning Brief (M5): "what a
representative battery would have earned in the past month", realistically
capped at ~1.5 charge/discharge cycles/day (`shared/bess_simulator.py`'s
`BessConfig.max_cycles_per_day`).

**P5: migrated to the co-optimizer, in BOTH foresight modes (achievable +
ceiling), replacing the threshold engine.** The threshold engine is RETIRED
from the brief -- P2's diagnostic (`shared/bess_dispatch_milp.py:
phantom_capacity_revenue`) found it published ~65% phantom (infeasible)
capacity revenue on real windows, which is not a number this project wants
in front of a non-technical reader. Every illustrative config now runs
TWICE, both with `strategy="cooptimized"` (docs/bess-cooptimizer-design.md):

- **Achievable** (`foresight="forecast"`) -- the LP schedules against a
  causal lag-24h-persistence forecast and settles at actual prices (§5's
  pre mode). This is the headline: an honest "what a battery would have
  earned last month" figure a real, causal strategy could actually have
  followed, with no double-selling and no lookahead.
- **Ceiling** (`foresight="perfect"`) -- the LP schedules AND settles at
  actual prices (§5's post mode), a perfect-foresight oracle. Reported
  purely as labelled context ("theoretical ceiling, not achievable"),
  never as if it were a deployable number.

Both illustrative configs (`ILLUSTRATIVE_CONFIGS`) x both zones x both
foresight modes = 8 backtests/day, run and persisted via the *existing*
`shared.bess_simulator.run_backtest` / `DatabaseManager.save_bess_run`,
tagged `label="morning_brief"` (init-db/05-morning-briefs.sql) so they're
distinguishable from ad-hoc `/dashboard/bess/new` runs in the run list, but
otherwise identical, independently queryable backtest runs -- no parallel
persistence mechanism. The persisted `config` JSONB itself carries
`strategy`/`foresight`, so all 8 runs stay distinguishable from each other
purely by re-reading that column -- no new DB column needed.

**`BessConfig`'s own defaults are UNCHANGED** (`strategy="threshold"`,
`foresight="perfect"`) -- this module passes both overrides EXPLICITLY via
`dataclasses.replace` on every call, exactly the same "opt in per call,
never change the shared default" discipline `_with_cycle_cap`/
`_with_zone_capacity_markets` below already use. This is what keeps old
persisted run configs (from before this field existed, or from an ad-hoc
threshold run) reproducing identically when re-run -- P1's guarantee.
`capacity_commit_mw`/`capacity_allocation` are threshold-only concepts
(`shared/bess_simulator.py`'s `BessConfig` docstring) and are silently
ignored by the co-optimizer -- harmless to leave configured, not acted on.
"""

from __future__ import annotations

from dataclasses import replace
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
# combined into one number. The co-optimizer's single joint LP
# (`shared/bess_dispatch_milp.py`, P4) dispatches this mixed-currency stack
# natively -- no crowd-out, no per-run currency switch needed.
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
# (BessConfig.capacity_allocation) was required for the retired threshold
# engine's illustrative runs (it never mattered to the co-optimizer, which
# ignores `capacity_allocation` entirely -- the LP decides each leg's
# commitment level directly) -- kept only so a threshold run of this same
# config (e.g. an ad-hoc `/dashboard/bess/new` comparison) still avoids the
# even-split dilution artifact (see shared/bess_simulator.py's module
# docstring §2, "Allocation mode").
_PRICE_RANKED_ZONES = frozenset({"DK2"})


def _with_zone_capacity_markets(config: BessConfig, zone: str) -> BessConfig:
    """
    `BessConfig` is frozen -- build a copy with `capacity_markets` (and,
    where needed, `capacity_allocation`) set from `ZONE_CAPACITY_MARKETS`/
    `_PRICE_RANKED_ZONES` above for `zone`. A zone not present in
    `ZONE_CAPACITY_MARKETS` keeps the base config's own `capacity_markets`
    default unchanged.
    """
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
    Runs `run_backtest` TWICE (achievable + ceiling, see module docstring)
    for every (label, config) in `ILLUSTRATIVE_CONFIGS` x every zone in
    `zones` -- 8 runs for the default 2 configs x 2 zones x 2 foresight
    modes -- persists EACH via `db.save_bess_run(result,
    label="morning_brief")` (8 persisted rows/day), and returns ONE summary
    dict per (config, zone) carrying BOTH totals:

    `{config_label, zone, achievable_run_id, ceiling_run_id,
    total_revenue_dkk_achievable, total_revenue_dkk_ceiling,
    total_revenue_all_dkk_achievable, total_revenue_all_dkk_ceiling,
    total_revenue_all_eur_achievable, total_revenue_all_eur_ceiling,
    full_cycle_equivalents_achievable, cycle_cap_was_binding_achievable,
    total_afrr_activation_revenue_eur_achievable,
    total_capacity_revenue_eur_achievable, currencies_present,
    zero_price_periods_by_leg}`

    Every per-run detail field (cycle cap, aFRR activation, capacity-EUR,
    currencies present, zero-price periods) is taken from the ACHIEVABLE
    run only -- the ceiling run is an oracle, never a deployable policy, so
    only its headline totals are surfaced (`total_revenue_dkk_ceiling`/
    `total_revenue_all_dkk_ceiling`/`total_revenue_all_eur_ceiling`); a
    ceiling-side cycle-cap/activation breakdown would invite a reader to
    treat the oracle as if it were something a real strategy could follow.

    `total_revenue_all_dkk`/`_eur` (`BacktestResult`'s §4.1 peg-converted
    combined totals) are what `shared/morning_brief_editor.py` actually
    renders -- a single headline figure per currency-thinking reader,
    rather than the unconverted per-currency buckets alone. `currencies_present`
    (`BacktestResult.currencies_present`, a `frozenset`) tells a caller
    whether that separation actually matters for this run -- `{"DKK", "EUR"}`
    for DK2 (mixed FCR/EUR + aFRR+FFR/DKK), `{"DKK"}` for DK1.

    `zero_price_periods_by_leg` (`BacktestResult.zero_price_periods_by_leg`)
    supports honest framing for a market that's currently earning nothing
    (FFR today) -- "FFR cleared at 0 for 720/720 hours in this window"
    rather than silently showing a flat zero total with no context.

    DK2 runs widen `capacity_markets` to the full stack in
    `ZONE_CAPACITY_MARKETS` (FCR-N/FCR-D/aFRR up+down/FFR) -- see
    `shared/bess_simulator.py`'s module docstring §2 and
    `_with_zone_capacity_markets` below.

    `cycle_cap_was_binding_achievable` is `True` if the rolling-24h cycle
    cap (`BessTick.cycle_cap_binding`) was ever the limiting factor across
    the achievable run.

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
            zone_config = _with_zone_capacity_markets(config_with_cap, zone)

            # BessConfig's own defaults stay threshold/perfect (module
            # docstring) -- both overrides applied explicitly, per call.
            achievable_config = replace(zone_config, strategy="cooptimized", foresight="forecast")
            ceiling_config = replace(zone_config, strategy="cooptimized", foresight="perfect")

            achievable_result = run_backtest(db, zone, start_time, end_time, achievable_config)
            achievable_run_id = db.save_bess_run(achievable_result, label=MORNING_BRIEF_RUN_LABEL)

            ceiling_result = run_backtest(db, zone, start_time, end_time, ceiling_config)
            ceiling_run_id = db.save_bess_run(ceiling_result, label=MORNING_BRIEF_RUN_LABEL)

            summaries.append(
                {
                    "config_label": label,
                    "zone": zone,
                    "achievable_run_id": achievable_run_id,
                    "ceiling_run_id": ceiling_run_id,
                    "total_revenue_dkk_achievable": achievable_result.total_revenue_dkk,
                    "total_revenue_dkk_ceiling": ceiling_result.total_revenue_dkk,
                    "total_revenue_all_dkk_achievable": achievable_result.total_revenue_all_dkk,
                    "total_revenue_all_dkk_ceiling": ceiling_result.total_revenue_all_dkk,
                    "total_revenue_all_eur_achievable": achievable_result.total_revenue_all_eur,
                    "total_revenue_all_eur_ceiling": ceiling_result.total_revenue_all_eur,
                    "full_cycle_equivalents_achievable": achievable_result.full_cycle_equivalents,
                    "cycle_cap_was_binding_achievable": any(
                        t.cycle_cap_binding for t in achievable_result.ticks
                    ),
                    "total_afrr_activation_revenue_eur_achievable": (
                        achievable_result.total_afrr_activation_revenue_eur
                    ),
                    "total_capacity_revenue_eur_achievable": (
                        achievable_result.total_capacity_revenue_eur
                    ),
                    # sorted list, not the raw frozenset -- this summary is
                    # persisted as JSONB (db.save_morning_brief), and a
                    # frozenset isn't JSON-serializable.
                    "currencies_present": sorted(achievable_result.currencies_present),
                    "zero_price_periods_by_leg": achievable_result.zero_price_periods_by_leg,
                }
            )
    return summaries
