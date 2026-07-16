-- M4 Intelligence Orchestrator: persisted Event Reports (README §2, §5).
--
-- Every published Event Report (the exact JSON shape from README §2) is
-- stored here as `report` JSONB, keyed by its own `event_id`. This table is
-- the durable record the orchestrator consults before publishing a new
-- report for a given (market, zone, product, time) — if a later revision
-- alert fires for a key that already has a row here, the orchestrator emits
-- a *new* row with `is_correction = true` and `corrects_event_id` pointing
-- back at the report it corrects (README §5: "never silently rewrite").
-- The original row is never updated or deleted.
--
-- Not a hypertable: report volume is orders of magnitude lower than
-- `market_data_history` (one row per published Event Report, not per poll),
-- and the Phase 5 API/dashboard needs simple keyed lookups more than
-- time-bucketed compression.

CREATE TABLE IF NOT EXISTS event_reports (
    event_id TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    zone TEXT NOT NULL,
    product TEXT NOT NULL,
    time TIMESTAMPTZ NOT NULL,              -- the market time unit the report is about
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    report JSONB NOT NULL,                  -- the full README §2 Event Report JSON
    is_correction BOOLEAN NOT NULL DEFAULT false,
    corrects_event_id TEXT REFERENCES event_reports (event_id)
);

-- Fast lookup of "is there already a published report for this
-- (market, zone, product, time)?" — the correction-event check (README §5).
CREATE INDEX IF NOT EXISTS idx_event_reports_lookup
    ON event_reports (market, zone, product, time);
