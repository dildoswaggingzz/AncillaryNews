"""
M6 P4: does acting on a forecast capture more money than acting on trailing
persistence? (`docs/forecast-economic-eval-design.md`.)

**The reframing this module exists to answer (design §0):** `shared/
bess_simulator.py`'s capacity commitment always clears at the realised
price -- there is no bid/clear model -- so a forecast cannot add value by
bidding better; it can only add value through **allocation**: which market
gets committed capacity, and how much power is held for capacity versus
arbitrage, each period. This module builds that allocation layer for
exactly the two legs the M6 forecasts cover -- FCR-D DK2 up/down and
day-ahead DK2 -- deliberately excluding `aFRR_capacity` (no model for it;
including it would force a non-forecast leg into the comparison and muddy
it, design §3).

**Reused, not reimplemented:**
- `shared.bess_simulator._leg_relative_strength`/`PRICE_RANKED_BASELINE_MULTIPLIER`
  -- the exact causal "this leg's recent price vs. its own longer-run
  baseline" ratio the existing `price_ranked` capacity allocation uses. The
  `trailing` policy below scores FCR-D's two legs with this function,
  imported unmodified.
- `shared.bess_simulator._causal_zscore` -- the existing causal arbitrage
  trigger, imported unmodified. (`_value_at_or_before`'s forward-fill lookup
  is not needed here: every series this module reads is already on the same
  hourly grid -- see "Hourly only" below -- so a plain dict lookup by exact
  timestamp is all a tick needs, unlike `run_backtest`'s mixed-cadence
  day-ahead/capacity-price join.)
- `shared.forecast_model.fit_quantile_model`/`effective_train_window` and
  `shared.baselines.Fold` -- the P3/P3b leak-safe walk-forward machinery.
  `_walk_forward_predictions` below mirrors `run_model_walk_forward`'s
  per-fold-refit loop exactly, differing only in what it returns
  (predictions, not pooled pinball loss -- `run_model_walk_forward` itself
  cannot be reused as-is because an allocation policy needs the actual
  predicted values, not a loss scalar).

**What is genuinely new here (not a reuse of `bess_simulator.run_backtest`
itself):** `run_backtest`'s `capacity_allocation="price_ranked"` only
reweights *across* `capacity_markets` groups (`_group_commit_shares`); DK2's
FCR-D up/down pair is a single group there (`legs_by_group` keys on
`market`, and both legs share `market="FCR"`), so within that one group the
existing code always splits 1/n_groups... /len(legs) flat, **regardless of
allocation mode** (see that module's `run_backtest`,
`commit_per_leg = commit_per_group / len(legs)`, unconditional on
`capacity_allocation`). A "trailing" policy that never differentiates FCR-D
up from FCR-D down cannot be beaten or matched by a forecast that does, so
this module extends the *same relative-strength ranking shape* one level
deeper -- to individual legs, not just groups -- via `_ratio_strength`/
`_weighted_split` below, generalising `_leg_relative_strength`/
`_group_commit_shares`'s pattern rather than importing them for this
purpose. **`bess_simulator.py` itself is not modified** -- its existing
"even"/"price_ranked" behaviour (load-bearing for other configs, e.g. the
morning-brief FCR+aFRR default) stays exactly as it was.

**The capacity-vs-arbitrage power split (design §3's other named decision):**
for the `trailing`/`model`/`oracle` policies, `EconomicEvalConfig.power_mw`
is redistributed *every tick* across three competing "legs" -- `FCR:up`,
`FCR:down`, and a synthetic `arbitrage` leg scored by how far day-ahead's
price sits from its own trailing normal level in *either* direction
(`_abs_deviation_strength` -- a high OR a low price is an arbitrage
opportunity, unlike a capacity leg where higher is unambiguously better,
so this is deliberately not `_ratio_strength`/`_leg_relative_strength`'s
signed ratio). This makes `EconomicEvalConfig.capacity_commit_mw`
**unused** for those three policies -- the full `power_mw` competes, not a
pre-carved envelope -- and used only by the `even` floor policy, which
reproduces `bess_simulator`'s original fixed-split behaviour untouched, as
the zero-signal baseline for context (design §2's "report `even` too").

**A known scale-mismatch limitation in that three-way split, stated plainly
rather than tuned away after seeing results:** `_ratio_strength` (FCR-D's
two legs) centres on **1.0** ("this leg is at its own normal level"), while
`_abs_deviation_strength` (the arbitrage leg) centres on **0** ("day-ahead
is at its own normal level, i.e. no opportunity right now"). `_weighted_split`
compares these two centrings directly, so FCR-D's ~1.0 baseline structurally
outweighs arbitrage's typically-small (~0.1-0.3) deviation most hours,
independent of which is actually the better economic decision that tick --
this is very likely why the realised FCR-vs-arbitrage MW split comes out
similar across both asset configs in `docs/forecast-economic-eval-results.md`
§4 (see that section's own note on why the tilt prediction can't be tested
by this implementation at all). A principled fix (e.g. z-scoring both
signals onto a common scale before splitting) is a real improvement but is
NOT made here -- changing the scoring function after already having run it
against the eval window would itself be exactly the "no tuning against the
eval window's own numbers" this phase's acceptance criteria rules out.
Flagged for whoever picks this up next.

**Leak discipline (design §4, the module's central correctness property):**
`simulate()`'s `policy` parameter is a `Literal["even", "trailing",
"model"]` -- **"oracle" is not a value it accepts**, so it is structurally
unreachable through the one deployable entry point. The only lookahead path
is `run_oracle_ceiling()`, a separate, unmistakably-named function that
exists solely to compute the headroom ceiling (design §1) and must never be
treated as a policy a real deployment could run. Both call a shared private
core (`_simulate_core`) purely for code reuse of the tick loop's
non-allocation mechanics (SoC, efficiency, the rolling cycle cap); the
`policy` value that reaches it is never attacker-controlled -- `simulate()`
raises before `_simulate_core` is ever called with anything but "even",
"trailing", or "model". For every policy, revenue *realisation* always uses
the real, already-known-at-settlement clearing price -- only the
*allocation weights* differ between "trailing"/"model" (causal) and
"oracle" (lookahead); this mirrors the design's core point (§0) that a
forecast can only ever change *which* leg gets the commitment, never what
price it settles at.

**Currency (a correction to the design doc, flagged rather than silently
fixed -- same posture `shared/baselines.py`'s day-ahead retarget took for
the identical discrepancy):** design §3 states "FCR-D DK2 and day-ahead DK2
are both EUR -- clean comparison, no FX." This is **not true of the live
registry**: `shared/datasets.py`'s `day_ahead_prices` entry declares
`unit="DKK/MWh"` (`DayAheadPriceDKK`), matching `shared/baselines.py`'s own
already-documented finding for the identical series. FCR-D DK2 (`fcr_dk2`)
really is EUR/MW/h. So capacity revenue (FCR-D) and arbitrage revenue
(day-ahead) here are in **different currencies** and are reported in
separate buckets throughout this module (`capacity_revenue_eur`/
`arbitrage_revenue_dkk`, never summed) -- the same "per-currency buckets,
never converted, never summed" discipline `bess_simulator.py`'s own
module docstring §2 already established for the identical DKK/EUR
conflation hazard.

**Hourly only, dt_hours=1.0** -- every series this module reads is already
hourly (FCR-D natively; day-ahead aggregated by `shared.baselines.
DAY_AHEAD_TARGET`'s `aggregate_hourly=True`, matching P3b), so unlike
`run_backtest`'s variable-cadence `dt_hours`, a constant 1-hour period is
exact here, not an approximation.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

from shared.baselines import Fold
from shared.bess_simulator import (
    PRICE_RANKED_BASELINE_MULTIPLIER,
    _causal_zscore,
    _leg_relative_strength,
)
from shared.forecast_model import (
    ForecastModelConfig,
    JoinedDataset,
    effective_train_window,
    fit_quantile_model,
)

# The three legs P4 evaluates (design §3) -- FCR-D DK2's two directions plus
# day-ahead DK2, the exact and only coverage of the two M6 forecast models.
# aFRR_capacity is deliberately excluded (no model for it -- see module
# docstring).
LEG_FCR_UP = "FCR:up"
LEG_FCR_DOWN = "FCR:down"
LEG_ARBITRAGE = "day_ahead:price"

AllocationPolicy = Literal["even", "trailing", "model"]

QuantileVariant = Literal["median", "low_tail"]
# design §4: median (τ=0.5) is the natural default for ranking expected
# revenue; low-tail (τ=0.1) is reported because P3/P3b found the model's
# edge concentrated there. Not a sweep -- exactly these two, both reported.
QUANTILE_VARIANT_TAU: dict[QuantileVariant, float] = {"median": 0.5, "low_tail": 0.1}


@dataclass(frozen=True)
class EconomicEvalConfig:
    """
    Physical battery + strategy parameters for P4's simulate loop --
    deliberately a separate dataclass from `shared.bess_simulator.BessConfig`,
    not a reuse of it, because several of that config's fields are
    meaningless here (`capacity_markets`/`capacity_allocation`/
    `afrr_activation_participation_rate`/`price_market` -- P4's scope is
    fixed to exactly FCR-D up/down + day-ahead, never configurable per-run,
    per design §3) and reusing `BessConfig` directly would let a caller set
    one of those and silently have it ignored. Every field that *is*
    shared keeps `BessConfig`'s exact name/default/validation so the two
    asset configs (design's allocation doc §1: 1MW/2MWh "0.5C" and
    1MW/4MWh "0.25C") are constructed identically either way.
    """

    power_mw: float = 1.0
    capacity_mwh: float = 2.0
    round_trip_efficiency: float = 0.90
    soc_min_fraction: float = 0.10
    soc_max_fraction: float = 0.90
    starting_soc_fraction: float = 0.50
    arbitrage_lookback_periods: int = 30
    arbitrage_z_threshold: float = 0.5
    # Used ONLY by the `even` policy (module docstring) -- the fixed
    # envelope `bess_simulator`'s original default splits 50/50 across
    # FCR-D's two legs, with `power_mw - capacity_commit_mw` fixed for
    # arbitrage. `trailing`/`model`/`oracle` redistribute the FULL
    # `power_mw` every tick instead and never read this field.
    capacity_commit_mw: float = 0.3
    max_cycles_per_day: float | None = 1.5

    def __post_init__(self):
        if self.power_mw <= 0:
            raise ValueError("power_mw must be positive")
        if self.capacity_mwh <= 0:
            raise ValueError("capacity_mwh must be positive")
        if not 0 < self.round_trip_efficiency <= 1:
            raise ValueError("round_trip_efficiency must be in (0, 1]")
        if not 0 <= self.soc_min_fraction < self.soc_max_fraction <= 1:
            raise ValueError("soc_min_fraction must be < soc_max_fraction, both in [0, 1]")
        if not self.soc_min_fraction <= self.starting_soc_fraction <= self.soc_max_fraction:
            raise ValueError("starting_soc_fraction must be within the usable SoC band")
        if self.arbitrage_lookback_periods <= 0:
            raise ValueError("arbitrage_lookback_periods must be positive")
        if self.capacity_commit_mw < 0 or self.capacity_commit_mw > self.power_mw:
            raise ValueError("capacity_commit_mw must be in [0, power_mw]")
        if self.max_cycles_per_day is not None and self.max_cycles_per_day <= 0:
            raise ValueError("max_cycles_per_day must be positive (or None for unconstrained)")


# --- leg relative-strength scoring (generalises _leg_relative_strength) -----


def _baseline_mean(baseline_history: deque[float]) -> float | None:
    """`None` if there is no baseline yet, or its mean is not positive (no
    "normal level" to be relatively strong against) -- same degenerate
    cases `shared.bess_simulator._leg_relative_strength` guards against."""
    if not baseline_history:
        return None
    mean = statistics.mean(baseline_history)
    return mean if mean > 0 else None


def _ratio_strength(value: float | None, baseline_history: deque[float]) -> float:
    """
    Single-point analogue of `_leg_relative_strength`'s short-window mean:
    `value`'s ratio to `baseline_history`'s own trailing mean, clipped at 0.
    Used for FCR-D legs' `model`/`oracle` policies, where the "current"
    signal is one forecast/actual value per tick rather than a window.
    `None` `value` (no forecast/no data at this tick) scores 0 -- the same
    "shrink toward siblings" fallback `_group_commit_shares` uses for a
    leg with nothing to rank by.
    """
    if value is None:
        return 0.0
    baseline_mean = _baseline_mean(baseline_history)
    if baseline_mean is None:
        return 0.0
    return max(value / baseline_mean, 0.0)


def _abs_deviation_strength(value: float | None, baseline_history: deque[float]) -> float:
    """
    The arbitrage leg's "opportunity" signal: how far `value` sits from its
    own trailing normal level, in **either** direction. Deliberately not
    `_ratio_strength`'s signed ratio -- a day-ahead price unusually *low*
    is as good an arbitrage opportunity (charge) as one unusually *high*
    (discharge), whereas a capacity leg's price is unambiguously better the
    higher it is. Same `None`/degenerate-baseline handling as
    `_ratio_strength`.
    """
    if value is None:
        return 0.0
    baseline_mean = _baseline_mean(baseline_history)
    if baseline_mean is None:
        return 0.0
    return abs(value - baseline_mean) / baseline_mean


def _weighted_split(strengths: dict[str, float], total: float) -> dict[str, float]:
    """
    Splits `total` across `strengths`' keys proportional to their
    (non-negative-clipped) values -- the same two-level shape
    `shared.bess_simulator._group_commit_shares` uses (group share, then
    leg share within a group), generalised to be data-source-agnostic (the
    caller supplies a strength per key, from whichever policy's signal) so
    it serves both the FCR-vs-arbitrage split and the up-vs-down split
    below with one function. Falls back to an even split -- and mirrors
    `_group_commit_shares`'s fallback exactly -- when every strength is 0
    (no signal at all yet, e.g. before any history has accumulated).
    """
    n = len(strengths)
    if n == 0:
        return {}
    if total <= 0:
        return dict.fromkeys(strengths, 0.0)
    positive_total = sum(max(s, 0.0) for s in strengths.values())
    if positive_total <= 0:
        return {k: total / n for k in strengths}
    return {k: total * (max(s, 0.0) / positive_total) for k, s in strengths.items()}


# --- tick-level result -------------------------------------------------------


@dataclass
class EconomicEvalTick:
    time: datetime
    action: str  # "charge" | "discharge" | "idle"
    capacity_reserved_mw: float  # FCR-D up + down, this tick
    arbitrage_power_mw: float
    capacity_revenue_eur: float
    arbitrage_revenue_dkk: float
    energy_discharged_mwh: float
    cumulative_capacity_revenue_eur: float
    cumulative_arbitrage_revenue_dkk: float
    cycle_cap_binding: bool = False


@dataclass
class EconomicEvalResult:
    policy: str  # "even" | "trailing" | "model" | "oracle"
    quantile_variant: QuantileVariant | None  # None: quantile-invariant (even/trailing/oracle)
    config: EconomicEvalConfig
    ticks: list[EconomicEvalTick] = field(default_factory=list)

    @property
    def total_capacity_revenue_eur(self) -> float:
        return self.ticks[-1].cumulative_capacity_revenue_eur if self.ticks else 0.0

    @property
    def total_arbitrage_revenue_dkk(self) -> float:
        return self.ticks[-1].cumulative_arbitrage_revenue_dkk if self.ticks else 0.0

    @property
    def total_discharged_mwh(self) -> float:
        return sum(t.energy_discharged_mwh for t in self.ticks)

    @property
    def cycle_cap_binding_periods(self) -> int:
        return sum(1 for t in self.ticks if t.cycle_cap_binding)

    @property
    def realised_cycles_per_day(self) -> float:
        """
        Mean realised full-cycle-equivalents/day over the run -- the
        allocation design's §2.4 reporting requirement ("every backtest
        result must report realised cycles/day"), computed the same way
        `shared.bess_simulator.BacktestResult.full_cycle_equivalents` does
        (discharged MWh / nameplate capacity), just divided by the run's
        own span in days rather than left as a raw total.
        """
        if not self.ticks or not self.config.capacity_mwh:
            return 0.0
        n_days = len(self.ticks) / 24.0
        if n_days <= 0:
            return 0.0
        return (self.total_discharged_mwh / self.config.capacity_mwh) / n_days


# --- the simulate loop --------------------------------------------------------


def _simulate_core(
    times: list[datetime],
    actuals: dict[str, dict[datetime, float]],
    config: EconomicEvalConfig,
    *,
    policy: str,
    quantile_variant: QuantileVariant | None,
    forecast_maps: dict[str, dict[datetime, float]] | None,
    lookahead: bool,
) -> EconomicEvalResult:
    """
    Shared tick loop for every policy -- **private**. `simulate()` and
    `run_oracle_ceiling()` are the only two callers; `simulate()`'s
    `policy: Literal["even", "trailing", "model"]` parameter makes it
    structurally impossible for a caller of the public API to ever reach
    this function with `policy="oracle"`/`lookahead=True` (module
    docstring's leak-discipline paragraph). This function does not
    validate `policy` itself -- both public callers already have, and this
    stays a private implementation detail, never re-exported.
    """
    up_actual = actuals[LEG_FCR_UP]
    down_actual = actuals[LEG_FCR_DOWN]
    da_actual = actuals[LEG_ARBITRAGE]

    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    soc_mwh = config.starting_soc_fraction * config.capacity_mwh
    leg_efficiency = config.round_trip_efficiency**0.5

    short_maxlen = config.arbitrage_lookback_periods
    baseline_maxlen = config.arbitrage_lookback_periods * PRICE_RANKED_BASELINE_MULTIPLIER
    up_short: deque[float] = deque(maxlen=short_maxlen)
    up_baseline: deque[float] = deque(maxlen=baseline_maxlen)
    down_short: deque[float] = deque(maxlen=short_maxlen)
    down_baseline: deque[float] = deque(maxlen=baseline_maxlen)
    da_short: deque[float] = deque(maxlen=short_maxlen)
    da_baseline: deque[float] = deque(maxlen=baseline_maxlen)

    # Separate from da_short/da_baseline above -- this is the exact z-score
    # history shape `shared.bess_simulator._causal_zscore`/`run_backtest`
    # use for the arbitrage trigger (a plain rolling list, capped at
    # arbitrage_lookback_periods), reused unmodified via `_causal_zscore`.
    da_zscore_history: list[float] = []

    cap_mwh_per_window = (
        config.capacity_mwh * config.max_cycles_per_day
        if config.max_cycles_per_day is not None
        else None
    )
    discharge_window: deque[tuple[datetime, float]] = deque()

    cumulative_capacity_eur = 0.0
    cumulative_arbitrage_dkk = 0.0
    ticks: list[EconomicEvalTick] = []

    for t in times:
        up_price = up_actual.get(t)
        down_price = down_actual.get(t)
        da_price = da_actual.get(t)

        # --- allocation weights: the ONLY thing that differs by policy ---
        if policy == "even":
            mw_up = config.capacity_commit_mw / 2.0
            mw_down = config.capacity_commit_mw / 2.0
            arbitrage_power_mw = config.power_mw - config.capacity_commit_mw
        else:
            if policy == "trailing":
                up_strength = _leg_relative_strength(up_short, up_baseline)
                down_strength = _leg_relative_strength(down_short, down_baseline)
                da_short_mean = statistics.mean(da_short) if da_short else None
                arb_strength = _abs_deviation_strength(da_short_mean, da_baseline)
            elif policy == "model":
                # `forecast_maps` already holds exactly one quantile
                # variant's predictions (the caller selected `tau` when
                # building it via `build_forecast_maps`); this loop never
                # needs to know which tau, only the resulting value per leg.
                assert forecast_maps is not None
                up_val = forecast_maps[LEG_FCR_UP].get(t)
                down_val = forecast_maps[LEG_FCR_DOWN].get(t)
                da_val = forecast_maps[LEG_ARBITRAGE].get(t)
                up_strength = _ratio_strength(up_val, up_baseline)
                down_strength = _ratio_strength(down_val, down_baseline)
                arb_strength = _abs_deviation_strength(da_val, da_baseline)
            elif policy == "oracle":
                if not lookahead:
                    raise AssertionError(
                        "oracle policy reached _simulate_core with lookahead=False -- "
                        "this must never happen (leak-safety invariant)"
                    )
                up_strength = _ratio_strength(up_price, up_baseline)
                down_strength = _ratio_strength(down_price, down_baseline)
                arb_strength = _abs_deviation_strength(da_price, da_baseline)
            else:
                raise ValueError(f"unknown policy {policy!r}")

            fcr_strength = statistics.mean([up_strength, down_strength])
            group_split = _weighted_split(
                {"FCR": fcr_strength, "arbitrage": arb_strength}, config.power_mw
            )
            leg_split = _weighted_split(
                {"up": up_strength, "down": down_strength}, group_split["FCR"]
            )
            mw_up, mw_down = leg_split["up"], leg_split["down"]
            arbitrage_power_mw = group_split["arbitrage"]

        # --- capacity revenue: ALWAYS the real clearing price, every policy ---
        capacity_revenue_eur = (up_price or 0.0) * mw_up + (down_price or 0.0) * mw_down
        cumulative_capacity_eur += capacity_revenue_eur

        # --- arbitrage: existing causal z-score trigger (unchanged),
        # capped by this tick's (policy-dependent) arbitrage_power_mw ---
        action = "idle"
        arbitrage_revenue_dkk = 0.0
        energy_discharged_mwh = 0.0
        cycle_cap_binding = False
        z = _causal_zscore(da_zscore_history, da_price) if da_price is not None else None

        if z is not None and z <= -config.arbitrage_z_threshold:
            headroom_mwh = (soc_max - soc_mwh) / leg_efficiency if leg_efficiency else 0.0
            grid_energy_mwh = max(min(arbitrage_power_mw, headroom_mwh), 0.0)
            if grid_energy_mwh > 0:
                soc_mwh += grid_energy_mwh * leg_efficiency
                arbitrage_revenue_dkk = -da_price * grid_energy_mwh
                action = "charge"
        elif z is not None and z >= config.arbitrage_z_threshold:
            available_mwh = (soc_mwh - soc_min) * leg_efficiency
            grid_energy_mwh = max(min(arbitrage_power_mw, available_mwh), 0.0)
            if cap_mwh_per_window is not None:
                window_start = t - timedelta(hours=24)
                while discharge_window and discharge_window[0][0] < window_start:
                    discharge_window.popleft()
                discharged_in_window = sum(mwh for _, mwh in discharge_window)
                remaining_cap_mwh = max(cap_mwh_per_window - discharged_in_window, 0.0)
                if grid_energy_mwh > remaining_cap_mwh:
                    grid_energy_mwh = remaining_cap_mwh
                    cycle_cap_binding = True
            if grid_energy_mwh > 0:
                soc_mwh -= grid_energy_mwh / leg_efficiency if leg_efficiency else 0.0
                arbitrage_revenue_dkk = da_price * grid_energy_mwh
                energy_discharged_mwh = grid_energy_mwh
                action = "discharge"
                if cap_mwh_per_window is not None:
                    discharge_window.append((t, grid_energy_mwh))

        cumulative_arbitrage_dkk += arbitrage_revenue_dkk

        ticks.append(
            EconomicEvalTick(
                time=t,
                action=action,
                capacity_reserved_mw=mw_up + mw_down,
                arbitrage_power_mw=arbitrage_power_mw,
                capacity_revenue_eur=capacity_revenue_eur,
                arbitrage_revenue_dkk=arbitrage_revenue_dkk,
                energy_discharged_mwh=energy_discharged_mwh,
                cumulative_capacity_revenue_eur=cumulative_capacity_eur,
                cumulative_arbitrage_revenue_dkk=cumulative_arbitrage_dkk,
                cycle_cap_binding=cycle_cap_binding,
            )
        )

        # --- causal history updates, strictly AFTER this tick's decision
        # and revenue were computed (mirrors shared.bess_simulator's own
        # "append after use" discipline for _causal_zscore/
        # _group_commit_shares) -- maintained unconditionally, regardless
        # of policy, so the baseline a `model`/`oracle` tick normalises
        # against is exactly the same causal trailing history a `trailing`
        # tick would see. ---
        if up_price is not None:
            up_short.append(up_price)
            up_baseline.append(up_price)
        if down_price is not None:
            down_short.append(down_price)
            down_baseline.append(down_price)
        if da_price is not None:
            da_short.append(da_price)
            da_baseline.append(da_price)
            da_zscore_history.append(da_price)
            if len(da_zscore_history) > config.arbitrage_lookback_periods:
                da_zscore_history.pop(0)

    return EconomicEvalResult(
        policy=policy, quantile_variant=quantile_variant, config=config, ticks=ticks
    )


def simulate(
    times: list[datetime],
    actuals: dict[str, dict[datetime, float]],
    config: EconomicEvalConfig,
    policy: AllocationPolicy,
    quantile_variant: QuantileVariant | None = None,
    forecast_maps: dict[str, dict[datetime, float]] | None = None,
) -> EconomicEvalResult:
    """
    The one deployable entry point (design §4). `policy` is a
    `Literal["even", "trailing", "model"]` -- **"oracle" is not a valid
    value**, checked explicitly below, so it can never reach
    `_simulate_core` through this function. `actuals` must have exactly the
    three keys `LEG_FCR_UP`/`LEG_FCR_DOWN`/`LEG_ARBITRAGE`, each a
    `{time: actual_clearing_price}` map used only for revenue realisation
    (and, for `trailing`, for the causal short/baseline windows) -- never
    read at or ahead of a tick's own allocation decision except to settle
    that same tick's already-realised price, exactly as
    `shared.bess_simulator.run_backtest` itself does.

    `policy="model"` requires `forecast_maps` (same three keys, `{time:
    forecast_value}`, one `quantile_variant`'s worth -- build via
    `build_forecast_maps`) and `quantile_variant`; both are `ValueError` if
    missing, so a caller cannot accidentally run with a forecast silently
    ignored or absent.
    """
    if policy not in ("even", "trailing", "model"):
        raise ValueError(
            f"policy must be one of 'even', 'trailing', 'model' -- got {policy!r}. "
            "'oracle' is deliberately not a valid policy here; use "
            "run_oracle_ceiling() for the (non-deployable) headroom ceiling."
        )
    if policy == "model":
        if forecast_maps is None:
            raise ValueError("policy='model' requires forecast_maps")
        if quantile_variant is None:
            raise ValueError("policy='model' requires quantile_variant")
    return _simulate_core(
        times,
        actuals,
        config,
        policy=policy,
        quantile_variant=quantile_variant,
        forecast_maps=forecast_maps,
        lookahead=False,
    )


def run_oracle_ceiling(
    times: list[datetime],
    actuals: dict[str, dict[datetime, float]],
    config: EconomicEvalConfig,
) -> EconomicEvalResult:
    """
    **The only lookahead path in this module** (design §1/§4): allocates
    every tick using the ACTUAL price realised AT that same tick -- the
    perfect-foresight ceiling, never a deployable policy. Deliberately a
    separate, unmistakably-named function rather than a value `simulate()`
    accepts, so a caller cannot reach it by passing a string through the
    normal policy path. Compute `headroom` (`compute_headroom` below) from
    this result and `simulate(..., policy="trailing")`'s -- that is the
    ceiling on what any forecast, however perfect, could add over
    persistence (design §1), and must be computed and reported before any
    `model` result is interpreted.
    """
    return _simulate_core(
        times,
        actuals,
        config,
        policy="oracle",
        quantile_variant=None,
        forecast_maps=None,
        lookahead=True,
    )


def restrict_to_scored_ticks(
    result: EconomicEvalResult, scored_times: set[datetime]
) -> EconomicEvalResult:
    """
    Returns a new `EconomicEvalResult` containing only the ticks whose
    `time` is in `scored_times`, `cumulative_*` fields recomputed over just
    that subset.

    **Why this exists:** `simulate()`/`run_oracle_ceiling()` must be run
    over the FULL fetched window (including the walk-forward folds' initial
    ~90-day training-only region, where `model` has no forecast yet) so the
    causal short/baseline windows have real history *before* the first
    scored tick -- an empty deque at the very first evaluated tick would
    force every policy into `_weighted_split`'s even-split fallback for no
    reason. But that warm-up region must NOT count toward the headline
    revenue comparison (design §2's "same simulator run, same window,
    differing only in allocation" -- a fair comparison needs `model` to
    only be scored where it actually has a forecast). This function slices
    the already-warmed-up result down to `scored_times` (the walk-forward
    test-fold ticks a forecast exists for) after the fact, rather than
    ever running `simulate()` itself over a truncated `times` list.
    """
    filtered = sorted((t for t in result.ticks if t.time in scored_times), key=lambda t: t.time)
    cumulative_capacity = 0.0
    cumulative_arbitrage = 0.0
    rebuilt: list[EconomicEvalTick] = []
    for tick in filtered:
        cumulative_capacity += tick.capacity_revenue_eur
        cumulative_arbitrage += tick.arbitrage_revenue_dkk
        rebuilt.append(
            replace(
                tick,
                cumulative_capacity_revenue_eur=cumulative_capacity,
                cumulative_arbitrage_revenue_dkk=cumulative_arbitrage,
            )
        )
    return EconomicEvalResult(
        policy=result.policy,
        quantile_variant=result.quantile_variant,
        config=result.config,
        ticks=rebuilt,
    )


# --- headroom and band-fraction (design §1/§2) -------------------------------


@dataclass(frozen=True)
class Headroom:
    """
    design §1: `oracle - trailing`, the ceiling on what any forecast could
    add over persistence, per currency bucket (module docstring's currency
    note -- capacity/EUR and arbitrage/DKK are never combined).
    """

    capacity_eur: float
    capacity_eur_fraction_of_trailing: float
    arbitrage_dkk: float
    arbitrage_dkk_fraction_of_trailing: float


def compute_headroom(oracle: EconomicEvalResult, trailing: EconomicEvalResult) -> Headroom:
    """Design §1's headroom diagnostic -- compute and report this FIRST,
    before any `model` result is interpreted (design's own instruction)."""
    cap_headroom = oracle.total_capacity_revenue_eur - trailing.total_capacity_revenue_eur
    arb_headroom = oracle.total_arbitrage_revenue_dkk - trailing.total_arbitrage_revenue_dkk
    cap_trailing = trailing.total_capacity_revenue_eur
    arb_trailing = trailing.total_arbitrage_revenue_dkk
    return Headroom(
        capacity_eur=cap_headroom,
        capacity_eur_fraction_of_trailing=(
            cap_headroom / cap_trailing if cap_trailing else float("nan")
        ),
        arbitrage_dkk=arb_headroom,
        arbitrage_dkk_fraction_of_trailing=(
            arb_headroom / arb_trailing if arb_trailing else float("nan")
        ),
    )


@dataclass(frozen=True)
class BandFraction:
    """design §2: `(model - trailing) / (oracle - trailing)`, per currency
    bucket -- the fraction of the available headroom the model captures.
    Positive: beats persistence. Near 1: approaches the oracle. <= 0: worse
    than trailing. `NaN` where headroom is exactly 0 (undefined ratio, not
    silently 0)."""

    capacity_eur: float
    arbitrage_dkk: float


def compute_band_fraction(
    model: EconomicEvalResult, trailing: EconomicEvalResult, oracle: EconomicEvalResult
) -> BandFraction:
    cap_headroom = oracle.total_capacity_revenue_eur - trailing.total_capacity_revenue_eur
    arb_headroom = oracle.total_arbitrage_revenue_dkk - trailing.total_arbitrage_revenue_dkk
    cap_gain = model.total_capacity_revenue_eur - trailing.total_capacity_revenue_eur
    arb_gain = model.total_arbitrage_revenue_dkk - trailing.total_arbitrage_revenue_dkk
    return BandFraction(
        capacity_eur=(cap_gain / cap_headroom if cap_headroom != 0 else float("nan")),
        arbitrage_dkk=(arb_gain / arb_headroom if arb_headroom != 0 else float("nan")),
    )


# --- forecast precompute: reuse P3/P3b's walk-forward machinery (design §4) -


def _walk_forward_predictions(
    dataset: JoinedDataset,
    folds: list[Fold],
    lookback: timedelta,
    config: ForecastModelConfig | None = None,
) -> dict[float, dict[datetime, float]]:
    """
    Mirrors `shared.forecast_model.run_model_walk_forward`'s per-fold-refit
    loop exactly -- same `effective_train_window(fold, lookback)` call, same
    per-fold `fit_quantile_model`, same leak discipline -- but returns
    **predictions** keyed by `(tau, time)` rather than pooled pinball loss.
    `run_model_walk_forward` itself is not reusable for this purpose: it
    only ever returns a `WalkForwardResult` (an aggregate loss scalar per
    tau), which an allocation policy cannot consume -- it needs the actual
    predicted price at each tick. Every fold's test-window tick gets
    exactly one prediction, from a model fit ONLY on that fold's own
    `effective_train_window` -- never later folds' data, identical
    per-fold-refit discipline to every other consumer of
    `fit_quantile_model` in this codebase.
    """
    config = config or ForecastModelConfig()
    predictions: dict[float, dict[datetime, float]] = {tau: {} for tau in config.quantiles}
    for fold in folds:
        if fold.test_start < fold.train_end:
            raise AssertionError(
                f"fold's test_start ({fold.test_start}) precedes its own train_end "
                f"({fold.train_end}) -- walk-forward invariant violated"
            )
        train_start, train_end = effective_train_window(fold, lookback)
        model = fit_quantile_model(dataset, train_start, train_end, config)

        test_mask = (dataset.time_epochs >= fold.test_start.timestamp()) & (
            dataset.time_epochs < fold.test_end.timestamp()
        )
        idx = np.where(test_mask)[0]
        if len(idx) == 0:
            continue
        preds = model.predict(dataset.X[idx])
        for row_i, data_i in enumerate(idx):
            t = dataset.times[data_i]
            for tau_i, tau in enumerate(config.quantiles):
                predictions[tau][t] = float(preds[row_i, tau_i])
    return predictions


def build_forecast_maps(
    leg_datasets: dict[str, JoinedDataset],
    folds: list[Fold],
    lookback: timedelta,
    model_config: ForecastModelConfig | None = None,
) -> dict[QuantileVariant, dict[str, dict[datetime, float]]]:
    """
    Runs `_walk_forward_predictions` once per leg (`leg_datasets` keyed by
    `LEG_FCR_UP`/`LEG_FCR_DOWN`/`LEG_ARBITRAGE`) and slices out BOTH
    `QUANTILE_VARIANT_TAU` columns from the same fitted models' predictions
    -- one walk-forward pass per leg, not one per (leg, quantile_variant),
    since `ForecastQuantileModel.predict` already returns every declared
    quantile in a single call (design §4: "report both [variants]", not "refit
    twice").
    """
    per_leg_predictions = {
        leg: _walk_forward_predictions(dataset, folds, lookback, model_config)
        for leg, dataset in leg_datasets.items()
    }
    return {
        variant: {leg: dict(per_leg_predictions[leg][tau]) for leg in per_leg_predictions}
        for variant, tau in QUANTILE_VARIANT_TAU.items()
    }
