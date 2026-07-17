-- BESS (Battery Energy Storage System) backtest simulator
-- (shared/bess_simulator.py): persisted simulation runs and their
-- tick-level results, so the API/dashboard can read a backtest's output
-- without recomputing it on every request (same "persist, don't recompute
-- on every request" rationale as init-db/03-triggers.sql).
--
-- This is a **backtest** over historical `market_data_history` data, not a
-- live/forward dispatch service — every run here is a one-shot batch job
-- triggered via the API (see services/api/main.py), not something the
-- ingestor/orchestrator write to on a schedule.
--
-- Both revenue streams a run computes (energy arbitrage and capacity
-- reservation) are estimates, not a real co-optimized dispatch — see
-- shared/bess_simulator.py's module docstring for exactly what's
-- simplified. `bess_simulation_runs.config` carries the full `BessConfig`
-- used for the run (JSONB) so a stored result is self-describing without
-- needing to cross-reference code defaults that may since have changed.

CREATE TABLE IF NOT EXISTS bess_simulation_runs (
    id BIGSERIAL PRIMARY KEY,
    zone TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    config JSONB NOT NULL,               -- the BessConfig used for this run
    total_arbitrage_revenue_dkk DOUBLE PRECISION NOT NULL,
    total_capacity_revenue_dkk DOUBLE PRECISION NOT NULL,
    total_revenue_dkk DOUBLE PRECISION NOT NULL,
    full_cycle_equivalents DOUBLE PRECISION NOT NULL,
    tick_count INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bess_simulation_runs_lookup
    ON bess_simulation_runs (zone, created_at DESC);

-- One row per simulated period (BessTick). Not a hypertable: a single
-- backtest run over a real historical window is orders of magnitude
-- smaller than `market_data_history`'s continuous polling volume, and the
-- API only ever reads a whole run's ticks (ordered by time) at once.
CREATE TABLE IF NOT EXISTS bess_simulation_ticks (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES bess_simulation_runs (id) ON DELETE CASCADE,
    time TIMESTAMPTZ NOT NULL,
    soc_mwh DOUBLE PRECISION NOT NULL,
    soc_fraction DOUBLE PRECISION NOT NULL,
    action TEXT NOT NULL,                -- 'charge' | 'discharge' | 'idle'
    day_ahead_price DOUBLE PRECISION,
    energy_discharged_mwh DOUBLE PRECISION NOT NULL,
    arbitrage_revenue_dkk DOUBLE PRECISION NOT NULL,
    capacity_reserved_mw DOUBLE PRECISION NOT NULL,
    capacity_revenue_dkk DOUBLE PRECISION NOT NULL,
    capacity_revenue_by_market JSONB NOT NULL,
    cumulative_arbitrage_revenue_dkk DOUBLE PRECISION NOT NULL,
    cumulative_capacity_revenue_dkk DOUBLE PRECISION NOT NULL,
    cumulative_total_revenue_dkk DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bess_simulation_ticks_run
    ON bess_simulation_ticks (run_id, time);
