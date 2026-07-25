-- Adds the per-run data-coverage counts (shared/bess_simulator.py's
-- BacktestResult.uncovered_periods_by_leg / .activation_uncovered_periods /
-- .zero_price_periods_by_leg) to the existing BESS backtest run table
-- (init-db/04-bess-simulations.sql), same pattern as
-- init-db/07-bess-capacity-currency.sql.
--
-- These three counts were computed on every run but existed only on the
-- freshly-returned result object (services/api/main.py's response), never on
-- a re-fetch, which is the wrong way round: a revenue total is only
-- interpretable next to how much of the window actually had data behind it.
-- "This run earned little", "this run's market genuinely cleared at 0", and
-- "this run had no prices to earn against" are three different findings, and
-- without these columns a stored run collapses all three into one low
-- number. Runs 77 and 82 are the worked example -- two June backtests
-- 61% apart on all-in revenue, entirely because of how much activation-price
-- data each happened to see, which nothing in their stored rows recorded.
--
-- Nullable rather than `NOT NULL DEFAULT 0`, deliberately: every run
-- persisted before this migration has *unknown* coverage, not zero
-- uncovered periods. A 0 default would assert full coverage for exactly the
-- historical runs whose coverage is in question. NULL reads as "not
-- recorded" and keeps them distinguishable from a genuinely fully-covered
-- run going forward.
--
-- Per Stage 0 (scripts/migrate.py): this file's `ALTER TABLE ... ADD COLUMN
-- IF NOT EXISTS` statements only run automatically against a brand new
-- `pgdata` volume (docker-compose.yml's docker-entrypoint-initdb.d mount) --
-- any existing deployment needs `poetry run python scripts/migrate.py` run
-- against it (see DEPLOYMENT.md).

ALTER TABLE bess_simulation_runs
    ADD COLUMN IF NOT EXISTS uncovered_periods_by_leg JSONB,
    ADD COLUMN IF NOT EXISTS activation_uncovered_periods INTEGER,
    ADD COLUMN IF NOT EXISTS zero_price_periods_by_leg JSONB;
