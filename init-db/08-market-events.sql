-- M6+: Supply-event features (docs/supply-event-features-design.md §2).
--
-- Structured, typed events extracted from crawled news by
-- shared/event_extractor.py and stored by services/crawler/main.py,
-- additively alongside the existing Qdrant claims store. **Postgres, not
-- Qdrant** (design §2): these events need structured numeric/date queries
-- for the feature-store join (shared/feature_store.py), not semantic
-- retrieval -- Qdrant remains the home for free-text claims (RAG); this is
-- its structured complement.
--
-- `known_at` (design §1) is the leak-safe availability key: the article's
-- `published` timestamp, falling back to crawl time, assigned by the
-- CRAWLER -- never the model (shared/event_extractor.py must not be trusted
-- to date events). `effective_from` is a VALUE inside the feature (when the
-- announced capacity change lands), never the availability key -- a join
-- keyed on it instead of `known_at` would leak the future into the past.
--
-- `event_id` is deterministic (`uuid5(NAMESPACE_URL, f"{url}#event:{i}")`,
-- mirroring shared/vector_store.py's `_claim_point_id`), so re-crawling the
-- same article upserts (no-ops on conflict via `ON CONFLICT (event_id) DO
-- NOTHING`, shared/db_manager.py:save_market_event) rather than duplicating.
--
-- Not a hypertable: expected volume is orders of magnitude lower than
-- market_data_history (one row per extracted event, not per poll) -- see
-- design §0 on the honest, currently-tiny history depth.

CREATE TABLE IF NOT EXISTS market_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,      -- prequalification | capacity_commissioning |
                                    -- capacity_retirement | demand_volume_change |
                                    -- outage | regime_change | other
    market TEXT,                   -- FCR | aFRR | mFRR | FFR | day_ahead | null
    zone TEXT,                     -- DK1 | DK2 | SE4 | ... | null
    direction TEXT,                -- up | down | null (FCR-D is directional)
    magnitude_mw DOUBLE PRECISION, -- null when not stated -- never invented
    effective_from DATE,           -- when the change takes effect; a VALUE, not
                                    -- the availability key (design §1)
    known_at TIMESTAMPTZ NOT NULL, -- the leak-safe availability key (design §1)
    confidence DOUBLE PRECISION NOT NULL,  -- 0-1; Tier-2 sources capped, see
                                            -- shared/event_extractor.py
    source_url TEXT NOT NULL,
    source_title TEXT,
    source_tier TEXT,
    raw_excerpt TEXT NOT NULL,     -- the sentence the event came from -- every
                                    -- event must be traceable to source text
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now()  -- crawl time (audit only,
                                                       -- distinct from known_at)
);

CREATE INDEX IF NOT EXISTS idx_market_events_known_at ON market_events (known_at);
CREATE INDEX IF NOT EXISTS idx_market_events_market_zone ON market_events (market, zone);
CREATE INDEX IF NOT EXISTS idx_market_events_effective_from ON market_events (effective_from);
