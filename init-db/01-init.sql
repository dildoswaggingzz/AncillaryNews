CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Revision-aware schema.
--
-- README §5 requires that revisions to previously-published figures are
-- never silently overwritten. The M0 dataset audit (docs/dataset-catalogue.md
-- §11.2) found that no Energinet Energi Data Service dataset currently
-- exposes an explicit PublishedTime/RevisedTime field, so we cannot key on a
-- true "when Energinet published this figure" dimension yet. As the best
-- available proxy, we use `fetched_at` — the ingestor's own poll timestamp —
-- as the revision-tracking dimension: every fetch is recorded as its own
-- row, so a later fetch that returns a different value for the same
-- (time, market, zone, product) shows up as an additional history row
-- instead of overwriting the earlier one. This is a foundation for M2's
-- "revision alert" trigger and M4's correction-event handling, not the full
-- implementation of either.

CREATE TABLE IF NOT EXISTS market_data_history (
    time TIMESTAMPTZ NOT NULL,           -- market time unit (TimeUTC, HourUTC, Minutes1UTC, ...)
    market TEXT NOT NULL,                -- e.g. 'mFRR_capacity', 'aFRR_energy', 'imbalance'
    zone TEXT NOT NULL,                  -- e.g. 'DK1', 'DK2', or 'ALL' for system-wide series
    product TEXT NOT NULL,               -- e.g. 'up', 'down', 'imbalance_price'
    value DOUBLE PRECISION,
    source TEXT NOT NULL,                -- 'Energinet', 'ENTSO-E', 'NBM'
    is_provisional BOOLEAN DEFAULT true,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- proxy for published_at; see comment above
    PRIMARY KEY (time, market, zone, product, fetched_at)
);

SELECT create_hypertable('market_data_history', 'time', if_not_exists => TRUE);

ALTER TABLE market_data_history SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'market,zone,product'
);

-- Latest-value-per-key view for fast current-state queries. Derived, not
-- stored: always reflects the most recently fetched row for each
-- (time, market, zone, product), without ever mutating market_data_history.
CREATE OR REPLACE VIEW market_data AS
SELECT DISTINCT ON (time, market, zone, product)
    time,
    market,
    zone,
    product,
    value,
    source,
    is_provisional,
    fetched_at AS ingested_at
FROM market_data_history
ORDER BY time, market, zone, product, fetched_at DESC;
