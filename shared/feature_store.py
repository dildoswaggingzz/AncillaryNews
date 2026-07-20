"""
M6 P1: the leak-safe feature store for price forecasting
(docs/forecast-feature-store-design.md).

`build_features(db, zone, start, end, horizon)` returns one row per hourly
MTU (market time unit) in `[start, end)` for `zone`, containing only
features that were **knowable at `mtu_start - horizon`** (design §0). This
is not a "be careful" contract -- it is structural: a column that cannot be
proven safe at the requested `horizon` is never a key in the returned dict
at all, not present-with-a-null-value. See `tests/test_feature_store.py`
(written before this module, per the design's build order) for the four
required cases this guarantee is tested against.

**Two different vintage mechanisms, deliberately kept separate** (design
§4.1/§4.2, easy to conflate -- do not conflate them):

1. **Forecast-horizon columns** (`wind_solar_forecast`'s
   `*_day_ahead`/`*_intraday`/`*_5hour`/`*_1hour`, per dataset registry
   comment `shared/datasets.py`'s `forecasts_hour` entry). Each of these
   (except `_intraday`, see below) has a *name-encoded* minimum guaranteed
   lead time before its own MTU's delivery -- e.g. `*_1hour` is, by
   definition, never published later than 1h before delivery.
   `FORECAST_HORIZON_MIN_LEAD` below is that declared minimum, taken
   directly from design §4.1's "match the horizon column to the decision"
   mapping (`*_day_ahead` -> D-1 models' 12h canonical horizon; `*_5hour`/
   `*_1hour` -> intraday, deferred but the mapping is recorded now). A
   column's KEY is included in every row of one `build_features` call only
   when `horizon <= FORECAST_HORIZON_MIN_LEAD[name]` -- this is evaluated
   once per call (horizon is fixed for the whole call), so within one
   result set every row shares the same schema.

   `*_intraday` has **no declared minimum lead** anywhere in the design
   doc -- only `day_ahead`/`5hour`/`1hour` get one in §4.1's mapping. Rather
   than guess a number, `*_intraday` is never queried and never emitted at
   any horizon, in this module or in the `wind_revision_da_to_intraday`
   derived feature §5 describes (which depends on it). This is a
   deliberately conservative gap against an underspecified part of the
   design, not an oversight -- see the module's build report for the flag.

   `*_current_leaky_do_not_use_as_feature` (design §4.1) is denied
   **structurally**: the only forecast-horizon products this module ever
   constructs a query for are the names in `FORECAST_HORIZON_MIN_LEAD`
   (`day_ahead`/`5hour`/`1hour`) -- the leaky suffix is never one of them,
   so it is impossible for this module to query or emit it, at any horizon,
   not merely filtered out after the fact. `LEAKY_SUFFIX` exists only so a
   test can assert the invariant directly.

2. **Everything else** -- realised production/exchange, cross-zone
   day-ahead prices, imbalance, inertia, `aFRR_lfc_limits`, border ATC --
   has no forecast-horizon column at all. `fetched_at` (design §4.2) is
   **not** a substitute: it is the *ingestor's own poll time*
   (`shared/db_manager.py:save_market_data`, `fetched_at =
   datetime.now(UTC)` at insert), which for the P0 backfill means "when the
   backfill ran", not "when Energinet published this figure" -- using it as
   a vintage key would silently leak. The only trustworthy vintage signal
   these series have is **`time` itself** (the market time unit the row is
   about): a row is only ever read if `time <= mtu_start - horizon`, via an
   as-of ("most recent value at or before") join, mirroring
   `shared.bess_simulator._value_at_or_before`'s carry-forward semantics
   (re-implemented here rather than imported, to keep this module free of a
   dependency on an unrelated module's private helper).

   This is deliberately conservative for series that are, in the real
   world, cleared well ahead of their own `time` (FCR/aFRR_capacity auction
   results clear day-ahead, for instance) -- the design doc does not grant
   that assumption anywhere, and neither does this module.

**Every raw read goes through `DatabaseManager.fetch_series_values(...,
history=False)`**, which reads the `market_data` view
(`DISTINCT ON (time, market, zone, product) ... ORDER BY ... fetched_at
DESC`, `init-db/01-init.sql`) -- the dedupe design §4.3 requires (duplicate
revisions from an append-only, re-runnable backfill). No new SQL is
introduced for feature reads; the existing dedupe view supplies it. One
small addition to `DatabaseManager` supports this module's diagnostics
only, never feature reads: `fetch_zone_counts` (added alongside this
module, for the §4.5 zone-allowlist drop count).

**Schema determinism (coordinator review, post-smoke-run):** the returned
row's *key set* depends only on `(zone, horizon)`, never on what data
happens to exist in `[start, end]`. This matters because a training frame
and a serving frame built from different windows must be comparable -- a
feature store whose schema drifts with data availability silently produces
train/serve skew, the same "invisible until it costs money" failure class
as a leak, just one layer up. Concretely: `_corridors_for_zone` returns a
**declared, static** per-zone corridor list (`KNOWN_BORDER_CORRIDORS`
below), not a live discovery query -- a corridor with no rows in the
requested window still gets its columns, populated with `None`, not
omitted. (An earlier version of this module discovered corridors live via
`fetch_distinct_series` and dropped a corridor's columns entirely when it
had zero rows in-window, to satisfy a since-revised "no all-null columns"
acceptance bar -- verified live to break exactly the way described above:
`SE4-DK2` stopped publishing 2026-06-02 and `DK1-NL` stopped 2026-07-09,
so a "today"-anchored window and a months-old window produced different
schemas for the same zone. Fixed here; see `_log_all_null_columns` for the
replacement diagnostic.) The same reasoning is why every other column
group below (forecast-horizon columns aside, which are gated on `horizon`
alone) is always emitted regardless of data availability -- only `horizon`
and `zone` may ever change what keys a row has.

**§4.4** (`aFRR_border_atc` keyed by corridor, not bidding zone) is handled
by `_corridors_for_zone`: a corridor string is relevant to `zone` if either
`-`-joined endpoint equals it (`SE4-DK2`/`DK2-DK1` put the zone second) --
evaluated once, at import time, against the declared `KNOWN_BORDER_CORRIDORS`
list rather than a live query, per the determinism note above.

**§4.5** (`wind_solar_forecast`'s confirmed `UNDEFINED` zone pocket) can
never actually reach a returned row -- every query below asks for an
explicit `zone`, so an off-allowlist row is never selected in the first
place -- but `_log_wind_solar_zone_drops` still counts and logs how many
rows in the requested window sit outside `{"DK1", "DK2"}`, once per
`build_features` call, so a future pocket doesn't silently vanish with no
operator-visible signal. `_log_all_null_columns` is the general form of the
same idea, applied to every column after all rows are built: an all-null
column is logged (real operational signal -- a corridor/series with a data
gap covering the whole window) but never dropped from the schema.

**`atc_saturated` was removed** (coordinator review): design §5 asks for a
flag comparing realised cross-border flow against `aFRR_border_atc`'s
import/export limits, but that dataset (`AfrrBorderAvailableTransferCapacity`)
is specifically the transfer capacity *reserved for aFRR exchange*
(verified live: ~0-50 MW), not the interconnector's total commercial
capacity -- while `realtime_production_exchange.exchange_*` is *all*
cross-border flow (day-ahead, intraday, everything; verified live: up to
+-1000+ MW). Comparing them is comparing two different physical
quantities at very different scales; the resulting flag was close to
always-`True` whenever there was meaningful commercial flow, which is
worse than no flag at all. A correct version needs realised flow *of aFRR
exchange specifically*, which is not in the verified §3 inventory -- do
not re-add this comparison against `exchange_*` if that data becomes
available; it would reproduce the same error.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from shared.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

# --- constants -----------------------------------------------------------

ZONE_ALLOWLIST = frozenset({"DK1", "DK2"})  # design §4.5

FORECAST_TYPES: tuple[str, ...] = ("offshore_wind", "onshore_wind", "solar")

# Product-name suffix `shared/datasets.py`'s `forecasts_hour` entry ingests
# `ForecastCurrent` under (design §4.1). Exists only so
# `tests/test_feature_store.py` can assert the deny-rule directly; never
# consulted by any query this module issues (see module docstring §1).
LEAKY_SUFFIX = "_current_leaky_do_not_use_as_feature"

# Declared minimum guaranteed lead time (design §4.1) for each named
# forecast-horizon product. `*_intraday` is deliberately absent -- see
# module docstring.
FORECAST_HORIZON_MIN_LEAD: dict[str, timedelta] = {
    "day_ahead": timedelta(hours=12),
    "5hour": timedelta(hours=5),
    "1hour": timedelta(hours=1),
}

# design §5 "Cross-zone prices": DE is DK1's coupling partner, SE3/SE4 are
# DK2's (design §3's cross-zone note).
CROSS_ZONE_DAY_AHEAD_BY_ZONE: dict[str, tuple[str, ...]] = {
    "DK1": ("DE",),
    "DK2": ("SE3", "SE4"),
}

# Best-effort corridor -> realised cross-border flow product mapping
# (`realtime_production_exchange.exchange_*`), used only for the
# interconnector "saturated" flag (design §5). DE/NL map unambiguously by
# country name. `DK2-DK1` is the internal Storebælt (Great Belt) link.
# `SE4-DK2` is assumed to be the mainland Zealand<->Sweden connection
# (`exchange_sweden`) rather than the separate Bornholm<->SE4 cable
# (`exchange_bornholm_se4`) -- Bornholm is part of the DK2 price area but
# physically islanded from the Zealand grid, so this is a genuine ambiguity
# the design doc does not resolve; flagged in the build report. A corridor
# with no entry here still gets its raw `atc_import`/`atc_export` columns,
# just no `realised_flow`/`atc_saturated` columns (degrades gracefully, see
# `build_features`).
CORRIDOR_REALISED_FLOW_PRODUCT: dict[str, str] = {
    "DK1-DE": "exchange_germany",
    "DK2-DE": "exchange_germany",
    "DK1-NL": "exchange_netherlands",
    "DK2-DK1": "exchange_great_belt",
    "SE4-DK2": "exchange_sweden",
}

# Declared, static corridor registry for `aFRR_border_atc` (design §3,
# confirmed live 2026-07-20). **Deliberately static, not a live discovery
# query** -- see module docstring's "Schema determinism" note: a corridor
# that stops publishing (verified live for `SE4-DK2`/`DK1-NL`, see that
# note) must still appear in the schema with `None` values, not vanish, so
# a training frame and a serving frame built from different windows share
# the same columns. Adding a genuinely new corridor is a deliberate,
# reviewed code change to this constant, not something the code picks up
# on its own -- the right trade-off for a declared feature-store schema.
KNOWN_BORDER_CORRIDORS: tuple[str, ...] = ("DK1-DE", "DK1-NL", "DK2-DE", "DK2-DK1", "SE4-DK2")

# Bounded backward search depth for `wind_forecast_error_lag_1`'s "most
# recent settled MTU" scan (design §5) -- guarantees termination even
# across a data gap, never looks forward.
MAX_FORECAST_ERROR_LOOKBACK_HOURS = 24

COPENHAGEN_TZ = ZoneInfo("Europe/Copenhagen")


@dataclass(frozen=True)
class FeatureStoreConfig:
    """
    Knobs beyond `build_features`'s required interface (design §1) --
    dataclass config, no hidden globals, matching `shared/bess_simulator.py`'s
    `BessConfig` convention.
    """

    # How far before `start - horizon` to fetch RULE-B (as-of joined) raw
    # series, so the very first requested MTU's decision point can still
    # find a carried-forward value even for a sparsely-published series
    # (e.g. `aFRR_lfc_limits`, which only writes a row when a limit
    # actually changes). Purely a fetch-window sizing knob -- widening it
    # can only ever find an *older* value, never a value time-stamped later
    # than the decision point, so it cannot affect leak-safety either way.
    lookback_buffer: timedelta = timedelta(days=7)

    def __post_init__(self):
        if self.lookback_buffer < timedelta(0):
            raise ValueError("lookback_buffer cannot be negative")


# --- small internal helpers ------------------------------------------------


def _fetch_sorted_series(
    db: DatabaseManager,
    market: str,
    zone: str,
    product: str,
    time_from: datetime,
    time_to: datetime,
) -> list[tuple[datetime, float]]:
    """
    Ascending-by-time `(time, value)` pairs for one `(market, zone,
    product)` key in `[time_from, time_to]`, nulls dropped, deduped to the
    latest revision per `time` via `fetch_series_values(history=False)`'s
    `market_data` view read (design §4.3's dedupe requirement -- see module
    docstring).
    """
    rows = db.fetch_series_values(
        market, zone, product, limit=200_000, time_from=time_from, time_to=time_to, history=False
    )
    return sorted(
        ((r["time"], r["value"]) for r in rows if r["value"] is not None), key=lambda kv: kv[0]
    )


def _fetch_exact_map(
    db: DatabaseManager,
    market: str,
    zone: str,
    product: str,
    time_from: datetime,
    time_to: datetime,
) -> dict[datetime, float]:
    """Same as `_fetch_sorted_series`, as a `{time: value}` map for exact-MTU lookups."""
    return dict(_fetch_sorted_series(db, market, zone, product, time_from, time_to))


def _at_or_before(series: list[tuple[datetime, float]], t: datetime) -> float | None:
    """
    Latest value in `series` (ascending by time) whose time is `<= t`, or
    `None` if no such entry exists. This -- never `fetched_at` -- is the
    vintage bound every RULE-B feature is joined on (see module docstring
    §2). Mirrors `shared.bess_simulator._value_at_or_before`'s carry-forward
    semantics; re-implemented here (via `bisect`, since callers here reuse
    one series across many MTUs rather than scanning linearly per tick) to
    avoid a cross-module dependency on that function's leading underscore.
    """
    if not series:
        return None
    times = [ts for ts, _ in series]
    idx = bisect_right(times, t) - 1
    if idx < 0:
        return None
    return series[idx][1]


def _sum_or_none(*values: float | None) -> float | None:
    """Sums `values`, or `None` if any is `None` (a missing input, not a real zero)."""
    if any(v is None for v in values):
        return None
    return sum(values)


def _corridors_for_zone(zone: str) -> tuple[str, ...]:
    """
    Design §4.4: `aFRR_border_atc` is keyed by corridor (`"DK1-DE"`,
    `"SE4-DK2"`, ...), not bidding zone. A corridor is relevant to `zone` if
    EITHER `-`-joined endpoint equals it -- `SE4-DK2`/`DK2-DK1` put the
    zone second, so a naive prefix check would miss both. Evaluated against
    the declared `KNOWN_BORDER_CORRIDORS` registry (module docstring's
    "Schema determinism" note), not a live query -- a pure function of
    `zone` alone, so its result never varies with the requested window.
    """
    return tuple(c for c in KNOWN_BORDER_CORRIDORS if zone in c.split("-"))


def _log_wind_solar_zone_drops(db: DatabaseManager, time_from: datetime, time_to: datetime) -> None:
    """
    Design §4.5: `wind_solar_forecast` carries a confirmed `UNDEFINED` zone
    pocket (482 rows, 2026-05-27 -> 2026-05-29, 0.3% of that market).
    `build_features` can never actually *read* those rows -- every query it
    issues asks for an explicit `zone` -- but this logs how many rows in the
    requested window sit outside the `{"DK1", "DK2"}` allow-list, once per
    `build_features` call, so a future pocket (or this one, for a window
    that overlaps it) doesn't silently vanish with no operator-visible
    signal.
    """
    counts = db.fetch_zone_counts("wind_solar_forecast", time_from, time_to)
    dropped = sum(count for zone, count in counts.items() if zone not in ZONE_ALLOWLIST)
    if dropped:
        logger.warning(
            "wind_solar_forecast: %d row(s) outside the zone allow-list %s dropped in [%s, %s]",
            dropped,
            sorted(ZONE_ALLOWLIST),
            time_from,
            time_to,
        )


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian (Meeus/Jones/Butcher) Easter Sunday algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _danish_public_holidays(year: int) -> frozenset[date]:
    """
    Official Danish "helligdage" for `year`: fixed dates plus the Easter-
    relative set. Deliberately excludes "Store Bededag" (General Prayer
    Day), abolished as a public holiday from 2024 onward, and "Grundlovsdag"
    (Constitution Day, June 5), which is a half-day flag day, not a public
    holiday. Calendar facts, computed with no database read -- always safe
    at any horizon, since a holiday date is never information that could
    "leak" (it's knowable arbitrarily far in advance).
    """
    easter = _easter_sunday(year)
    return frozenset(
        {
            date(year, 1, 1),
            easter - timedelta(days=3),  # Maundy Thursday
            easter - timedelta(days=2),  # Good Friday
            easter,  # Easter Sunday
            easter + timedelta(days=1),  # Easter Monday
            easter + timedelta(days=39),  # Ascension Day
            easter + timedelta(days=49),  # Whit Sunday
            easter + timedelta(days=50),  # Whit Monday
            date(year, 12, 25),
            date(year, 12, 26),
        }
    )


def _calendar_features(mtu_start: datetime, decision_time: datetime) -> dict:
    """
    Pure calendar features (design §5) -- hour/day-of-week/month in local
    Danish time (demand/generation patterns follow local clock time, not
    UTC), a Danish public-holiday flag, and `is_after_d1_gate`.

    `is_after_d1_gate` is purely informational (design §7: gate-time-aware
    horizons are an explicitly out-of-scope later refinement -- the flat
    `timedelta horizon` is the only thing that actually gates what this
    module emits). It approximates the classic Nordic day-ahead gate as
    12:00 local time on the calendar day before `mtu_start`'s local date;
    this approximation is never used to decide inclusion of any other
    feature.
    """
    local = mtu_start.astimezone(COPENHAGEN_TZ)
    local_date = local.date()
    gate_date = (local - timedelta(days=1)).date()
    gate_time_local = datetime(
        gate_date.year, gate_date.month, gate_date.day, 12, 0, tzinfo=COPENHAGEN_TZ
    )
    return {
        "hour_of_day": local.hour,
        "day_of_week": local.weekday(),
        "month": local.month,
        "is_danish_public_holiday": local_date in _danish_public_holidays(local_date.year),
        "is_after_d1_gate": decision_time >= gate_time_local.astimezone(UTC),
    }


def _log_all_null_columns(
    zone: str, horizon: timedelta, start: datetime, end: datetime, rows: list[dict]
) -> None:
    """
    Logs which columns came back entirely `None` across every row of one
    `build_features` call -- real operational signal (a series/corridor
    with a data gap covering the whole requested window) now that the
    schema itself is never adjusted based on data availability (module
    docstring's "Schema determinism" note). Never removes a column; this is
    purely a log line.
    """
    if not rows:
        return
    keys = [k for k in rows[0] if k not in ("zone", "mtu_start")]
    all_null = [k for k in keys if all(r[k] is None for r in rows)]
    if all_null:
        logger.warning(
            "build_features(zone=%s, horizon=%s): %d column(s) entirely null across [%s, %s]: %s",
            zone,
            horizon,
            len(all_null),
            start,
            end,
            all_null,
        )


def _wind_forecast_error_lag_1(
    realised_exact: dict[str, dict[datetime, float]],
    forecast_maps: dict[tuple[str, str], dict[datetime, float]],
    decision_time: datetime,
) -> float | None:
    """
    Design §5's "realised forecast error, lagged": realised
    (`offshore_wind + onshore_wind`) minus the corresponding `*_day_ahead`
    forecast, for the most recent MTU at or before `decision_time`.

    Only `_day_ahead` is used as "the corresponding forecast" (the design
    doesn't specify which of the four horizon columns) -- it's the one
    column with a name-encoded guarantee wide enough to be meaningful
    regardless of the *current* row's own horizon (see module docstring).
    This is an assumption, flagged in the build report, not a verified
    design fact.

    Only ever looks at or before `decision_time` -- `candidate` starts at
    `decision_time` floored to the hour and only ever steps backward, up to
    `MAX_FORECAST_ERROR_LOOKBACK_HOURS` times, so this can never read a
    value published after the decision point regardless of data gaps.
    """
    candidate = decision_time.replace(minute=0, second=0, microsecond=0)
    if candidate > decision_time:
        candidate -= timedelta(hours=1)

    for _ in range(MAX_FORECAST_ERROR_LOOKBACK_HOURS):
        realised_off = realised_exact["offshore_wind"].get(candidate)
        realised_on = realised_exact["onshore_wind"].get(candidate)
        forecast_off = forecast_maps[("offshore_wind", "day_ahead")].get(candidate)
        forecast_on = forecast_maps[("onshore_wind", "day_ahead")].get(candidate)
        if None not in (realised_off, realised_on, forecast_off, forecast_on):
            return (realised_off + realised_on) - (forecast_off + forecast_on)
        candidate -= timedelta(hours=1)
    return None


# --- the builder -------------------------------------------------------------


def build_features(
    db: DatabaseManager,
    zone: str,
    start: datetime,
    end: datetime,
    horizon: timedelta,
    config: FeatureStoreConfig | None = None,
) -> list[dict]:
    """
    Returns one row per hourly MTU in `[start, end)` for `zone`, containing
    only features knowable at `mtu_start - horizon` (see module docstring
    for the two vintage mechanisms this enforces). Sorted by `mtu_start`.

    `start` is floored to the top of the hour (`minute=second=microsecond=0`)
    to define the hourly grid; `end` is exclusive. Both must be
    timezone-aware. Returns `[]` if the (floored) grid is empty.
    """
    if zone not in ZONE_ALLOWLIST:
        raise ValueError(f"zone must be one of {sorted(ZONE_ALLOWLIST)}, got {zone!r}")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware datetimes")
    if horizon < timedelta(0):
        raise ValueError("horizon cannot be negative")

    config = config or FeatureStoreConfig()

    grid_start = start.replace(minute=0, second=0, microsecond=0)
    mtu_grid: list[datetime] = []
    t = grid_start
    while t < end:
        mtu_grid.append(t)
        t += timedelta(hours=1)
    if not mtu_grid:
        return []

    fetch_start = grid_start - horizon - config.lookback_buffer
    fetch_end = mtu_grid[-1]

    _log_wind_solar_zone_drops(db, fetch_start, fetch_end)

    # --- forecast-horizon (RULE A) series: exact per-MTU maps ---
    forecast_maps: dict[tuple[str, str], dict[datetime, float]] = {}
    for ftype in FORECAST_TYPES:
        for horizon_name in FORECAST_HORIZON_MIN_LEAD:
            product = f"{ftype}_{horizon_name}"
            forecast_maps[(ftype, horizon_name)] = _fetch_exact_map(
                db, "wind_solar_forecast", zone, product, fetch_start, fetch_end
            )

    permitted_horizon_names = {
        name for name, min_lead in FORECAST_HORIZON_MIN_LEAD.items() if horizon <= min_lead
    }

    # --- RULE B series (as-of joined) ---
    realised_products = (
        "offshore_wind",
        "onshore_wind",
        "solar",
        "production_lt100mw",
        "production_ge100mw",
    )
    realised_series = {
        product: _fetch_sorted_series(
            db, "realtime_production_exchange", zone, product, fetch_start, fetch_end
        )
        for product in realised_products
    }
    realised_exact = {product: dict(series) for product, series in realised_series.items()}

    cross_zone_series = {
        cz: _fetch_sorted_series(db, "day_ahead", cz, "price", fetch_start, fetch_end)
        for cz in CROSS_ZONE_DAY_AHEAD_BY_ZONE.get(zone, ())
    }

    imbalance_products = ("imbalance_price", "afrr_vwa_up", "afrr_vwa_down")
    imbalance_series = {
        product: _fetch_sorted_series(db, "imbalance", zone, product, fetch_start, fetch_end)
        for product in imbalance_products
    }

    nordic_series: dict[str, list[tuple[datetime, float]]] = {}
    if zone == "DK2":
        nordic_series["inertia_nordic"] = _fetch_sorted_series(
            db, "inertia", "ALL", "nordic", fetch_start, fetch_end
        )
        nordic_series["afrr_lfc_limit_up"] = _fetch_sorted_series(
            db, "aFRR_lfc_limits", zone, "up", fetch_start, fetch_end
        )
        nordic_series["afrr_lfc_limit_down"] = _fetch_sorted_series(
            db, "aFRR_lfc_limits", zone, "down", fetch_start, fetch_end
        )

    # Declared, static per-zone corridor list (module docstring's "Schema
    # determinism" note) -- every corridor here gets columns in every row,
    # `None`-valued if it has no data in range, never omitted. Verified live
    # that this matters: `SE4-DK2` (last published 2026-06-02) and `DK1-NL`
    # (last published 2026-07-09) can both have zero rows in a plausible
    # window.
    corridors = _corridors_for_zone(zone)
    corridor_series: dict[str, dict[str, list[tuple[datetime, float]]]] = {}
    for corridor in corridors:
        entry = {
            "import": _fetch_sorted_series(
                db, "aFRR_border_atc", corridor, "import", fetch_start, fetch_end
            ),
            "export": _fetch_sorted_series(
                db, "aFRR_border_atc", corridor, "export", fetch_start, fetch_end
            ),
        }
        flow_product = CORRIDOR_REALISED_FLOW_PRODUCT.get(corridor)
        if flow_product:
            entry["flow"] = _fetch_sorted_series(
                db, "realtime_production_exchange", zone, flow_product, fetch_start, fetch_end
            )
        corridor_series[corridor] = entry

    rows: list[dict] = []
    for mtu_start in mtu_grid:
        decision_time = mtu_start - horizon
        row: dict = {"zone": zone, "mtu_start": mtu_start}
        row.update(_calendar_features(mtu_start, decision_time))

        # --- RULE A: forecast-horizon raw columns, horizon-gated ---
        for ftype in FORECAST_TYPES:
            for horizon_name in FORECAST_HORIZON_MIN_LEAD:
                if horizon_name not in permitted_horizon_names:
                    continue
                row[f"{ftype}_{horizon_name}"] = forecast_maps[(ftype, horizon_name)].get(mtu_start)

        # --- forecast revision (design §5), gated on both endpoints ---
        if {"5hour", "1hour"} <= permitted_horizon_names:
            wind_total = 0.0
            wind_total_valid = True
            for ftype in FORECAST_TYPES:
                v5 = forecast_maps[(ftype, "5hour")].get(mtu_start)
                v1 = forecast_maps[(ftype, "1hour")].get(mtu_start)
                revision = v1 - v5 if v5 is not None and v1 is not None else None
                row[f"{ftype}_revision_5h_to_1h"] = revision
                if ftype != "solar":
                    if revision is None:
                        wind_total_valid = False
                    else:
                        wind_total += revision
            row["wind_revision_5h_to_1h_total"] = wind_total if wind_total_valid else None

        # --- RULE B: realised production/exchange, as-of joined ---
        for product, series in realised_series.items():
            row[f"realised_{product}"] = _at_or_before(series, decision_time)

        total_production = _sum_or_none(
            row["realised_production_lt100mw"], row["realised_production_ge100mw"]
        )
        wind_and_solar = _sum_or_none(
            row["realised_offshore_wind"], row["realised_onshore_wind"], row["realised_solar"]
        )
        row["residual_production"] = (
            total_production - wind_and_solar
            if total_production is not None and wind_and_solar is not None
            else None
        )

        # --- cross-zone day-ahead prices, as-of joined ---
        for cz, series in cross_zone_series.items():
            row[f"day_ahead_price_{cz}"] = _at_or_before(series, decision_time)

        # --- imbalance, as-of joined ---
        for product, series in imbalance_series.items():
            row[product] = _at_or_before(series, decision_time)

        # --- Nordic system state (DK2 only), as-of joined ---
        for name, series in nordic_series.items():
            row[name] = _at_or_before(series, decision_time)

        # --- interconnector ATC/realised flow per relevant corridor ---
        # No "saturated" flag (design §5 asked for one; removed -- see
        # module docstring's "atc_saturated was removed" note: comparing
        # aFRR-reserved ATC against total commercial flow compares two
        # different physical quantities).
        for corridor, series_map in corridor_series.items():
            slug = corridor.replace("-", "_")
            row[f"atc_import_{slug}"] = _at_or_before(series_map["import"], decision_time)
            row[f"atc_export_{slug}"] = _at_or_before(series_map["export"], decision_time)
            row[f"realised_flow_{slug}"] = _at_or_before(series_map.get("flow", []), decision_time)

        # --- realised wind forecast error, lagged (design §5) ---
        row["wind_forecast_error_lag_1"] = _wind_forecast_error_lag_1(
            realised_exact, forecast_maps, decision_time
        )

        rows.append(row)

    _log_all_null_columns(zone, horizon, start, end, rows)

    return rows
