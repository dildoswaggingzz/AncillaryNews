-- Morning Brief pipeline (M5, shared/morning_brief_editor.py +
-- services/orchestrator/main.py:run_morning_brief): a daily, non-technical
-- "morning news brief" combining (1) yesterday's price recap
-- (shared/price_recap_synthesizer.py), (2) cached month/quarter/year
-- forecasts (shared/forecast_synthesizer.py), and (3) illustrative BESS
-- backtest estimates (shared/bess_estimator.py) into one generic brief
-- delivered via Slack and email. Numbering follows
-- 01-init.sql...04-bess-simulations.sql's convention.

-- Cached LLM-synthesized forecasts, keyed by horizon ('month' | 'quarter' |
-- 'year'). Refresh cadence lives in shared/forecast_synthesizer.py
-- (weekly for month/quarter, monthly for year -- none regenerate daily),
-- not here; this table is just the cache + its `valid_until` staleness
-- marker.
CREATE TABLE IF NOT EXISTS forecast_cache (
    id BIGSERIAL PRIMARY KEY,
    horizon TEXT NOT NULL,                 -- 'month' | 'quarter' | 'year'
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_until TIMESTAMPTZ NOT NULL,
    forecast JSONB NOT NULL                -- {narrative, confidence, swing_factors: [...]}
);

CREATE INDEX IF NOT EXISTS idx_forecast_cache_horizon ON forecast_cache (horizon, generated_at DESC);

-- One row per calendar day's published Morning Brief. `brief_date` is
-- UNIQUE so a same-day re-run (via the API's `POST /morning-briefs/run-now`
-- or the dashboard button) is a confirmed overwrite (see
-- shared/db_manager.py:save_morning_brief's ON CONFLICT upsert), not a
-- duplicate row.
CREATE TABLE IF NOT EXISTS morning_briefs (
    id BIGSERIAL PRIMARY KEY,
    brief_date DATE NOT NULL UNIQUE,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_recap JSONB NOT NULL,
    forecast_month_id BIGINT REFERENCES forecast_cache (id),
    forecast_quarter_id BIGINT REFERENCES forecast_cache (id),
    forecast_year_id BIGINT REFERENCES forecast_cache (id),
    bess_estimates JSONB NOT NULL,         -- [{config_label, zone, run_id, revenue summary}, ...]
    brief JSONB NOT NULL,                  -- full composed MorningBrief object
    slack_sent BOOLEAN NOT NULL DEFAULT false,
    email_sent BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_morning_briefs_date ON morning_briefs (brief_date DESC);

-- Distinguishes morning-brief illustrative runs (shared/bess_estimator.py,
-- label="morning_brief") from ad-hoc /dashboard/bess/new runs, so the BESS
-- run list can be filtered/labeled without a heuristic.
ALTER TABLE bess_simulation_runs ADD COLUMN IF NOT EXISTS label TEXT;
