import json
import logging
import os
from datetime import UTC, datetime, timedelta

from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import Json, execute_values

from shared.datasets import DatasetConfig

logger = logging.getLogger(__name__)

INSERT_QUERY = """
    INSERT INTO market_data_history
        (time, market, zone, product, value, source, is_provisional, fetched_at)
    VALUES %s
    ON CONFLICT (time, market, zone, product, fetched_at) DO NOTHING;
"""

EVENT_REPORT_INSERT_QUERY = """
    INSERT INTO event_reports
        (event_id, market, zone, product, time, report, is_correction, corrects_event_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (event_id) DO NOTHING;
"""

TRIGGER_INSERT_QUERY = """
    INSERT INTO triggers
        (trigger_type, market, zone, product, value, time, baseline, threshold, details,
         detected_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
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

    def fetch_context_window(
        self,
        market: str,
        zone: str,
        product: str,
        center_time: datetime,
        hours_before: float = 6.0,
        hours_after: float = 6.0,
    ) -> list[dict]:
        """
        Returns the "hard-data context window" (README §3C step 2) for one
        (market, zone, product) key: every history row whose `time` falls in
        `[center_time - hours_before, center_time + hours_after]`, deduped to
        the latest `fetched_at` revision per `time` (this is context for
        synthesis, not revision-alert detection, so only the most current
        known value per time unit is relevant), ordered oldest-to-newest.
        """
        window_start = center_time - timedelta(hours=hours_before)
        window_end = center_time + timedelta(hours=hours_after)

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (time) time, value, source, is_provisional, fetched_at
                    FROM market_data_history
                    WHERE market = %s AND zone = %s AND product = %s
                      AND time >= %s AND time <= %s
                    ORDER BY time ASC, fetched_at DESC;
                    """,
                    (market, zone, product, window_start, window_end),
                )
                rows = cur.fetchall()
        finally:
            self._pool.putconn(conn)
        return [
            {
                "time": r[0],
                "value": r[1],
                "source": r[2],
                "is_provisional": r[3],
                "fetched_at": r[4],
            }
            for r in rows
        ]

    def save_event_report(
        self,
        event_id: str,
        market: str,
        zone: str,
        product: str,
        time,
        report: dict,
        is_correction: bool = False,
        corrects_event_id: str | None = None,
    ) -> None:
        """
        Persists one published Event Report (README §2) to `event_reports`
        (init-db/02-event-reports.sql). Always an INSERT — corrections are
        new rows referencing `corrects_event_id`, never an UPDATE of the
        original (README §5: "never silently rewrite").
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    EVENT_REPORT_INSERT_QUERY,
                    (
                        event_id,
                        market,
                        zone,
                        product,
                        time,
                        Json(report),
                        is_correction,
                        corrects_event_id,
                    ),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Event report insertion failed for {event_id}: {e}")
            raise
        finally:
            self._pool.putconn(conn)

    def find_published_report(self, market: str, zone: str, product: str, time) -> dict | None:
        """
        Returns the most recently published `event_reports` row (as a dict)
        matching this exact (market, zone, product, time) key, or None if
        none exists yet. Used by the orchestrator's correction-event check
        (README §5) — if a revision alert fires for a key that already has a
        published report, the new report must reference this one via
        `corrects_event_id` rather than overwrite it.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_id, market, zone, product, time, published_at, report,
                           is_correction, corrects_event_id
                    FROM event_reports
                    WHERE market = %s AND zone = %s AND product = %s AND time = %s
                    ORDER BY published_at DESC
                    LIMIT 1;
                    """,
                    (market, zone, product, time),
                )
                row = cur.fetchone()
        finally:
            self._pool.putconn(conn)

        if row is None:
            return None

        report_payload = row[6]
        if isinstance(report_payload, str):
            report_payload = json.loads(report_payload)

        return {
            "event_id": row[0],
            "market": row[1],
            "zone": row[2],
            "product": row[3],
            "time": row[4],
            "published_at": row[5],
            "report": report_payload,
            "is_correction": row[7],
            "corrects_event_id": row[8],
        }

    def fetch_series_values(
        self,
        market: str,
        zone: str,
        product: str,
        limit: int = 500,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        history: bool = False,
    ) -> list[dict]:
        """
        Returns time-series data for one (market, zone, product) key for the
        Phase 5 read API/dashboard (services/api), most-recent-time-first.

        By default reads the `market_data` view (init-db/01-init.sql) — one
        row per `time`, the latest fetched revision — since a chart or table
        of "recent values" should show the current known figure, not every
        revision. Pass `history=True` to read `market_data_history` directly
        instead, returning every revision (with its own `fetched_at`) rather
        than only the latest one per `time`.
        """
        conditions = ["market = %s", "zone = %s", "product = %s"]
        params: list = [market, zone, product]
        if time_from is not None:
            conditions.append("time >= %s")
            params.append(time_from)
        if time_to is not None:
            conditions.append("time <= %s")
            params.append(time_to)
        where_clause = " AND ".join(conditions)

        if history:
            table = "market_data_history"
            select_cols = "time, value, source, is_provisional, fetched_at"
            order_clause = "time DESC, fetched_at DESC"
        else:
            table = "market_data"
            select_cols = "time, value, source, is_provisional, ingested_at"
            order_clause = "time DESC"

        query = f"""
            SELECT {select_cols}
            FROM {table}
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT %s;
        """
        params.append(limit)

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        finally:
            self._pool.putconn(conn)

        if history:
            return [
                {
                    "time": r[0],
                    "value": r[1],
                    "source": r[2],
                    "is_provisional": r[3],
                    "fetched_at": r[4],
                }
                for r in rows
            ]
        return [
            {
                "time": r[0],
                "value": r[1],
                "source": r[2],
                "is_provisional": r[3],
                "ingested_at": r[4],
            }
            for r in rows
        ]

    def fetch_event_reports(
        self,
        market: str | None = None,
        zone: str | None = None,
        product: str | None = None,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Returns published Event Reports (init-db/02-event-reports.sql) for
        the Phase 5 API `GET /event-reports`, most-recently-published-first,
        optionally filtered by market/zone/product and a [time_from, time_to]
        range on the report's own `time` (the market time unit it's about,
        not `published_at`), paginated via limit/offset.
        """
        conditions = []
        params: list = []
        if market is not None:
            conditions.append("market = %s")
            params.append(market)
        if zone is not None:
            conditions.append("zone = %s")
            params.append(zone)
        if product is not None:
            conditions.append("product = %s")
            params.append(product)
        if time_from is not None:
            conditions.append("time >= %s")
            params.append(time_from)
        if time_to is not None:
            conditions.append("time <= %s")
            params.append(time_to)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT event_id, market, zone, product, time, published_at, report,
                   is_correction, corrects_event_id
            FROM event_reports
            {where_clause}
            ORDER BY published_at DESC
            LIMIT %s OFFSET %s;
        """
        params.extend([limit, offset])

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        finally:
            self._pool.putconn(conn)

        return [self._row_to_event_report(row) for row in rows]

    def fetch_event_report(self, event_id: str) -> dict | None:
        """
        Returns a single published Event Report by its `event_id` (Phase 5
        API `GET /event-reports/{event_id}`), or None if no such report
        exists. Unlike `find_published_report` (which looks up the current
        report for a (market, zone, product, time) key), this looks up one
        exact row by its primary key — including correction rows, which
        `find_published_report` would return in preference to the report
        they correct.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_id, market, zone, product, time, published_at, report,
                           is_correction, corrects_event_id
                    FROM event_reports
                    WHERE event_id = %s;
                    """,
                    (event_id,),
                )
                row = cur.fetchone()
        finally:
            self._pool.putconn(conn)

        if row is None:
            return None
        return self._row_to_event_report(row)

    @staticmethod
    def _row_to_event_report(row: tuple) -> dict:
        report_payload = row[6]
        if isinstance(report_payload, str):
            report_payload = json.loads(report_payload)
        return {
            "event_id": row[0],
            "market": row[1],
            "zone": row[2],
            "product": row[3],
            "time": row[4],
            "published_at": row[5],
            "report": report_payload,
            "is_correction": row[7],
            "corrects_event_id": row[8],
        }

    def save_trigger(self, trigger: dict) -> None:
        """
        Persists one fired rule-engine Trigger (shared/rule_engine.py's
        `Trigger.to_dict()`) to `triggers` (init-db/03-triggers.sql), giving
        the Phase 5 API a queryable trigger history — previously every fired
        trigger existed only as an ephemeral Slack post.

        Called from `shared/rule_engine.py:run_rule_engine` alongside (not
        instead of) the existing Slack alert; that caller wraps this in its
        own try/except so a persistence failure never blocks the Slack
        alert or evaluation of the rest of the cycle's triggers.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    TRIGGER_INSERT_QUERY,
                    (
                        trigger["trigger_type"],
                        trigger["market"],
                        trigger["zone"],
                        trigger["product"],
                        trigger["value"],
                        trigger["time"],
                        trigger.get("baseline"),
                        trigger.get("threshold"),
                        trigger.get("details", ""),
                        trigger.get("detected_at"),
                    ),
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Trigger insertion failed for {trigger.get('trigger_type')}: {e}")
            raise
        finally:
            self._pool.putconn(conn)

    def fetch_triggers(
        self,
        market: str | None = None,
        zone: str | None = None,
        product: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """
        Returns persisted rule-engine triggers (Phase 5 API `GET /triggers`),
        most-recently-detected-first, optionally filtered by
        market/zone/product and paginated via limit/offset.
        """
        conditions = []
        params: list = []
        if market is not None:
            conditions.append("market = %s")
            params.append(market)
        if zone is not None:
            conditions.append("zone = %s")
            params.append(zone)
        if product is not None:
            conditions.append("product = %s")
            params.append(product)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT id, trigger_type, market, zone, product, value, time, baseline,
                   threshold, details, detected_at
            FROM triggers
            {where_clause}
            ORDER BY detected_at DESC
            LIMIT %s OFFSET %s;
        """
        params.extend([limit, offset])

        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
        finally:
            self._pool.putconn(conn)

        return [
            {
                "id": r[0],
                "trigger_type": r[1],
                "market": r[2],
                "zone": r[3],
                "product": r[4],
                "value": r[5],
                "time": r[6],
                "baseline": r[7],
                "threshold": r[8],
                "details": r[9],
                "detected_at": r[10],
            }
            for r in rows
        ]

    def close(self):
        """Releases all pooled connections. Call once per process lifecycle."""
        self._pool.closeall()
