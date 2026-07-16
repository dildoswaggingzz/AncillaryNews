import logging
import os
from datetime import UTC, datetime

from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import execute_values

from shared.datasets import DatasetConfig

logger = logging.getLogger(__name__)

INSERT_QUERY = """
    INSERT INTO market_data_history
        (time, market, zone, product, value, source, is_provisional, fetched_at)
    VALUES %s
    ON CONFLICT (time, market, zone, product, fetched_at) DO NOTHING;
"""


class DatabaseManager:
    """
    Persists ingested records to the append-only market_data_history table
    via a small, reused connection pool (see init-db/01-init.sql for the
    revision-preserving schema this writes to).
    """

    def __init__(self, minconn: int = 1, maxconn: int = 5):
        self.conn_str = os.getenv("DATABASE_URL")
        if not self.conn_str:
            raise ValueError("DATABASE_URL environment variable is not set")
        self._pool = psycopg2_pool.SimpleConnectionPool(minconn, maxconn, self.conn_str)

    def save_market_data(self, records: list[dict], dataset: DatasetConfig) -> int:
        """
        Maps a batch of raw Energinet records to market_data_history rows per
        `dataset`'s declarative field mapping (shared/datasets.py) and inserts
        them.

        Every call is a fresh INSERT tagged with a single `fetched_at` for the
        whole batch — never an UPDATE — so a later call with a changed value
        for the same (time, market, zone, product) is preserved as a new
        history row rather than overwriting the earlier figure.

        Records missing the configured time/zone field raise KeyError (fail
        fast on a malformed record before touching the database). Records
        missing an individual series' value field simply omit that product
        for that record, since not every Energinet record populates every
        product column (e.g. a capacity record may carry `UpPriceDKK` but not
        `DownPriceDKK` for a given hour).
        """
        fetched_at = datetime.now(UTC)
        values = []
        for record in records:
            time_value = record[dataset.time_field]
            zone_value = record[dataset.zone_field] if dataset.zone_field else dataset.zone
            for s in dataset.series:
                if record.get(s.value_field) is None:
                    continue
                values.append(
                    (
                        time_value,
                        s.market or dataset.market,
                        zone_value,
                        s.product,
                        record[s.value_field],
                        dataset.source,
                        dataset.is_provisional,
                        fetched_at,
                    )
                )

        if not values:
            logger.warning(
                "No mappable series found in %d records for %s", len(records), dataset.name
            )
            return 0

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                execute_values(cur, INSERT_QUERY, values)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database insertion failed for {dataset.name}: {e}")
            raise
        finally:
            self._pool.putconn(conn)

        return len(values)

    def fetch_distinct_series(self) -> list[tuple[str, str, str]]:
        """
        Returns every distinct (market, zone, product) key currently present
        in market_data_history — the set of series the rule engine (see
        shared/rule_engine.py) should evaluate on a given cycle.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT market, zone, product FROM market_data_history;")
                return cur.fetchall()
        finally:
            self._pool.putconn(conn)

    def fetch_history(self, market: str, zone: str, product: str, limit: int = 1000) -> list[dict]:
        """
        Returns raw market_data_history rows for one (market, zone, product)
        key, ordered most-recent-time-first (ties broken by most-recent-
        fetched_at-first).

        Every revision is included — nothing is deduped here — since some
        callers (revision-alert detection) need every fetched_at, while
        others (baseline/spike detection) need only the latest revision per
        time. Deduping is the caller's job (see
        shared/rule_engine.py:_dedupe_latest_per_time).
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT time, value, fetched_at
                    FROM market_data_history
                    WHERE market = %s AND zone = %s AND product = %s
                    ORDER BY time DESC, fetched_at DESC
                    LIMIT %s;
                    """,
                    (market, zone, product, limit),
                )
                rows = cur.fetchall()
        finally:
            self._pool.putconn(conn)
        return [{"time": r[0], "value": r[1], "fetched_at": r[2]} for r in rows]

    def close(self):
        """Releases all pooled connections. Call once per process lifecycle."""
        self._pool.closeall()
