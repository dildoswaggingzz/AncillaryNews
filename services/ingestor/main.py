import asyncio
import logging
from shared.base_ingestor import BaseIngestor
from shared.db_manager import DatabaseManager

# Konfigurer logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
            db.save_market_data(data["records"])
            logger.info(f"Succesfuldt gemt {len(data['records'])} records i databasen.")
        else:
            logger.warning("Ingen data modtaget fra API.")
            
    except Exception as e:
        logger.error(f"Fejl under ingestion: {e}")
    finally:
        await ingestor.close()

if __name__ == "__main__":
    asyncio.run(run_ingestion_cycle())