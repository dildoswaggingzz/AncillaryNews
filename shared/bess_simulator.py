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

   **Per-currency buckets, never summed across currencies.** Each configured
   leg's currency is resolved via `shared/units.py:currency_for` (backed by
   `shared/datasets.py`'s registry) and every tick's revenue is accumulated
   into `capacity_revenue_by_currency`, not one flat total. This is the fix
   for a real, live defect: DK2's FCR price (`("FCR", "price")`) is EUR/MW/h
   while DK1's is DKK/MW/h and DK2's `aFRR_capacity` is also DKK/MW/h (see
   `shared/datasets.py`'s `fcr_dk2` comment) -- a DK2 run configured with
   both FCR and aFRR_capacity legs previously summed EUR and DKK figures
   into one `capacity_revenue_dkk`/`cumulative_total_revenue_dkk` number.
   `capacity_revenue_dkk`/`cumulative_capacity_revenue_dkk`/
   `cumulative_total_revenue_dkk` now mean **DKK legs only**;
   `capacity_revenue_eur`/`cumulative_capacity_revenue_eur`/
   `BacktestResult.total_capacity_revenue_eur` carry the EUR legs
   separately, mirroring the aFRR-activation treatment in §3 below (never
   converted, never combined -- see that section's rationale, which applies
   identically here). A leg whose currency can't be resolved at all (a
   registry gap, not a real "no data" case) raises `ValueError` in
   `run_backtest` rather than silently defaulting somewhere -- see that
   function's docstring.

   **Allocation mode (`BessConfig.capacity_allocation`).** The "evenly
   across groups" split described above is the `"even"` mode -- the global
   default, kept that way so a stored run's persisted `config` JSONB always
   reproduces the exact numbers it was run with (`shared/db_manager.py:
   save_bess_run`). It has a real hazard, though: adding a group that
   currently clears at/near 0 (e.g. FFR -- see `shared/datasets.py`'s
   `ffr_dk2` entry, prices currently `0.0`) as a third group shrinks the
   other, genuinely-earning groups' shares (1/2 each -> 1/3 each) while
   itself earning nothing, so *total* modelled capacity revenue drops
   purely from the split, not from anything about the added market itself
   -- a user ticking "include FFR" would wrongly conclude FFR is
   unprofitable, having actually seen an allocation artifact. `"price_ranked"`
   fixes this: each group's share is weighted by its **relative strength**
   -- each leg's own recent trailing price against its *own* longer-run
   average, not a raw price level -- causally (see `_group_commit_shares`/
   `_leg_relative_strength` -- no lookahead, mirroring `_causal_zscore`'s
   discipline), so a group trailing near its own normal level ranks the
   same regardless of which currency or magnitude it happens to be
   denominated in, and a group trailing at/near 0 relative to its own
   history shrinks toward its sibling groups instead of diluting them.
   **Deliberately never ranks on raw price magnitude**: an earlier version
   of this function did, which silently compared price *levels* across
   currencies (e.g. DK2's ~20 DKK/MW/h aFRR_capacity vs. ~2 EUR/MW/h FCR)
   and let the larger-magnitude currency dominate purely because its
   numbers are bigger -- the same unit-mixing bug class this module's
   per-currency capacity buckets (§2 above) exist to catch, just
   resurfacing inside the allocator instead of the revenue totals. The
   total committed MW is conserved exactly either way (`arbitrage_power_mw`
   is unaffected by allocation mode -- it depends only on the configured
   `capacity_commit_mw` scalar, never on how many/which groups share it).

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
from typing import Literal

from shared.db_manager import DatabaseManager
from shared.units import DKK_PER_EUR, currency_for

# Mirrors shared/rule_engine.py's MIN_HISTORY_POINTS pattern: below this many
# trailing price points, the arbitrage strategy has no baseline to compare
# against and stays idle rather than false-triggering on a thin sample.
MIN_ARBITRAGE_HISTORY_POINTS = 5

# "price_ranked" capacity allocation (`_group_commit_shares`) ranks each
# capacity-market leg by its *own* recent trailing price relative to its
# *own* longer-run average -- never a raw price level (see that function's
# docstring for why: comparing raw levels silently compares across
# currencies). Two windows per leg are needed: a short "how is this leg
# doing right now" window, and a longer "what's normal for this leg"
# baseline it's measured against. The short window reuses
# `BessConfig.arbitrage_lookback_periods` (no second short-window knob to
# configure); this constant sets the baseline window as a multiple of it.
# 4x is a deliberate middle ground, not an arbitrary round number: too
# small (close to 1x) and the baseline just re-measures the same recent
# ticks the short window already captures, collapsing every ratio toward
# ~1.0 regardless of what's actually happening; too large (multiple
# dozens x) and a backtest needs an impractically long warm-up before any
# leg's baseline is well-established, during which `_group_commit_shares`
# has little choice but to fall back to even allocation. 4x keeps the
# baseline meaningfully longer/smoother than the short window while still
# reaching a stable estimate within a realistic backtest window.
PRICE_RANKED_BASELINE_MULTIPLIER = 4

# Markets excluded per the domain constraint: BESS cannot currently
# participate in mFRR in these markets. Never read by this module.
# `mFRR_capacity_extra` (shared/datasets.py's mfrr_capacity_extra entry,
# Energinet's afternoon "extra auction" on top of `mFRR_capacity`) is the
# same domain rule applied to the same underlying market -- an extra auction
# doesn't change what a BESS can bid into.
EXCLUDED_MARKETS = frozenset({"mFRR_capacity", "mFRR_EAM", "mFRR_capacity_extra"})

# Product string for every energy market `BessConfig.energy_markets` can name
# OTHER than the day-ahead-driving series itself (day-ahead's own product is
# `price_market`/`price_product` above, never this table -- one source of
# truth for the series that drives the tick timeline). P3
# (docs/bess-cooptimizer-design.md §6): `imbalance`'s product is
# `imbalance_price` per `shared/datasets.py`'s `imbalance_price` dataset
# (`ImbalancePriceDKK`, DKK/MWh -- the same currency as day_ahead, so no new
# currency handling is needed for it anywhere in this module or
# `shared/bess_dispatch_milp.py`).
ENERGY_MARKET_PRODUCT: dict[str, str] = {
    "imbalance": "imbalance_price",
}


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
    # How each period's capacity_commit_mw is split across capacity_markets'
    # distinct market groups -- see module docstring §2's "Allocation mode"
    # for the full rationale/hazard this exists to fix. "even" (the global
    # default) always has, and still does, split flat 1/n_groups regardless
    # of price; "price_ranked" weights each group by its own causal
    # relative strength (recent trailing price vs. its own longer-run
    # average -- unit-free, never a raw price level, so a DKK leg and a EUR
    # leg at the same relative strength always rank equally) instead. Kept
    # at "even" by default so a stored run's persisted `config` JSONB
    # always reproduces the exact numbers it was run with -- opt into
    # "price_ranked" per run, most importantly for any config that includes
    # a market prone to clearing at/near 0 (FFR today).
    capacity_allocation: Literal["even", "price_ranked"] = "even"

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

    # --- strategy selection ---
    # "threshold" (the default, and the only strategy this field's addition
    # changes anything about) is the causal z-score heuristic documented
    # above -- every existing stored run's persisted `config` JSONB
    # (`shared/db_manager.py:save_bess_run`) omits this field entirely, and
    # `dataclasses.asdict` under an *older* code version never wrote it, so
    # defaulting here to "threshold" is what makes an old persisted config
    # keep reproducing identically when re-run (json.load simply supplies
    # the default for a key that isn't present). "cooptimized" routes
    # `run_backtest` to `shared/bess_dispatch_milp.py`'s perfect-foresight
    # linear program instead -- see that module's docstring for the
    # formulation. This is a pure strategy switch: every other `BessConfig`
    # field (`power_mw`, `capacity_mwh`, SoC band, `round_trip_efficiency`,
    # `capacity_markets`, ...) means the same physical battery under either
    # strategy. `capacity_commit_mw`/`capacity_allocation` are threshold-only
    # concepts, though -- the co-optimizer decides period-by-period how much
    # (if any) capacity to reserve itself (that fixed-then-co-optimized
    # split is exactly the defect docs/bess-cooptimizer-design.md §0 point 2
    # describes), so those two fields are read but not acted on by
    # "cooptimized" runs.
    strategy: Literal["threshold", "cooptimized"] = "threshold"

    # --- shared with the co-optimizer only (docs/bess-cooptimizer-design.md §2) ---
    # T_act: the *energy-endurance* duration (hours) a committed reserve MW
    # must be able to sustain out of stored SoC before the co-optimizer will
    # let it be offered -- **not** a ramp/response time (a BESS ramps in
    # seconds and trivially meets every FAT requirement; energy endurance,
    # not speed, is what actually limits how much reserve it can honestly
    # commit). Meaningless to, and ignored by, the "threshold" strategy
    # (which has no such headroom constraint at all -- the exact defect the
    # co-optimizer exists to fix). Default 0.25 h (one 15-min MTU) -- see
    # `shared/bess_dispatch_milp.py` module docstring for the worked sanity
    # check of when this does/doesn't bind relative to `power_mw`.
    activation_endurance_hours: float = 0.25

    # --- co-optimizer-only: dispatchable energy markets (docs/bess-cooptimizer-design.md §6) ---
    # Every energy market the co-optimizer's LP is allowed to dispatch
    # against, sharing the ONE SoC/power budget with each other --
    # `("day_ahead",)` (the default) reproduces P1/P2's single-energy-market
    # behaviour exactly (regression-tested,
    # tests/test_bess_dispatch_milp.py). Adding `"imbalance"` (P3) lets the
    # LP route discharge to whichever of {day_ahead, imbalance} pays more
    # and charge to whichever costs less, each period, since a BESS is a
    # *controllable* asset that CHOOSES its imbalance exposure rather than
    # merely settling a forecast-error deviation the way a non-dispatchable
    # generator would (the design doc's earlier "passive settlement" lean,
    # its §9, was correct for that case but wrong for a battery, and is
    # superseded -- see its §6). `imbalance`'s product is `imbalance_price`,
    # DKK/MWh -- the SAME currency as day_ahead (`ENERGY_MARKET_PRODUCT`
    # above), so both energy markets live entirely inside
    # `shared/bess_dispatch_milp.py`'s Solve 1 -- no new currency handling.
    # **Threshold-strategy runs ignore this field entirely** (same posture
    # as `activation_endurance_hours` above) -- that engine stays
    # day_ahead-only, reading `price_market`/`price_product` exactly as it
    # did before this field existed.
    energy_markets: tuple[str, ...] = ("day_ahead",)

    # --- co-optimizer-only: post vs. pre foresight (docs/bess-cooptimizer-design.md §5) ---
    # "perfect" (the default) is P1/P2's behaviour: the LP optimises
    # against, and every tick's revenue is reported at, the SAME actual/
    # realised prices -- a perfect-foresight oracle, not a deployable
    # policy. "forecast" is P3's pre mode: the LP optimises against a
    # CAUSAL forecast of each schedulable series (P3's concrete source is
    # lag-24h persistence, `_lag24h_forecast` below) to fix a schedule
    # (`ch`/`dis`/`cap`), then that FIXED schedule's revenue is reported at
    # actual/settlement prices (`Σ actual_price · scheduled_flow`) --
    # `shared/bess_dispatch_milp.py`'s module docstring documents the
    # schedule/settlement mechanism in full. The post − pre gap is the
    # monetary value of forecast skill; with the lag-24h forecast this gap
    # is a conservative *floor* on that value (a richer forecast, e.g. the
    # M6 LightGBM models in `shared/forecast_model.py`, is a documented
    # later hook, not P3 scope, and could only narrow the gap further).
    # `foresight="forecast"`'s realised total is always <=
    # `foresight="perfect"`'s on the same window: the fixed forecast-driven
    # schedule is itself one feasible schedule the perfect-foresight problem
    # could also have chosen, so its actual-settled value can never exceed
    # the perfect-foresight optimum (tests/test_bess_dispatch_milp.py's
    # `pre <= post` gate). **Threshold-strategy runs ignore this field
    # entirely** -- there is no schedule/settlement split in that engine,
    # it always reads and reports actual prices tick by tick.
    foresight: Literal["perfect", "forecast"] = "perfect"

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
        if self.capacity_allocation not in ("even", "price_ranked"):
            raise ValueError(
                f"capacity_allocation must be 'even' or 'price_ranked', "
                f"got {self.capacity_allocation!r}"
            )
        if self.strategy not in ("threshold", "cooptimized"):
            raise ValueError(
                f"strategy must be 'threshold' or 'cooptimized', got {self.strategy!r}"
            )
        if self.activation_endurance_hours <= 0:
            raise ValueError("activation_endurance_hours must be positive")
        if not self.energy_markets:
            raise ValueError("energy_markets must not be empty")
        if len(set(self.energy_markets)) != len(self.energy_markets):
            raise ValueError(
                f"energy_markets must not contain duplicates, got {self.energy_markets!r}"
            )
        for market in self.energy_markets:
            if market in EXCLUDED_MARKETS:
                raise ValueError(
                    f"energy market {market!r} is not eligible for BESS participation "
                    "(mFRR_capacity/mFRR_EAM are excluded — see module docstring)"
                )
            if market not in ("day_ahead", "imbalance"):
                raise ValueError(
                    "energy_markets entries must be one of ('day_ahead', 'imbalance') for P3 "
                    f"(the tuple is kept general for a future intraday entry, P4), got {market!r}"
                )
        if self.foresight not in ("perfect", "forecast"):
            raise ValueError(f"foresight must be 'perfect' or 'forecast', got {self.foresight!r}")


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
    # DKK capacity-reservation legs ONLY (module docstring §2) -- a DK2 run
    # with any EUR-denominated leg (e.g. FCR) never mixes it in here; see
    # `capacity_revenue_eur` below for that leg's own total.
    capacity_revenue_dkk: float
    capacity_revenue_by_market: dict[str, float]
    cumulative_arbitrage_revenue_dkk: float
    # DKK capacity legs ONLY, same scope as `capacity_revenue_dkk` above.
    cumulative_capacity_revenue_dkk: float
    # arbitrage (DKK) + capacity (DKK legs only) -- never includes
    # `capacity_revenue_eur`/`afrr_activation_revenue_eur` below.
    cumulative_total_revenue_dkk: float
    # True when max_cycles_per_day (not power/SoC) capped this tick's discharge
    cycle_cap_binding: bool = False
    # aFRR energy activation revenue (module docstring §3) -- reported in
    # EUR, separately from every DKK field above; never summed into
    # cumulative_total_revenue_dkk.
    afrr_activation_revenue_eur: float = 0.0
    cumulative_afrr_activation_revenue_eur: float = 0.0
    # EUR capacity-reservation legs (module docstring §2, e.g. DK2's FCR) --
    # reported separately from every DKK field above, never converted or
    # summed into them.
    capacity_revenue_eur: float = 0.0
    cumulative_capacity_revenue_eur: float = 0.0


@dataclass
class BacktestResult:
    zone: str
    start_time: datetime
    end_time: datetime
    config: BessConfig
    ticks: list[BessTick] = field(default_factory=list)
    # How many periods each configured capacity leg cleared at exactly 0 --
    # a *real, present* price of 0 (e.g. FFR today, see shared/datasets.py's
    # ffr_dk2 entry), distinct from "no price data available at all" (which
    # capacity_revenue_by_market already silently treats as 0 revenue with
    # no further visibility). Lets a caller say "FFR cleared at 0 for
    # 720/720 hours in this window" rather than just showing a flat zero
    # with no context on whether that's typical or an anomaly. Keyed the
    # same way as capacity_revenue_by_market ("{market}:{product}").
    zero_price_periods_by_leg: dict[str, int] = field(default_factory=dict)
    # True if "price_ranked" capacity_allocation (BessConfig) ever had to
    # fall back to an even split because every configured group's trailing
    # price was 0 at some tick -- most commonly the backtest's very first
    # tick(s), before any trailing history exists (causal allocation has
    # nothing to rank by yet; see `_group_commit_shares`). Always False for
    # "even" allocation, which never consults trailing prices at all.
    capacity_allocation_fell_back_to_even: bool = False

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
    def total_capacity_revenue_eur(self) -> float:
        """
        EUR capacity-reservation revenue (module docstring §2, e.g. DK2's
        FCR legs), separate from `total_capacity_revenue_dkk`'s DKK-only
        total above -- never summed with it, same "separate currency
        buckets, never converted" posture as `total_afrr_activation_revenue_eur`.
        """
        return self.ticks[-1].cumulative_capacity_revenue_eur if self.ticks else 0.0

    @property
    def currencies_present(self) -> frozenset[str]:
        """
        Currencies with nonzero capacity revenue across the run. `len() > 1`
        means this run's capacity revenue is genuinely not summable to one
        number -- callers (dashboard templates, the morning-brief synthesis
        prompts) must show every currency's total separately rather than
        picking/combining one, and can use this property to decide whether
        that "not summable" framing is even needed.
        """
        currencies = set()
        if self.total_capacity_revenue_dkk != 0.0:
            currencies.add("DKK")
        if self.total_capacity_revenue_eur != 0.0:
            currencies.add("EUR")
        return frozenset(currencies)

    @property
    def total_revenue_all_dkk(self) -> float:
        """
        Combined headline total at the fixed ERM II peg
        (`shared.units.DKK_PER_EUR`), in DKK -- a *presentation-layer*
        convenience on top of, never a replacement for, the unconverted
        per-currency totals above (docs/bess-cooptimizer-design.md §4.1).
        The EUR-denominated totals (`total_capacity_revenue_eur`,
        `total_afrr_activation_revenue_eur`) are converted at the fixed peg
        and added to the DKK-native totals; nothing here feeds back into any
        optimization objective or per-currency bucket -- see
        `shared/bess_dispatch_milp.py`'s module docstring for why a fixed
        policy peg is not the same class of bug as the floating-market-price
        mixing `shared/units.py` otherwise guards against. Always show this
        figure alongside the raw per-currency buckets, never in place of
        them (`currencies_present` flags when that matters).
        """
        return (self.total_arbitrage_revenue_dkk + self.total_capacity_revenue_dkk) + (
            self.total_capacity_revenue_eur + self.total_afrr_activation_revenue_eur
        ) * DKK_PER_EUR

    @property
    def total_revenue_all_eur(self) -> float:
        """
        The same combined headline total as `total_revenue_all_dkk`, for a
        EUR-thinking reader: the DKK-native totals are converted at the
        fixed peg instead, and the EUR-native totals added directly. See
        that property's docstring for the full rationale.
        """
        return (
            self.total_arbitrage_revenue_dkk + self.total_capacity_revenue_dkk
        ) / DKK_PER_EUR + (self.total_capacity_revenue_eur + self.total_afrr_activation_revenue_eur)

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


def _lag24h_forecast(actual_series: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """
    P3's pre-mode forecast source (docs/bess-cooptimizer-design.md §5,
    `BessConfig.foresight`): a causal lag-24h persistence of `actual_series`
    -- for each `(t, v)` point, the forecast value AT `t` is whatever
    `actual_series` itself carried at `t - 24h` (via `_value_at_or_before`,
    this module's usual carry-forward convention), falling back to the
    tick's own actual value `v` only when no `t - 24h` value exists at all
    (cold start -- the first ~24h of any window, before there is a full
    day of trailing history to lag from). Both branches are causal: the lag
    branch only ever reads a value from >=24h in the past; the cold-start
    fallback reads the CURRENT tick's own actual value, which is not
    lookahead (it is exactly what perfect-foresight mode would already use
    for that same tick) but does mean the very first day of a
    "forecast"-mode window isn't actually forecast-driven -- an explicit,
    honest limitation (docs/bess-cooptimizer-design.md §5 already frames
    the lag-24h gap as a floor estimate of forecast value, not an exact
    one, and this cold-start fallback only ever narrows that gap further,
    consistent with the "pre <= post" guarantee).

    A small, no-model-training, swappable-later helper -- wiring a richer
    forecast source (e.g. the M6 LightGBM day-ahead/FCR-D models,
    `shared/forecast_model.py`) as `run_backtest`'s schedule-price source is
    a documented future hook, not P3 scope; nothing about
    `shared/bess_dispatch_milp.py`'s schedule/settlement mechanism assumes
    this particular forecast function.
    """
    forecast: list[tuple[datetime, float]] = []
    for t, v in actual_series:
        lag_value = _value_at_or_before(actual_series, t - timedelta(hours=24))
        forecast.append((t, lag_value if lag_value is not None else v))
    return forecast


def _leg_relative_strength(short_history: deque[float], baseline_history: deque[float]) -> float:
    """
    Unit-free "how strong is this leg's price right now, relative to its
    *own* longer-run history" ratio: `mean(short_history) / mean(baseline_history)`,
    both windows drawn from the *same leg's own* past clearing prices (so
    always the same currency -- see `_group_commit_shares` for why this,
    not a raw price level, is what gets compared/averaged across legs).

    A flat, unremarkable leg (short mean == baseline mean, at any
    magnitude, in any currency) always ratios to ~1.0. A leg trailing
    unusually high relative to its own history ratios above 1.0; unusually
    low, below. `0.0` if either window is empty (no history yet) or the
    baseline mean is 0 or negative (an undefined/meaningless ratio -- most
    notably a leg that has *always* cleared at 0, e.g. FFR today: its own
    baseline is 0, so there is no "normal level" to be relatively strong
    against, and this returns 0 rather than raising `ZeroDivisionError`).
    Clipped at 0 on the low end (a negative ratio would be meaningless for
    weighting purposes even in the unlikely event of a negative price).
    """
    if not short_history or not baseline_history:
        return 0.0
    baseline_mean = statistics.mean(baseline_history)
    if baseline_mean <= 0:
        return 0.0
    return max(statistics.mean(short_history) / baseline_mean, 0.0)


def _group_commit_shares(
    legs_by_group: dict[str, list[tuple[str, str]]],
    capacity_price_short_history: dict[str, deque[float]],
    capacity_price_baseline_history: dict[str, deque[float]],
    capacity_commit_mw: float,
) -> tuple[dict[str, float], bool]:
    """
    `"price_ranked"` capacity allocation (`BessConfig.capacity_allocation`)
    for one tick: each market group's share of `capacity_commit_mw` is
    weighted by its **relative strength** -- the mean, across that group's
    legs, of each leg's own `_leg_relative_strength` (a leg's recent
    trailing price against its *own* longer-run baseline, never a raw price
    level). **Deliberately unit-free**: an earlier version of this function
    weighted groups by raw trailing *price*, which silently compared
    magnitudes across currencies -- e.g. DK2's aFRR_capacity (~20 DKK) vs.
    FCR (~2 EUR) -- and let a DKK leg dominate a EUR leg purely because DKK
    numbers happen to be bigger, the same unit-mixing bug class
    `shared/units.py` exists to catch elsewhere in this module (§2). Ranking
    on each leg's ratio to its own history instead means two legs at the
    *same relative strength* (e.g. both currently 2x their own normal
    level) always rank equally, regardless of currency or magnitude -- see
    `tests/test_bess_simulator.py`'s cross-currency regression test.

    **Causal by construction**, same discipline as `_causal_zscore` and the
    prior raw-price version: both `capacity_price_short_history` and
    `capacity_price_baseline_history` must only ever contain prices from
    ticks strictly before the one being allocated -- `run_backtest` appends
    each tick's own clearing price to both of its leg's deques only *after*
    using their pre-append contents for this function.

    A zero-relative-strength group's share shrinks toward its sibling
    groups (proportional reweighting, not a hard cutoff). The total
    committed MW across all groups is always conserved at exactly
    `capacity_commit_mw` (same as "even") when at least one group has
    positive relative strength; `arbitrage_power_mw` is computed
    independently of allocation mode (see `run_backtest`) and is therefore
    never affected by this function's output either way.

    Returns `(commit_per_group, fell_back_to_even)`. Falls back to an even
    split across every group -- and reports that via the second return
    value, for `BacktestResult.capacity_allocation_fell_back_to_even` -- if
    every group's relative strength is 0 (no history yet, or every leg's
    own baseline is itself 0/undefined) -- there is no signal at all to
    rank by.
    """
    n_groups = len(legs_by_group)
    if n_groups == 0:
        return {}, False

    group_relative_strength: dict[str, float] = {}
    for market, legs in legs_by_group.items():
        leg_ratios = [
            _leg_relative_strength(
                capacity_price_short_history[f"{m}:{product}"],
                capacity_price_baseline_history[f"{m}:{product}"],
            )
            for m, product in legs
        ]
        group_relative_strength[market] = statistics.mean(leg_ratios) if leg_ratios else 0.0

    total_relative_strength = sum(group_relative_strength.values())
    even_share = capacity_commit_mw / n_groups
    if total_relative_strength <= 0:
        return {market: even_share for market in legs_by_group}, True

    return (
        {
            market: capacity_commit_mw * (strength / total_relative_strength)
            for market, strength in group_relative_strength.items()
        },
        False,
    )


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

    Raises `ValueError` if any configured `capacity_markets` leg has no
    declared currency in `shared/datasets.py`'s registry (via
    `shared/units.py:currency_for`) -- fail loud rather than silently
    treating an unlabelled leg as "no currency"/0, since a silently
    unlabelled leg is exactly the kind of gap that let DKK and EUR get
    summed together in the first place (see module docstring §2).

    `config.strategy` (default `"threshold"`) selects between this module's
    causal heuristic (documented above) and `"cooptimized"`, which fetches
    the identical series (this function still owns every DB call either
    way) and delegates to `shared/bess_dispatch_milp.py`'s perfect-foresight
    linear program instead -- see that module's docstring for the
    formulation and docs/bess-cooptimizer-design.md for the full design.
    Both strategies return the same `BacktestResult`/`BessTick` shape, so
    every caller (the dashboard, `shared/bess_estimator.py`,
    `save_bess_run`) consumes either with no changes.
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
    # Resolved once per backtest, alongside the price series fetch above --
    # every leg's currency is a static registry fact (shared/units.py), not
    # something that varies per tick, so there's no reason to re-resolve it
    # inside the per-tick loop below.
    leg_currency: dict[str, str | None] = {}
    for legs in legs_by_group.values():
        for m, product in legs:
            key = f"{m}:{product}"
            capacity_series_by_leg[key] = _fetch_series(db, m, zone, product, start_time, end_time)
            leg_currency[key] = currency_for(m, zone, product)

    unknown_currency_legs = [k for k, c in leg_currency.items() if c is None]
    if unknown_currency_legs:
        raise ValueError(
            f"no unit declared for capacity leg(s) {unknown_currency_legs} in zone {zone!r}; "
            "add `unit=` to the SeriesConfig in shared/datasets.py"
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

    if config.strategy == "cooptimized":
        # Energy markets (P3, docs/bess-cooptimizer-design.md §6): the
        # day-ahead-driving series (`price_series`, from `price_market`/
        # `price_product` above) is reused for whichever `energy_markets`
        # entry matches `config.price_market` (almost always "day_ahead"),
        # avoiding a duplicate fetch of the identical rows; every other
        # configured energy market (e.g. "imbalance") is fetched fresh via
        # `ENERGY_MARKET_PRODUCT`'s product string.
        energy_series_by_market: dict[str, list[tuple[datetime, float]]] = {}
        for market in config.energy_markets:
            if market == config.price_market:
                energy_series_by_market[market] = price_series
            else:
                product = ENERGY_MARKET_PRODUCT.get(market)
                if product is None:
                    raise ValueError(
                        f"no known product string for energy market {market!r} -- add it to "
                        "ENERGY_MARKET_PRODUCT"
                    )
                energy_series_by_market[market] = _fetch_series(
                    db, market, zone, product, start_time, end_time
                )

        # Pre mode (P3, docs/bess-cooptimizer-design.md §5): a causal
        # lag-24h persistence forecast of every schedulable series -- energy
        # markets and capacity legs (`_lag24h_forecast`). `None` (perfect
        # mode, the default) tells `solve_cooptimized_dispatch` to schedule
        # against the same actual series it settles at -- P1/P2's behaviour,
        # unchanged.
        schedule_energy_series_by_market: dict[str, list[tuple[datetime, float]]] | None = None
        schedule_capacity_series_by_leg: dict[str, list[tuple[datetime, float]]] | None = None
        if config.foresight == "forecast":
            schedule_energy_series_by_market = {
                market: _lag24h_forecast(series)
                for market, series in energy_series_by_market.items()
            }
            schedule_capacity_series_by_leg = {
                key: _lag24h_forecast(series) for key, series in capacity_series_by_leg.items()
            }

        # Deferred import: `shared/bess_dispatch_milp.py` imports several
        # names from *this* module (`BessConfig`, `BessTick`,
        # `BacktestResult`, `_value_at_or_before`) at its own module level,
        # so importing it back here at this module's top level would be a
        # circular import. All the DB-touching work (fetching every series,
        # resolving currencies, the ValueError-on-unknown-currency check
        # above) is already done above and reused as-is -- the LP module
        # itself stays pure (no DB, no network; see its own docstring),
        # exactly the "fetch here, solve there" split
        # docs/bess-cooptimizer-design.md's P1 scope calls for.
        from shared.bess_dispatch_milp import solve_cooptimized_dispatch

        return solve_cooptimized_dispatch(
            zone=zone,
            start_time=start_time,
            end_time=end_time,
            config=config,
            price_series=price_series,
            capacity_series_by_leg=capacity_series_by_leg,
            leg_currency=leg_currency,
            activation_price_series=activation_price_series,
            energy_series_by_market=energy_series_by_market,
            schedule_energy_series_by_market=schedule_energy_series_by_market,
            schedule_capacity_series_by_leg=schedule_capacity_series_by_leg,
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
    # `arbitrage_power_mw` depends only on the configured `capacity_commit_mw`
    # scalar -- never on how many/which groups share it, or on which
    # allocation mode is in use (see BessConfig.capacity_allocation's
    # docstring: "price_ranked" reweights *within* the committed total, it
    # never changes the total itself).
    arbitrage_power_mw = max(config.power_mw - config.capacity_commit_mw, 0.0)

    # Per-leg rolling price histories for "price_ranked" allocation
    # (`_group_commit_shares`/`_leg_relative_strength`) -- maintained
    # unconditionally (cheap, and keeps the per-tick loop below branch-free
    # on this point) but only ever *read* when
    # `config.capacity_allocation == "price_ranked"`. Two windows per leg,
    # since ranking on relative strength (this leg's recent price vs. its
    # own longer-run average -- see PRICE_RANKED_BASELINE_MULTIPLIER's
    # comment for why this, not a raw price level) needs both a short
    # "recent" window and a longer "normal" baseline window to compare it
    # against.
    capacity_price_short_history: dict[str, deque[float]] = {
        key: deque(maxlen=config.arbitrage_lookback_periods) for key in capacity_series_by_leg
    }
    capacity_price_baseline_history: dict[str, deque[float]] = {
        key: deque(maxlen=config.arbitrage_lookback_periods * PRICE_RANKED_BASELINE_MULTIPLIER)
        for key in capacity_series_by_leg
    }

    cumulative_arbitrage = 0.0
    cumulative_capacity_dkk = 0.0
    cumulative_capacity_eur = 0.0
    cumulative_afrr_activation = 0.0
    ticks: list[BessTick] = []
    history: list[float] = []
    zero_price_periods_by_leg: dict[str, int] = defaultdict(int)
    capacity_allocation_fell_back_to_even = False

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
        # Per-currency buckets (module docstring §2) -- a leg's revenue lands
        # in exactly one bucket, keyed by its registry-declared currency
        # (resolved once above into `leg_currency`, never re-derived here).
        # `leg_currency` is guaranteed to hold no `None` values by this point
        # (the ValueError check above), so every leg has a real bucket to
        # land in.
        capacity_revenue_by_currency: dict[str, float] = defaultdict(float)
        capacity_reserved_mw = 0.0
        commit_per_group_this_tick: dict[str, float] = {}
        if n_groups:
            # Group-level commit shares for this tick: "even" is the flat
            # 1/n_groups split this module has always used; "price_ranked"
            # reweights by each leg's relative strength against its own
            # history (see BessConfig.capacity_allocation /
            # _group_commit_shares' docstrings -- deliberately unit-free,
            # never a raw price level). Either way the shares always sum to
            # config.capacity_commit_mw (barring a price_ranked fallback,
            # which is itself an even split) -- `arbitrage_power_mw` above
            # is unaffected regardless.
            if config.capacity_allocation == "price_ranked":
                commit_per_group_by_market, fell_back_to_even = _group_commit_shares(
                    legs_by_group,
                    capacity_price_short_history,
                    capacity_price_baseline_history,
                    config.capacity_commit_mw,
                )
                if fell_back_to_even:
                    capacity_allocation_fell_back_to_even = True
            else:
                even_share = config.capacity_commit_mw / n_groups
                commit_per_group_by_market = {market: even_share for market in legs_by_group}

            for market, legs in legs_by_group.items():
                commit_per_group = commit_per_group_by_market[market]
                commit_per_leg = commit_per_group / len(legs)
                commit_per_group_this_tick[market] = commit_per_group
                for m, product in legs:
                    key = f"{m}:{product}"
                    series = capacity_series_by_leg[key]
                    clearing_price = _value_at_or_before(series, t)
                    if clearing_price is None:
                        capacity_revenue_by_market[key] = 0.0
                        continue
                    if clearing_price == 0.0:
                        zero_price_periods_by_leg[key] += 1
                    revenue = clearing_price * commit_per_leg * dt_hours
                    capacity_revenue_by_market[key] = revenue
                    capacity_revenue_by_currency[leg_currency[key]] += revenue
                    capacity_reserved_mw += commit_per_leg
                    # Causal: this tick's own clearing price is only added
                    # to its leg's short/baseline trailing histories AFTER
                    # being used (via `_group_commit_shares`, called above,
                    # before this loop) for THIS tick's allocation weight --
                    # a later tick's weighting may see it, this tick's never
                    # does. Mirrors the arbitrage z-score `history.append`
                    # below.
                    capacity_price_short_history[key].append(clearing_price)
                    capacity_price_baseline_history[key].append(clearing_price)
        capacity_revenue_dkk_tick = capacity_revenue_by_currency["DKK"]
        capacity_revenue_eur_tick = capacity_revenue_by_currency["EUR"]
        cumulative_capacity_dkk += capacity_revenue_dkk_tick
        cumulative_capacity_eur += capacity_revenue_eur_tick

        # --- aFRR energy activation revenue (module docstring §3, always EUR,
        # never mixed into the DKK/EUR capacity buckets above) ---
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

        # Only feed *this* price into the baseline after the tick has been
        # decided, and cap the trailing window at arbitrage_lookback_periods
        # (oldest points fall off) — a rolling window, not an ever-growing one.
        history.append(price)
        if len(history) > config.arbitrage_lookback_periods:
            history.pop(0)

    return BacktestResult(
        zone=zone,
        start_time=start_time,
        end_time=end_time,
        config=config,
        ticks=ticks,
        zero_price_periods_by_leg=dict(zero_price_periods_by_leg),
        capacity_allocation_fell_back_to_even=capacity_allocation_fell_back_to_even,
    )
