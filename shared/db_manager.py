import psycopg2
from psycopg2.extras import execute_values
import os
import logging

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        self.conn_str = os.getenv("DATABASE_URL")
        if not self.conn_str:
            raise ValueError("DATABASE_URL environment variable is not set")

    def save_market_data(self, records, product):
        """
        Gemmer en liste af records i TimescaleDB.
        Bruger execute_values for performance ved bulk-indsætning.
        """
        query = """
            INSERT INTO market_data (time, market, zone, product, value, source, is_provisional)
            VALUES %s
            ON CONFLICT (time, market, zone, product)
            DO UPDATE SET value = EXCLUDED.value, is_provisional = EXCLUDED.is_provisional;
        """

        try:
            # Konverter records til tupler (tilpasses Energinets specifikke JSON-felter)
            values = [
                (r['HourUTC'], 'mFRR_EAM', r['PriceArea'], product, r['PriceDKK'], 'Energinet', True)
                for r in records
            ]

            conn = psycopg2.connect(self.conn_str)
            try:
                with conn.cursor() as cur:
                    execute_values(cur, query, values)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Database insertion failed: {e}")
            raise