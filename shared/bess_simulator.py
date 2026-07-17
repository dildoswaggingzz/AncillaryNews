"""
BESS (Battery Energy Storage System) backtest simulator.

Simulates a single generic grid-scale battery making simple threshold-based
charge/discharge/reservation decisions over *real* historical market data
already sitting in `market_data_history` (see `shared/datasets.py` for the
exact ingested market/zone/product strings this module reads), and estimates
the revenue it would have earned. This is a **backtest**, not a live/forward
dispatch service: `run_backtest` walks chronologically over a historical
`[start_time, end_time]` window and produces a tick-by-tick trace of
state-of-charge, the action taken, and cumulative revenue.

**Battery participation constraint:** per the current market rules a BESS
cannot participate in mFRR in these markets, so `mFRR_capacity` and
`mFRR_EAM` are never read here. Eligible markets are FCR (`FCR` — including
DK2's FCR-D up/down legs, `product="up"`/`"down"`, alongside FCR-N/DK1's
single `product="price"`), aFRR capacity (`aFRR_capacity`), aFRR energy
activation (`aFRR_energy` — read and, since this module's aFRR-activation
addition, turned into its own separately-reported EUR revenue stream; see §3
below), day-ahead (`day_ahead`), and imbalance (`imbalance`).

**Breaking change (capacity-market keys):** `BessTick.capacity_revenue_by_market`
is keyed by `"{market}:{product}"` (e.g. `"FCR:price"`, `"FCR:up"`,
`"FCR:down"`, `"aFRR_capacity:up"`), not bare `market`. This was fixed
alongside adding FCR-D support: two `capacity_markets` entries sharing one
`market` (e.g. `("FCR", "up")` and `("FCR", "down")`) would otherwise
silently collide and overwrite each other in that dict.

Three separate, deliberately simply-modeled revenue streams (README
"Brainstorming" §: "clearly labelled as an estimate" — every stream here is
an estimate, not a real co-optimized dispatch):

1. **Energy arbitrage** — a rolling mean/stdev threshold on the day-ahead
   price (`shared/rule_engine.py`'s baseline pattern: trailing-window
   mean/stdev, z-score against it), applied causally (only prices *before*
   the current tick feed the baseline, so the backtest never uses
   lookahead). Charge when the current price's z-score is at/below
   `-arbitrage_z_threshold`; discharge when at/above `+arbitrage_z_threshold`;
   otherwise idle. Every action respects the battery's power limit, usable
   SoC band, and round-trip efficiency.

2. **Capacity reservation revenue** — an *estimate*: each period,
   `capacity_commit_mw` is split evenly across the distinct *market groups*
   configured in `capacity_markets` (e.g. `"FCR"` and `"aFRR_capacity"` by
   default — see `BessConfig.capacity_markets`'s docstring for the two-level
   group/leg split this implies once a group like `"FCR"` has more than one
   leg, e.g. DK2's FCR-D up/down pair), held back from the power otherwise
   available for arbitrage, and "earns"
   `procured_clearing_price * committed_mw * period_duration_hours` using
   the real ingested FCR/aFRR capacity price series for the requested zone.
   This is explicitly **not** a real co-optimized dispatch: it assumes the
   commitment always clears, ignores any requirement to actually be able to
   deliver the reserved MW out of current SoC, and (for periods where a
   capacity price is configured but no price is available in the historical
   data) simply earns nothing for that market that period rather than
   guessing a value.

3. **aFRR energy activation revenue** — an *estimate*, reported separately in
   **EUR** (never summed into the DKK totals above — the ingested
   `aFRR_energy` dataset has no DKK field, and mixing currencies into one
   number would be misleading). Real PICASSO activation data
   (`aFRR_energy`/`activation_price`) is ingested but attributing
   system-wide activation to one hypothetical asset requires an assumption
   this module does not try to avoid: a flat configurable
   `afrr_activation_participation_rate` (default 0.3) is assumed activated
   out of whatever `aFRR_capacity` MW is committed that period, i.e.
   `activation_price * committed_aFRR_capacity_mw * participation_rate *
   period_duration_hours`. This is a directional simplification (real
   activation volume varies continuously, isn't a flat share of the
   capacity commitment, and this module does not read the system-wide
   activation-*volume* signal at all), not a real dispatch — see
   `BacktestResult.total_afrr_activation_revenue_eur`.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from shared.db_manager import DatabaseManager

# Mirrors shared/rule_engine.py's MIN_HISTORY_POINTS pattern: below this many
# trailing price points, the arbitrage strategy has no baseline to compare
# against and stays idle rather than false-triggering on a thin sample.
MIN_ARBITRAGE_HISTORY_POINTS = 5

# Markets excluded per the domain constraint: BESS cannot currently
# participate in mFRR in these markets. Never read by this module.
EXCLUDED_MARKETS = frozenset({"mFRR_capacity", "mFRR_EAM"})


@dataclass(frozen=True)
class BessConfig:
    """
    Battery + strategy parameters. Defaults describe a generic grid-scale
    unit (1 MW / 2 MWh, 2-hour duration) — not a specific real battery.

    These are configuration defaults for the backtest, not hardcoded
    assumptions baked into the strategy logic itself; every field can be
    overridden per run.
    """

    # --- physical battery parameters ---
    power_mw: float = 1.0
    capacity_mwh: float = 2.0
    round_trip_efficiency: float = 0.90  # AC-to-AC round trip
    soc_min_fraction: float = 0.10  # usable SoC band floor
    soc_max_fraction: float = 0.90  # usable SoC band ceiling
    starting_soc_fraction: float = 0.50

    # --- arbitrage strategy parameters ---
    arbitrage_lookback_periods: int = 30  # trailing window for mean/stdev
    arbitrage_z_threshold: float = 0.5  # charge below -z, discharge above +z
    price_market: str = "day_ahead"
    price_product: str = "price"

    # --- capacity reservation parameters ---
    # Total MW held back from arbitrage each period and offered across the
    # capacity markets below. Split in two levels, not evenly across every
    # raw (market, product) tuple: first evenly across the distinct *market
    # groups* present (the `market` half of each tuple, e.g. `"FCR"` vs.
    # `"aFRR_capacity"`), then each group's share evenly across however many
    # legs (products) that group has. This matters once a group has more
    # than one leg -- e.g. DK2's FCR-D pair, `(("FCR", "up"), ("FCR",
    # "down"))` -- since without the group-level split first, adding a
    # second FCR-D leg would silently dilute every *other* market's share
    # too (a real bug this two-level split fixes; see module docstring).
    # Default stays the DK1-safe two-group pair below; FCR-D is opt-in per
    # run (via extra `("FCR", "up")`/`("FCR", "down")` entries), never a new
    # default, since it's meaningless for DK1 (no FCR-D market there).
    capacity_commit_mw: float = 0.3
    capacity_markets: tuple[tuple[str, str], ...] = (("FCR", "price"), ("aFRR_capacity", "up"))

    # --- aFRR energy activation parameters ---
    # Fraction of the committed aFRR_capacity MW assumed activated each
    # period, driving the separately-reported EUR activation-revenue
    # estimate (module docstring §3). Only meaningful when "aFRR_capacity"
    # is one of `capacity_markets`' groups; otherwise activation revenue is
    # always 0.
    afrr_activation_participation_rate: float = 0.3

    # --- cycle cap ---
    # A contractual/warranty-style limit on how much the battery may
    # *discharge* (only discharge MWh counts, consistent with
    # `full_cycle_equivalents`'s definition) within any rolling 24-hour
    # window, expressed in full-capacity-equivalent cycles/day. `None` means
    # unconstrained (the theoretical-max mode the simulator ran in before
    # this field existed). Defaults to 1.5 -- a realistic illustrative
    # figure, not a hard physical constant -- since the morning-brief BESS
    # estimates (shared/bess_estimator.py) want a capped-by-default result.
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
        if self.capacity_commit_mw < 0:
            raise ValueError("capacity_commit_mw cannot be negative")
        if self.capacity_commit_mw > self.power_mw:
            raise ValueError("capacity_commit_mw cannot exceed power_mw")
        for market, _product in self.capacity_markets:
            if market in EXCLUDED_MARKETS:
                raise ValueError(
                    f"capacity market {market!r} is not eligible for BESS participation "
                    "(mFRR_capacity/mFRR_EAM are excluded — see module docstring)"
                )
        if self.price_market in EXCLUDED_MARKETS:
            raise ValueError(f"price market {self.price_market!r} is not eligible for BESS")
        if self.max_cycles_per_day is not None and self.max_cycles_per_day <= 0:
            raise ValueError("max_cycles_per_day must be positive (or None for unconstrained)")
        if not 0 <= self.afrr_activation_participation_rate <= 1:
            raise ValueError("afrr_activation_participation_rate must be in [0, 1]")


@dataclass
class BessTick:
    """One period's simulated state, action, and revenue."""

    time: datetime
    soc_mwh: float
    soc_fraction: float
    action: str  # "charge" | "discharge" | "idle"
    day_ahead_price: float | None
    energy_discharged_mwh: float  # grid-delivered energy this tick (0 unless action == "discharge")
    arbitrage_revenue_dkk: float
    capacity_reserved_mw: float
    capacity_revenue_dkk: float
    capacity_revenue_by_market: dict[str, float]
    cumulative_arbitrage_revenue_dkk: float
    cumulative_capacity_revenue_dkk: float
    cumulative_total_revenue_dkk: float
    # True when max_cycles_per_day (not power/SoC) capped this tick's discharge
    cycle_cap_binding: bool = False
    # aFRR energy activation revenue (module docstring §3) -- reported in
    # EUR, separately from every DKK field above; never summed into
    # cumulative_total_revenue_dkk.
    afrr_activation_revenue_eur: float = 0.0
    cumulative_afrr_activation_revenue_eur: float = 0.0


@dataclass
class BacktestResult:
    zone: str
    start_time: datetime
    end_time: datetime
    config: BessConfig
    ticks: list[BessTick] = field(default_factory=list)

    @property
    def total_arbitrage_revenue_dkk(self) -> float:
        return self.ticks[-1].cumulative_arbitrage_revenue_dkk if self.ticks else 0.0

    @property
    def total_capacity_revenue_dkk(self) -> float:
        return self.ticks[-1].cumulative_capacity_revenue_dkk if self.ticks else 0.0

    @property
    def total_revenue_dkk(self) -> float:
        return self.ticks[-1].cumulative_total_revenue_dkk if self.ticks else 0.0

    @property
    def total_afrr_activation_revenue_eur(self) -> float:
        """
        aFRR energy activation revenue (module docstring §3), in EUR.
        Deliberately **not** included in `total_revenue_dkk` above -- the
        ingested aFRR_energy dataset has no DKK field, and mixing EUR into a
        DKK total would misstate it. Report and compare this figure
        separately.
        """
        return self.ticks[-1].cumulative_afrr_activation_revenue_eur if self.ticks else 0.0

    @property
    def total_discharged_mwh(self) -> float:
        return sum(t.energy_discharged_mwh for t in self.ticks)

    @property
    def full_cycle_equivalents(self) -> float:
        """
        Total energy discharged to the grid (via arbitrage), divided by
        nameplate capacity — a common battery-health/utilization metric.
        Does not count capacity-reservation "throughput" (no energy is
        actually cycled for a capacity commitment in this estimate).
        """
        if not self.config.capacity_mwh:
            return 0.0
        return self.total_discharged_mwh / self.config.capacity_mwh


def _causal_zscore(history: list[float], current: float) -> float | None:
    """
    z-score of `current` against the mean/stdev of `history` (which must not
    include `current` itself — this is what makes the arbitrage strategy
    causal/no-lookahead in a backtest). Mirrors
    shared/rule_engine.py:check_price_spike's mean/stdev baseline pattern.
    Returns None if there isn't enough history or the history has zero
    variance (nothing to compare against).
    """
    if len(history) < MIN_ARBITRAGE_HISTORY_POINTS:
        return None
    mean = statistics.mean(history)
    try:
        stdev = statistics.stdev(history)
    except statistics.StatisticsError:
        return None
    if stdev == 0:
        return None
    return (current - mean) / stdev


def _value_at_or_before(sorted_series: list[tuple[datetime, float]], t: datetime) -> float | None:
    """
    Returns the value of the last entry in `sorted_series` (ascending by
    time) whose time is <= t, or None if no such entry exists. `FCR`/
    `aFRR_capacity` prices are hourly while the arbitrage tick cadence may be
    finer (e.g. 15-minute day-ahead MTUs), so a capacity price is carried
    forward to every arbitrage tick within its period rather than requiring
    an exact timestamp match.
    """
    result = None
    for time, value in sorted_series:
        if time > t:
            break
        result = value
    return result


def _fetch_series(
    db: DatabaseManager,
    market: str,
    zone: str,
    product: str,
    start_time: datetime,
    end_time: datetime,
) -> list[tuple[datetime, float]]:
    """
    Returns the (latest-revision) series for one (market, zone, product) key
    within [start_time, end_time], ascending by time, nulls dropped. Real
    Energinet data has plenty of per-record nulls (shared/datasets.py) so
    every caller of this helper must tolerate a short or empty result.
    """
    if market in EXCLUDED_MARKETS:
        raise ValueError(f"market {market!r} is not eligible for BESS participation")
    rows = db.fetch_series_values(
        market, zone, product, limit=100000, time_from=start_time, time_to=end_time, history=False
    )
    series = [(r["time"], r["value"]) for r in rows if r["value"] is not None]
    series.sort(key=lambda r: r[0])
    return series


def run_backtest(
    db: DatabaseManager,
    zone: str,
    start_time: datetime,
    end_time: datetime,
    config: BessConfig | None = None,
) -> BacktestResult:
    """
    Runs the BESS backtest strategy chronologically over real historical
    data for `zone` in `[start_time, end_time]`, pulled via `DatabaseManager`
    (`day_ahead` price ticks drive the arbitrage cadence; `FCR`/
    `aFRR_capacity` prices are looked up per tick via `_value_at_or_before`).

    Returns a `BacktestResult` with one `BessTick` per day-ahead price point
    in the window. If there are no day-ahead price points in the window, the
    result has an empty tick list (not an error) — this is a caller-facing
    "no data for this window" signal, not a crash.
    """
    config = config or BessConfig()

    price_series = _fetch_series(
        db, config.price_market, zone, config.price_product, start_time, end_time
    )

    # Two-level split: distinct market groups (the `market` half of each
    # `capacity_markets` tuple) first, then each group's legs (products) --
    # see BessConfig.capacity_markets' docstring for why (the commit-
    # dilution bug this fixes).
    legs_by_group: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for market, product in config.capacity_markets:
        legs_by_group[market].append((market, product))

    capacity_series_by_leg: dict[str, list[tuple[datetime, float]]] = {}
    for legs in legs_by_group.values():
        for m, product in legs:
            capacity_series_by_leg[f"{m}:{product}"] = _fetch_series(
                db, m, zone, product, start_time, end_time
            )

    # aFRR energy activation price series -- fetched once per backtest, only
    # if "aFRR_capacity" is actually a configured group (module docstring
    # §3); otherwise activation revenue is always 0 and there's no reason to
    # query it.
    activation_price_series: list[tuple[datetime, float]] = (
        _fetch_series(db, "aFRR_energy", zone, "activation_price", start_time, end_time)
        if "aFRR_capacity" in legs_by_group
        else []
    )

    soc_min = config.soc_min_fraction * config.capacity_mwh
    soc_max = config.soc_max_fraction * config.capacity_mwh
    soc_mwh = config.starting_soc_fraction * config.capacity_mwh

    # Symmetric split of round-trip efficiency across the charge and
    # discharge legs (a common simplifying convention for a single
    # round-trip figure) — documented here since it's a modelling choice,
    # not something Energinet or a datasheet hands us.
    leg_efficiency = config.round_trip_efficiency**0.5

    n_groups = len(legs_by_group)
    commit_per_group = config.capacity_commit_mw / n_groups if n_groups else 0.0
    arbitrage_power_mw = max(config.power_mw - config.capacity_commit_mw, 0.0)

    cumulative_arbitrage = 0.0
    cumulative_capacity = 0.0
    cumulative_afrr_activation = 0.0
    ticks: list[BessTick] = []
    history: list[float] = []

    # Rolling 24-hour discharge window for the cycle cap (see BessConfig's
    # `max_cycles_per_day` docstring for why this is rolling, not a
    # calendar-day reset): each entry is (time, discharged_mwh) for a tick
    # that discharged; entries older than `t - 24h` are pruned every tick
    # before the cap is applied.
    discharge_window: deque[tuple[datetime, float]] = deque()
    cap_mwh_per_window = (
        config.capacity_mwh * config.max_cycles_per_day
        if config.max_cycles_per_day is not None
        else None
    )

    for i, (t, price) in enumerate(price_series):
        # Period duration: gap to the next tick, or the gap from the
        # previous tick if this is the last one (falls back to 1 hour if
        # there's only a single tick in the whole window).
        if i + 1 < len(price_series):
            dt_hours = (price_series[i + 1][0] - t).total_seconds() / 3600.0
        elif i > 0:
            dt_hours = (t - price_series[i - 1][0]).total_seconds() / 3600.0
        else:
            dt_hours = 1.0
        dt_hours = max(dt_hours, 0.0)

        # --- capacity reservation (independent of the arbitrage decision) ---
        capacity_revenue_by_market: dict[str, float] = {}
        capacity_reserved_mw = 0.0
        commit_per_group_this_tick: dict[str, float] = {}
        if n_groups:
            for market, legs in legs_by_group.items():
                commit_per_leg = commit_per_group / len(legs)
                commit_per_group_this_tick[market] = commit_per_group
                for m, product in legs:
                    key = f"{m}:{product}"
                    series = capacity_series_by_leg[key]
                    clearing_price = _value_at_or_before(series, t)
                    if clearing_price is None:
                        capacity_revenue_by_market[key] = 0.0
                        continue
                    revenue = clearing_price * commit_per_leg * dt_hours
                    capacity_revenue_by_market[key] = revenue
                    capacity_reserved_mw += commit_per_leg
        capacity_revenue = sum(capacity_revenue_by_market.values())
        cumulative_capacity += capacity_revenue

        # --- aFRR energy activation revenue (module docstring §3, always EUR,
        # never mixed into the DKK capacity_revenue above) ---
        afrr_committed_mw = commit_per_group_this_tick.get("aFRR_capacity", 0.0)
        activation_price = (
            _value_at_or_before(activation_price_series, t) if afrr_committed_mw else None
        )
        afrr_activation_revenue = (
            activation_price
            * afrr_committed_mw
            * config.afrr_activation_participation_rate
            * dt_hours
            if activation_price is not None
            else 0.0
        )
        cumulative_afrr_activation += afrr_activation_revenue

        # --- arbitrage decision (causal: baseline excludes the current price) ---
        z = _causal_zscore(history, price)
        action = "idle"
        arbitrage_revenue = 0.0
        energy_discharged_mwh = 0.0
        cycle_cap_binding = False

        if z is not None and z <= -config.arbitrage_z_threshold:
            # Charge: draw energy from the grid, limited by available power,
            # remaining SoC headroom, and the charge-leg efficiency.
            max_energy_in_mwh = arbitrage_power_mw * dt_hours
            headroom_mwh = (soc_max - soc_mwh) / leg_efficiency if leg_efficiency else 0.0
            grid_energy_mwh = max(min(max_energy_in_mwh, headroom_mwh), 0.0)
            if grid_energy_mwh > 0:
                soc_mwh += grid_energy_mwh * leg_efficiency
                arbitrage_revenue = -price * grid_energy_mwh
                action = "charge"
        elif z is not None and z >= config.arbitrage_z_threshold:
            # Discharge: deliver energy to the grid, limited by available
            # power, remaining usable SoC, the discharge-leg efficiency, and
            # (if configured) the rolling-24h cycle cap.
            max_energy_out_mwh = arbitrage_power_mw * dt_hours
            available_mwh = (soc_mwh - soc_min) * leg_efficiency
            grid_energy_mwh = max(min(max_energy_out_mwh, available_mwh), 0.0)

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
                arbitrage_revenue = price * grid_energy_mwh
                energy_discharged_mwh = grid_energy_mwh
                action = "discharge"
                if cap_mwh_per_window is not None:
                    discharge_window.append((t, grid_energy_mwh))

        cumulative_arbitrage += arbitrage_revenue

        ticks.append(
            BessTick(
                time=t,
                soc_mwh=soc_mwh,
                soc_fraction=soc_mwh / config.capacity_mwh if config.capacity_mwh else 0.0,
                action=action,
                day_ahead_price=price,
                energy_discharged_mwh=energy_discharged_mwh,
                arbitrage_revenue_dkk=arbitrage_revenue,
                capacity_reserved_mw=capacity_reserved_mw,
                capacity_revenue_dkk=capacity_revenue,
                capacity_revenue_by_market=capacity_revenue_by_market,
                cumulative_arbitrage_revenue_dkk=cumulative_arbitrage,
                cumulative_capacity_revenue_dkk=cumulative_capacity,
                cumulative_total_revenue_dkk=cumulative_arbitrage + cumulative_capacity,
                cycle_cap_binding=cycle_cap_binding,
                afrr_activation_revenue_eur=afrr_activation_revenue,
                cumulative_afrr_activation_revenue_eur=cumulative_afrr_activation,
            )
        )

        # Only feed *this* price into the baseline after the tick has been
        # decided, and cap the trailing window at arbitrage_lookback_periods
        # (oldest points fall off) — a rolling window, not an ever-growing one.
        history.append(price)
        if len(history) > config.arbitrage_lookback_periods:
            history.pop(0)

    return BacktestResult(
        zone=zone, start_time=start_time, end_time=end_time, config=config, ticks=ticks
    )
