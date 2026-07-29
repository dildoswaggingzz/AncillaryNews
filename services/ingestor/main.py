import asyncio
import logging
import os
import time
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Counter, Histogram

from shared.base_ingestor import BaseIngestor
from shared.datasets import DATASETS, DatasetConfig, parse_update_frequency_seconds
from shared.db_manager import DatabaseManager
from shared.logging_config import configure_logging
from shared.metrics import start_metrics_server

configure_logging()
logger = logging.getLogger(__name__)

INGESTION_INTERVAL_MINUTES = 15
# The dataset catalogue (docs/dataset-catalogue.md) observed a rate limit of
# ~1 request/second on api.energidataservice.dk during bulk discovery; pace
# sequential fetches within a cycle accordingly.
RATE_LIMIT_SECONDS = 1.0

# How many times to poll a dataset per its own declared publication cadence
# (`DatasetConfig.update_frequency` -- see that field's docstring for the
# live 429 bug this whole mechanism exists to fix). The Energi Data Service
# API guide's literal rule is "1 request per Update Frequency", but polling
# a `P1D` dataset exactly once a day would put its poll at an arbitrary
# phase relative to Energinet's publish time, so a D-1 auction result could
# sit up to 24h unseen. Dividing by 4 bounds worst-case staleness at a
# quarter of the cadence (6h for `P1D`, 3h for `P0.5D`, 90min for `PT6H`)
# while still cutting `fcr_dk1` from 96 requests/day to 4 -- a 24x
# reduction, and comfortably inside the rolling window Energinet was
# actually enforcing when it started 429ing.
#
# Staleness here is bounded delay, never data loss: every slow-cadence
# dataset in the registry is also a forward-publishing one polled with
# `start=now-P2D` (shared/datasets.py:FORWARD_PUBLISH_START), so each poll
# re-reads a 48h window and a later poll picks up everything an earlier one
# was too early to see.
POLLS_PER_UPDATE_FREQUENCY = 4

# Floor on the computed interval: a dataset never polls more often than the
# scheduler's own cycle, so `power_system_right_now` (`PT1M`) and the other
# fast datasets simply poll every cycle, exactly as they did before this
# mechanism existed. Expressed in seconds and derived from
# INGESTION_INTERVAL_MINUTES so the two can't drift apart.
MIN_POLL_INTERVAL_SECONDS = INGESTION_INTERVAL_MINUTES * 60

# Slack allowed when deciding whether a dataset is due, so a poll interval
# that is an exact multiple of the cycle interval doesn't fall just short of
# being due and slip a whole cycle. Without it, a dataset floored at exactly
# MIN_POLL_INTERVAL_SECONDS would need >= 900s to have elapsed between two
# cycles that are themselves only ~900s apart minus however long the
# previous cycle's own fetches took -- i.e. it would poll every *second*
# cycle, silently halving the rate for exactly the fast datasets this
# mechanism is supposed to leave untouched. Half a cycle is wide enough to
# absorb that drift and far too narrow to let a 6h-interval dataset come due
# a cycle early.
POLL_DUE_TOLERANCE_SECONDS = MIN_POLL_INTERVAL_SECONDS / 2

# Monotonic timestamp of the last poll ATTEMPT per dataset name -- the state
# `_should_poll` reads and `run_ingestion_cycle` writes. Keyed on attempt,
# not on success, deliberately: a dataset whose fetch failed has already
# exhausted `BaseIngestor.fetch_data`'s own 5-attempt server-directed retry
# budget by the time we see the failure, so retrying it again next cycle
# would be the exact 429-amplifying behavior this change exists to stop.
# It waits its full interval like any other dataset.
_last_poll_attempt: dict[str, float] = {}

# Port for this service's standalone Prometheus exposition endpoint (README
# §7: "poller health"). Independently scrapeable -- see docker-compose.yml /
# prometheus/prometheus.yml.
METRICS_PORT = int(os.getenv("METRICS_PORT", "9100"))

CYCLE_DURATION = Histogram(
    "ingestor_cycle_duration_seconds", "Duration of one full ingestion cycle"
)
# `status` values: "success" / "zero_rows" / "fetch_failed" / "save_failed"
# for a dataset actually requested this cycle, plus "skipped" for one that
# wasn't due yet under its own poll interval (`poll_interval_seconds`).
# "skipped" is counted rather than left silent on purpose: a dataset that
# has quietly stopped being polled at all -- a mis-declared
# `update_frequency`, say -- otherwise looks identical to a healthy one that
# simply isn't due, since neither emits any other status. Rate-of-skips vs.
# rate-of-successes per dataset makes that distinguishable in Prometheus.
DATASET_POLL_TOTAL = Counter(
    "ingestor_dataset_poll_total",
    "Per-dataset poll outcomes for one ingestion cycle",
    ["dataset", "status"],
)
# Stage 2 guardrail: per-(dataset, market, product) row counter, incremented
# every cycle for *every configured series* -- including a 0 increment for a
# series that mapped no rows this cycle (shared/db_manager.py:SaveResult's
# `by_series` always carries every configured series, not only the ones that
# got a row). This is what makes a typo'd `value_field` visible: a
# permanently-flat-at-zero series shows up as a real, queryable Prometheus
# time series (`.inc(0)` still registers the label combination) rather than
# a metric that simply never exists -- alertable via
# `increase(ingestor_series_rows_total[6h]) == 0` (see
# grafana/dashboards/ancillarynews.json's matching panel).
SERIES_ROWS_TOTAL = Counter(
    "ingestor_series_rows_total",
    "Rows written per (dataset, market, product) series, by ingestion cycle",
    ["dataset", "market", "product"],
)


def poll_interval_seconds(dataset: DatasetConfig) -> float:
    """
    How often `dataset` should actually be requested, in seconds: its own
    declared publication cadence divided by POLLS_PER_UPDATE_FREQUENCY,
    floored at MIN_POLL_INTERVAL_SECONDS (one scheduler cycle).

    Falls back to MIN_POLL_INTERVAL_SECONDS -- i.e. "poll every cycle", the
    behavior that predates this mechanism -- for a dataset with no declared
    or no parseable `update_frequency`. See
    `shared.datasets.parse_update_frequency_seconds` for why an unparseable
    value degrades rather than raises.
    """
    cadence = parse_update_frequency_seconds(dataset.update_frequency)
    if cadence is None:
        return MIN_POLL_INTERVAL_SECONDS
    return max(MIN_POLL_INTERVAL_SECONDS, cadence / POLLS_PER_UPDATE_FREQUENCY)


def _should_poll(dataset: DatasetConfig, now: float) -> bool:
    """
    Whether enough time has passed since `dataset`'s last poll attempt (see
    `_last_poll_attempt`) to request it again this cycle, within
    POLL_DUE_TOLERANCE_SECONDS. Always True the first time a dataset is
    seen, so a freshly-started service polls everything once immediately
    rather than waiting out a full interval it has no record of.
    """
    last = _last_poll_attempt.get(dataset.name)
    if last is None:
        return True
    return (now - last) >= poll_interval_seconds(dataset) - POLL_DUE_TOLERANCE_SECONDS


def reset_poll_state() -> None:
    """Clears `_last_poll_attempt`, so the next cycle polls every dataset. Exists for tests
    (which run several cycles in one process); nothing in the service calls it."""
    _last_poll_attempt.clear()


async def run_ingestion_cycle():
    """
    Polls the datasets declared in shared/datasets.py that are *due* this
    cycle and saves the results.

    "Due" is per-dataset, not per-cycle: each dataset is requested at a rate
    derived from its own Energinet-declared publication cadence
    (`poll_interval_seconds`), because api.energidataservice.dk rate-limits
    per dataset against exactly that cadence. Polling everything at the flat
    cycle interval is what put `fcr_dk1` into sustained HTTP 429 -- see
    `shared/datasets.py:DatasetConfig.update_frequency` for the full
    finding. Datasets whose cadence is at or below the cycle interval
    (`power_system_right_now`, `forecasts_hour`, ...) are still polled every
    cycle.

    A failure fetching or saving one dataset is logged and skipped rather
    than aborting the whole cycle, so a single misbehaving dataset doesn't
    take down polling for the rest (README §3A KPI: 100% polling uptime).
    """
    ingestor = BaseIngestor("https://api.energidataservice.dk")
    db = DatabaseManager()
    cycle_start = time.monotonic()

    try:
        due_names = {d.name for d in DATASETS if _should_poll(d, cycle_start)}
        logger.info(
            "Starting ingestion cycle for %d of %d dataset(s) due this cycle...",
            len(due_names),
            len(DATASETS),
        )
        requested_this_cycle = 0
        for dataset in DATASETS:
            if dataset.name not in due_names:
                logger.debug(
                    "Skipping dataset %s -- not due (update_frequency=%s, poll interval %.0fs)",
                    dataset.name,
                    dataset.update_frequency,
                    poll_interval_seconds(dataset),
                )
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status="skipped").inc()
                continue

            # Paced only against requests actually made this cycle -- a
            # skipped dataset must not leave a 1s hole where its fetch
            # would have been.
            if requested_this_cycle:
                await asyncio.sleep(RATE_LIMIT_SECONDS)
            requested_this_cycle += 1
            _last_poll_attempt[dataset.name] = time.monotonic()

            try:
                data = await ingestor.fetch_data(
                    f"dataset/{dataset.dataset_id}", params=dataset.params
                )
            except Exception:
                logger.exception("Fetch failed for dataset %s", dataset.name)
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status="fetch_failed").inc()
                continue

            records = data.get("records") if data else None
            if not records:
                logger.warning("No records received for dataset %s", dataset.name)
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status="no_records").inc()
                continue

            try:
                result = db.save_market_data(records, dataset)
                logger.info("Saved %d row(s) for dataset %s", result.total, dataset.name)
                # "zero_rows" (not "success") when every configured series
                # mapped nothing this cycle -- e.g. a typo'd value_field, or
                # (less alarmingly) a dataset whose columns are all
                # legitimately null right now (SERIES_ROWS_TOTAL's per-series
                # breakdown below is what actually distinguishes those two
                # cases over time, this status is just a coarse per-dataset
                # signal).
                status = "success" if result.total else "zero_rows"
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status=status).inc()
                for key, count in result.by_series.items():
                    market, product = key.split(":", 1)
                    SERIES_ROWS_TOTAL.labels(
                        dataset=dataset.name, market=market, product=product
                    ).inc(count)
            except Exception:
                logger.exception("Save failed for dataset %s", dataset.name)
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status="save_failed").inc()
    finally:
        await ingestor.close()
        db.close()
        CYCLE_DURATION.observe(time.monotonic() - cycle_start)


def _warn_on_missing_schema_columns():
    """
    Startup check (Stage 0's migration-runner fix, `scripts/migrate.py`):
    logs a warning -- never mutates schema itself -- if the live database is
    missing columns `init-db/*.sql`'s `ALTER TABLE ... ADD COLUMN` files
    declare (see `shared/db_manager.py:EXPECTED_SCHEMA_COLUMNS` /
    `check_expected_columns` for why this check exists: those files don't
    apply themselves to a pre-existing `pgdata` volume the way a fresh
    volume's `docker-entrypoint-initdb.d` run would).
    """
    db = DatabaseManager()
    try:
        missing = db.check_expected_columns()
        if missing:
            logger.warning(
                "Database schema is missing %d expected column(s): %s -- run "
                "`poetry run python scripts/migrate.py` against DATABASE_URL "
                "(see DEPLOYMENT.md) before relying on affected features.",
                len(missing),
                missing,
            )
    finally:
        db.close()


async def main():
    _warn_on_missing_schema_columns()
    start_metrics_server(METRICS_PORT)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_ingestion_cycle,
        "interval",
        minutes=INGESTION_INTERVAL_MINUTES,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
