import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from shared.base_ingestor import BaseIngestor
from shared.db_manager import DatabaseManager

# Konfigurer logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INGESTION_INTERVAL_MINUTES = 15


async def run_ingestion_cycle():
    """
    Henter data fra Energinet og gemmer det i TimescaleDB.
    """
    ingestor = BaseIngestor("https://api.energidataservice.dk")
    db = DatabaseManager()

    try:
        logger.info("Starter ingestion cycle...")
        # Eksempel: Hent data
        data = await ingestor.fetch_data("dataset/mfrrRequest", params={"limit": 100})

        if "records" in data and data["records"]:
            db.save_market_data(data["records"], product="up")
            logger.info(f"Succesfuldt gemt {len(data['records'])} records i databasen.")
        else:
            logger.warning("Ingen data modtaget fra API.")

    except Exception:
        logger.exception("Fejl under ingestion cycle")
        raise
    finally:
        await ingestor.close()


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
