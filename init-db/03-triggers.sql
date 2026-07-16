-- Phase 5 read API: persisted rule-engine triggers (README §4,
-- shared/rule_engine.py). Every fired `Trigger` was previously posted only
-- to Slack -- an ephemeral, unqueryable record. This table gives the Phase 5
-- API's `GET /triggers` a durable, queryable trigger history without
-- changing what `run_rule_engine` already does with each trigger (it still
-- posts to Slack exactly as before; this is an addition, not a replacement).
--
-- `time` is stored as TEXT, matching `Trigger.time` (already a `str(...)` of
-- whatever psycopg2 returned for the underlying TIMESTAMPTZ column -- see
-- shared/rule_engine.py's `Trigger` dataclass) rather than re-parsed back
-- into a TIMESTAMPTZ here, to avoid a second, potentially lossy conversion.
--
-- Not a hypertable: trigger volume is orders of magnitude lower than
-- `market_data_history` (one row per fired trigger, not per poll).

CREATE TABLE IF NOT EXISTS triggers (
    id BIGSERIAL PRIMARY KEY,
    trigger_type TEXT NOT NULL,
    market TEXT NOT NULL,
    zone TEXT NOT NULL,
    product TEXT NOT NULL,
    value DOUBLE PRECISION,
    time TEXT NOT NULL,
    baseline DOUBLE PRECISION,
    threshold DOUBLE PRECISION,
    details TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_triggers_lookup
    ON triggers (market, zone, product, detected_at DESC);
