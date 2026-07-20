-- Adds the EUR capacity-reservation revenue columns
-- (shared/bess_simulator.py's capacity_revenue_eur /
-- cumulative_capacity_revenue_eur / BacktestResult.total_capacity_revenue_eur)
-- to the existing BESS backtest tables (init-db/04-bess-simulations.sql),
-- same pattern as init-db/06-bess-afrr-activation.sql's EUR activation
-- columns.
--
-- This is the fix for the live defect described in shared/bess_simulator.py
-- (per-currency capacity buckets, see its module docstring §2): DK2's FCR
-- capacity price is EUR/MW/h while DK1's (and DK2's aFRR_capacity) is
-- DKK/MW/h (see shared/units.py / shared/datasets.py's fcr_dk2 entry), so a
-- DK2 run's capacity revenue must be reported as two separate currency
-- totals, never summed into one number. `capacity_revenue_dkk` /
-- `cumulative_capacity_revenue_dkk` / `total_capacity_revenue_dkk` on the
-- existing columns now mean DKK legs ONLY (see shared/bess_simulator.py) --
-- these new columns carry the EUR legs alongside them, real scalar columns
-- (not folded into the existing `capacity_revenue_by_market` JSONB) so the
-- EUR total is queryable/sortable like every other revenue total on these
-- tables.
--
-- Per Stage 0 (scripts/migrate.py): this file's `ALTER TABLE ... ADD COLUMN
-- IF NOT EXISTS` statements only run automatically against a brand new
-- `pgdata` volume (docker-compose.yml's docker-entrypoint-initdb.d mount) --
-- any existing deployment needs `poetry run python scripts/migrate.py` run
-- against it (see DEPLOYMENT.md).

ALTER TABLE bess_simulation_runs
    ADD COLUMN IF NOT EXISTS total_capacity_revenue_eur DOUBLE PRECISION NOT NULL DEFAULT 0.0;

ALTER TABLE bess_simulation_ticks
    ADD COLUMN IF NOT EXISTS capacity_revenue_eur DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS cumulative_capacity_revenue_eur DOUBLE PRECISION NOT NULL DEFAULT 0.0;
