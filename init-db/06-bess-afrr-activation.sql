-- Adds the aFRR energy activation revenue columns (shared/bess_simulator.py's
-- afrr_activation_revenue_eur / cumulative_afrr_activation_revenue_eur /
-- total_afrr_activation_revenue_eur) to the existing BESS backtest tables
-- (init-db/04-bess-simulations.sql). Reported separately in EUR -- never
-- summed into the DKK totals already on these tables -- so real scalar
-- columns (not a JSONB blob) so the total is queryable/sortable like the
-- existing DKK totals.

ALTER TABLE bess_simulation_runs
    ADD COLUMN IF NOT EXISTS total_afrr_activation_revenue_eur DOUBLE PRECISION NOT NULL DEFAULT 0.0;

ALTER TABLE bess_simulation_ticks
    ADD COLUMN IF NOT EXISTS afrr_activation_revenue_eur DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS cumulative_afrr_activation_revenue_eur DOUBLE PRECISION NOT NULL DEFAULT 0.0;
