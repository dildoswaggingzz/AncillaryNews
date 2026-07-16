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
                saved = db.save_market_data(records, dataset)
                logger.info("Saved %d row(s) for dataset %s", saved, dataset.name)
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status="success").inc()
            except Exception:
                logger.exception("Save failed for dataset %s", dataset.name)
                DATASET_POLL_TOTAL.labels(dataset=dataset.name, status="save_failed").inc()
    finally:
        await ingestor.close()
        db.close()
        CYCLE_DURATION.observe(time.monotonic() - cycle_start)


async def main():
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
