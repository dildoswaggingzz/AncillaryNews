import asyncio
import logging
import os
import time
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Counter, Histogram

from shared.base_ingestor import BaseIngestor
from shared.datasets import DATASETS
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

# Port for this service's standalone Prometheus exposition endpoint (README
# §7: "poller health"). Independently scrapeable -- see docker-compose.yml /
# prometheus/prometheus.yml.
METRICS_PORT = int(os.getenv("METRICS_PORT", "9100"))

CYCLE_DURATION = Histogram(
    "ingestor_cycle_duration_seconds", "Duration of one full ingestion cycle"
)
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


async def run_ingestion_cycle():
    """
    Polls every dataset declared in shared/datasets.py and saves the results.

    A failure fetching or saving one dataset is logged and skipped rather
    than aborting the whole cycle, so a single misbehaving dataset doesn't
    take down polling for the rest (README §3A KPI: 100% polling uptime).
    """
    ingestor = BaseIngestor("https://api.energidataservice.dk")
    db = DatabaseManager()
    cycle_start = time.monotonic()

    try:
        logger.info("Starting ingestion cycle for %d dataset(s)...", len(DATASETS))
        for i, dataset in enumerate(DATASETS):
            if i > 0:
                await asyncio.sleep(RATE_LIMIT_SECONDS)

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
