"""
M2 rule engine: trigger classes from README §4, computed from
`market_data_history` (see `shared/db_manager.py` for the query methods this
module relies on).

Implemented trigger classes (feasible with what's actually ingested per
`shared/datasets.py` — no fake data, no invented fields):

- **Price spike / anomaly** ("Activation price spike" / "Capacity price
  anomaly" rows in README §4): a value more than `PRICE_SPIKE_STD_THRESHOLD`
  standard deviations from its own recent per-`(market, zone, product)`
  history.
- **Zone divergence** (part of "Abnormal pricing pattern"): DK1 vs DK2 price
  divergence for the same `(market, product)` beyond what recent paired
  history would predict. Guarded by `shared/units.py:same_currency` before
  ever computing a DK1-DK2 diff -- `("FCR", "price")` is DKK in DK1 and EUR
  in DK2 (see `shared/datasets.py`'s `fcr_dk2` entry), and subtracting those
  is a unit artifact, not a market signal. This module calls into
  `shared/units.py` for that check rather than hardcoding "FCR" (or any
  other market name) here, keeping this module free of market-name literals
  -- see `run_rule_engine`.
- **Negative/zero price flag** (part of "Abnormal pricing pattern"): a
  non-positive value for a product whose history shows non-positive values
  are rare.
- **Revision alert**: a later `fetched_at` row for the same
  `(time, market, zone, product)` whose value differs from an earlier
  `fetched_at` row beyond tolerance — see `init-db/01-init.sql` for why
  `fetched_at` (not a true `published_at`) is the available proxy.

Explicitly SKIPPED (no underlying data yet — see `docs/dataset-catalogue.md`
and `shared/datasets.py` module docstring):

- **Volume anomaly** — no volume fields are ingested by any `DatasetConfig`,
  only prices. Nothing to compute this from.
- **Structural events** (interconnector outages / UMMs / EAM platform
  incidents) — requires ENTSO-E and/or NBM ingestion, not built until a
  later milestone.

Every threshold-based check degrades gracefully: with fewer than
`MIN_HISTORY_POINTS` historical points to build a baseline from, it logs
"insufficient history, skipping" and returns no trigger, rather than
false-triggering on a near-empty series or crashing on a `StatisticsError`.
This matters most right after a fresh deployment, before real history has
accumulated.
"""

import logging
import statistics
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from prometheus_client import Counter

from shared.db_manager import DatabaseManager
from shared.units import same_currency

logger = logging.getLogger(__name__)

# README §7: "trigger rates". Labelled by trigger_type so Grafana can break
# down price_spike vs. negative_or_zero_price vs. zone_divergence vs.
# revision_alert rates independently.
TRIGGER_FIRED_TOTAL = Counter(
    "rule_engine_trigger_fired_total",
    "Rule-engine triggers fired, by trigger_type",
    ["trigger_type"],
)

# Minimum number of historical (baseline) data points required before any
# threshold-based check will fire. Below this, we don't have enough signal
# to distinguish "anomaly" from "normal noise in a thin sample".
MIN_HISTORY_POINTS = 30

# How many raw history rows to pull per (market, zone, product) series.
HISTORY_FETCH_LIMIT = 1000

PRICE_SPIKE_STD_THRESHOLD = 3.0
ZONE_DIVERGENCE_STD_THRESHOLD = 3.0

# A non-positive value is only flagged if historically non-positive values
# have occurred less than this fraction of the time for that series.
RARE_NON_POSITIVE_MAX_RATE = 0.05

# Revision alert tolerance: a revision must exceed both the absolute and
# relative tolerance to fire (avoids flagging float noise on near-zero
# values, and avoids flagging trivial revisions on large values).
REVISION_ABS_TOLERANCE = 1.0
REVISION_REL_TOLERANCE = 0.05


@dataclass
class Trigger:
    """A single raw rule-engine trigger, ready to serialize for Slack."""

    trigger_type: str
    market: str
    zone: str
    product: str
    value: float
    time: str
    baseline: float | None = None
    threshold: float | None = None
    details: str = ""
    detected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


def _dedupe_latest_per_time(raw_rows: list[dict]) -> list[tuple]:
    """
    Collapses raw (possibly multi-revision) rows down to one value per
    `time` — the most recently fetched revision — in chronological order.

    `raw_rows` is expected ordered `time DESC, fetched_at DESC` (as returned
    by `DatabaseManager.fetch_history`), so the first row seen per `time` is
    always its latest revision.
    """
    latest_value_by_time: dict = {}
    times_in_desc_order = []
    for row in raw_rows:
        t = row["time"]
        if t not in latest_value_by_time:
            latest_value_by_time[t] = row["value"]
            times_in_desc_order.append(t)
    times_in_desc_order.reverse()
    return [(t, latest_value_by_time[t]) for t in times_in_desc_order]


def check_price_spike(market: str, zone: str, product: str, raw_rows: list[dict]) -> Trigger | None:
    """Flags the latest value if it's PRICE_SPIKE_STD_THRESHOLD std devs from recent history."""
    series = _dedupe_latest_per_time(raw_rows)
    if len(series) < MIN_HISTORY_POINTS + 1:
        logger.info(
            "insufficient history, skipping price_spike check for %s/%s/%s (%d point(s))",
            market,
            zone,
            product,
            len(series),
        )
        return None

    *history, latest = series
    latest_time, latest_value = latest
    values = [v for _, v in history if v is not None]
    if latest_value is None or len(values) < MIN_HISTORY_POINTS:
        return None

    mean = statistics.mean(values)
    try:
        stdev = statistics.stdev(values)
    except statistics.StatisticsError:
        return None
    if stdev == 0:
        return None

    z_score = (latest_value - mean) / stdev
    if abs(z_score) < PRICE_SPIKE_STD_THRESHOLD:
        return None

    if z_score > 0:
        direction = mean + PRICE_SPIKE_STD_THRESHOLD * stdev
    else:
        direction = mean - PRICE_SPIKE_STD_THRESHOLD * stdev
    return Trigger(
        trigger_type="price_spike",
        market=market,
        zone=zone,
        product=product,
        value=latest_value,
        time=str(latest_time),
        baseline=mean,
        threshold=direction,
        details=f"z-score={z_score:.2f} over {len(values)} historical point(s)",
    )


def check_negative_or_zero(
    market: str, zone: str, product: str, raw_rows: list[dict]
) -> Trigger | None:
    """Flags a non-positive latest value when non-positive values are historically rare."""
    series = _dedupe_latest_per_time(raw_rows)
    if len(series) < MIN_HISTORY_POINTS + 1:
        logger.info(
            "insufficient history, skipping negative_or_zero check for %s/%s/%s (%d point(s))",
            market,
            zone,
            product,
            len(series),
        )
        return None

    *history, latest = series
    latest_time, latest_value = latest
    if latest_value is None or latest_value > 0:
        return None

    values = [v for _, v in history if v is not None]
    if len(values) < MIN_HISTORY_POINTS:
        return None

    non_positive_rate = sum(1 for v in values if v <= 0) / len(values)
    if non_positive_rate >= RARE_NON_POSITIVE_MAX_RATE:
        return None

    return Trigger(
        trigger_type="negative_or_zero_price",
        market=market,
        zone=zone,
        product=product,
        value=latest_value,
        time=str(latest_time),
        baseline=non_positive_rate,
        threshold=RARE_NON_POSITIVE_MAX_RATE,
        details=(
            f"value {latest_value} <= 0, while historically non-positive only "
            f"{non_positive_rate:.2%} of {len(values)} point(s)"
        ),
    )


def check_zone_divergence(
    market: str, product: str, dk1_raw_rows: list[dict], dk2_raw_rows: list[dict]
) -> Trigger | None:
    """Flags DK1 vs DK2 divergence for the same market/product beyond recent paired history."""
    dk1_series = dict(_dedupe_latest_per_time(dk1_raw_rows))
    dk2_series = dict(_dedupe_latest_per_time(dk2_raw_rows))
    common_times = sorted(t for t in dk1_series if t in dk2_series)

    if len(common_times) < MIN_HISTORY_POINTS + 1:
        logger.info(
            "insufficient paired history, skipping zone_divergence check for %s/%s "
            "(%d shared point(s))",
            market,
            product,
            len(common_times),
        )
        return None

    diffs = [dk1_series[t] - dk2_series[t] for t in common_times]
    *history_diffs, latest_diff = diffs
    latest_time = common_times[-1]

    mean = statistics.mean(history_diffs)
    try:
        stdev = statistics.stdev(history_diffs)
    except statistics.StatisticsError:
        return None
    if stdev == 0:
        return None

    z_score = (latest_diff - mean) / stdev
    if abs(z_score) < ZONE_DIVERGENCE_STD_THRESHOLD:
        return None

    return Trigger(
        trigger_type="zone_divergence",
        market=market,
        zone="DK1_vs_DK2",
        product=product,
        value=latest_diff,
        time=str(latest_time),
        baseline=mean,
        threshold=mean + ZONE_DIVERGENCE_STD_THRESHOLD * stdev,
        details=f"DK1-DK2 diff z-score={z_score:.2f} over {len(history_diffs)} paired point(s)",
    )


def check_revisions(market: str, zone: str, product: str, raw_rows: list[dict]) -> list[Trigger]:
    """
    Flags each `time` that has more than one `fetched_at` revision whose most
    recent two values differ beyond tolerance — the practical "revision
    alert" given `fetched_at` is only a proxy for a true `published_at`
    (see module docstring / init-db/01-init.sql).
    """
    rows_by_time: dict = {}
    for row in raw_rows:
        rows_by_time.setdefault(row["time"], []).append(row)

    triggers = []
    for t, rows in rows_by_time.items():
        if len(rows) < 2:
            continue
        rows_sorted = sorted(rows, key=lambda r: r["fetched_at"])
        older, newer = rows_sorted[-2], rows_sorted[-1]
        old_value, new_value = older["value"], newer["value"]
        if old_value is None or new_value is None:
            continue

        tolerance = max(REVISION_ABS_TOLERANCE, REVISION_REL_TOLERANCE * abs(old_value))
        if abs(new_value - old_value) <= tolerance:
            continue

        triggers.append(
            Trigger(
                trigger_type="revision_alert",
                market=market,
                zone=zone,
                product=product,
                value=new_value,
                time=str(t),
                baseline=old_value,
                threshold=tolerance,
                details=(
                    f"revised {old_value} -> {new_value} between fetched_at "
                    f"{older['fetched_at']} and {newer['fetched_at']}"
                ),
            )
        )
    return triggers


async def run_rule_engine(db: DatabaseManager) -> list[Trigger]:
    """
    Evaluates every trigger class above across every `(market, zone, product)`
    series currently in `market_data_history` and persists each fired
    trigger (init-db/03-triggers.sql, Phase 5's `GET /triggers`).

    Raw triggers are **no longer** auto-posted to Slack (that was M2
    behavior; it produced one Slack message per fired trigger — most of
    which never survive citation validation into a synthesized Event
    Report — which the user experienced as spam). Persisted triggers remain
    fully queryable via `GET /triggers` and the `/dashboard/triggers` page
    (services/api/main.py) as the "manual pull" replacement. The only
    automatic Slack push left in the pipeline is
    `shared.slack_notifier.send_event_report_alert`, fired once a
    synthesized, citation-validated Event Report is published (see
    `shared/event_synthesizer.py` / `services/orchestrator/main.py`) — that
    is the "interpretation" the user wants prioritized over raw signal.

    Called on its own schedule by `services/orchestrator/main.py`'s
    `run_synthesis_cycle` (README §9 M4) — no longer coupled to the
    ingestion poll cadence, and no longer called from
    `services/ingestor/main.py`. Every fired Trigger returned here also
    feeds the orchestrator's RAG + LLM synthesis pipeline on top of the
    persistence call below, which this function's own behavior is unaware
    of and unaffected by.
    """
    series_keys = db.fetch_distinct_series()
    if not series_keys:
        logger.info("No series in market_data_history yet; rule engine has nothing to evaluate")
        return []

    history_by_key: dict[tuple[str, str, str], list[dict]] = {}
    for market, zone, product in series_keys:
        history_by_key[(market, zone, product)] = db.fetch_history(
            market, zone, product, limit=HISTORY_FETCH_LIMIT
        )

    triggers: list[Trigger] = []

    for (market, zone, product), raw_rows in history_by_key.items():
        spike = check_price_spike(market, zone, product, raw_rows)
        if spike:
            triggers.append(spike)

        neg_zero = check_negative_or_zero(market, zone, product, raw_rows)
        if neg_zero:
            triggers.append(neg_zero)

        triggers.extend(check_revisions(market, zone, product, raw_rows))

    # Zone divergence needs a paired (market, product) present in both DK1
    # and DK2; only check each pair once regardless of iteration order.
    checked_pairs = set()
    for market, zone, product in series_keys:
        if zone != "DK1":
            continue
        pair_key = (market, product)
        if pair_key in checked_pairs:
            continue
        checked_pairs.add(pair_key)

        dk1_rows = history_by_key.get((market, "DK1", product))
        dk2_rows = history_by_key.get((market, "DK2", product))
        if not dk1_rows or not dk2_rows:
            continue

        if not same_currency((market, "DK1", product), (market, "DK2", product)):
            logger.info(
                "skipping zone_divergence check for %s/%s: DK1 and DK2 are denominated in "
                "different currencies (a unit artifact, not a market signal) -- see "
                "shared/units.py",
                market,
                product,
            )
            continue

        divergence = check_zone_divergence(market, product, dk1_rows, dk2_rows)
        if divergence:
            triggers.append(divergence)

    for trigger in triggers:
        TRIGGER_FIRED_TOTAL.labels(trigger_type=trigger.trigger_type).inc()

        try:
            db.save_trigger(trigger.to_dict())
        except Exception:
            logger.exception("Failed to persist trigger: %s", trigger.trigger_type)

    return triggers
