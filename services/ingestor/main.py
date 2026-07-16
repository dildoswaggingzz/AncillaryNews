import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from shared.base_ingestor import BaseIngestor
from shared.datasets import DATASETS
from shared.db_manager import DatabaseManager
from shared.rule_engine import run_rule_engine

# Konfigurer logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INGESTION_INTERVAL_MINUTES = 15
# The dataset catalogue (docs/dataset-catalogue.md) observed a rate limit of
# ~1 request/second on api.energidataservice.dk during bulk discovery; pace
# sequential fetches within a cycle accordingly.
RATE_LIMIT_SECONDS = 1.0


async def run_ingestion_cycle():
    """
    Polls every dataset declared in shared/datasets.py and saves the results.

    A failure fetching or saving one dataset is logged and skipped rather
    than aborting the whole cycle, so a single misbehaving dataset doesn't
    take down polling for the rest (README §3A KPI: 100% polling uptime).
    """
    ingestor = BaseIngestor("https://api.energidataservice.dk")
    db = DatabaseManager()

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
                continue

            records = data.get("records") if data else None
            if not records:
                logger.warning("No records received for dataset %s", dataset.name)
                continue

            try:
                saved = db.save_market_data(records, dataset)
                logger.info("Saved %d row(s) for dataset %s", saved, dataset.name)
            except Exception:
                logger.exception("Save failed for dataset %s", dataset.name)

        # M2 rule engine (README §9): evaluated right after this cycle's data
        # is saved. A failure here shouldn't invalidate an otherwise-successful
        # ingestion cycle. This should move into a dedicated orchestrator
        # service once README §9 M4 exists — see shared/rule_engine.py.
        try:
            triggers = await run_rule_engine(db)
            logger.info("Rule engine evaluated cycle: %d trigger(s) fired", len(triggers))
        except Exception:
            logger.exception("Rule engine evaluation failed")
    finally:
        await ingestor.close()
        db.close()


async def main():
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
