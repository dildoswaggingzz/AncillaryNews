# CLAUDE.md

Practical guidance for working in this repo. For mission/scope and market background see `README.md`; for deep design of a subsystem see the relevant `docs/*-design.md`.

## What this is

AncillaryNews ("EnergySignals Agent") monitors the Danish ancillary-services (balancing) markets, detects abnormal price movements, and explains why they happened by correlating hard market data (Energinet / Energi Data Service, ENTSO-E) with soft data (news, TSO announcements). It also runs BESS (battery) revenue/co-optimization backtests for the Morning Brief.

Python 3.12 monorepo, Poetry-managed, run as a docker-compose stack.

## Commands

```bash
poetry install                       # set up the environment

poetry run pytest                    # offline test suite (hermetic; the default)
poetry run pytest -m live            # tests that make real HTTP calls to api.energidataservice.dk
poetry run pytest tests/test_bess_economics.py -q   # a single test file

poetry run ruff check --fix .        # lint (autofix)
poetry run ruff format .             # format
```

Ruff is pinned to **v0.8.6** in `.pre-commit-config.yaml`, kept in lockstep with CI's `ruff format --check .` — do not bump one without the other, or formatting diverges and unformatted files slip onto `main`.

### Running the stack / analysis scripts

```bash
docker-compose up --build            # full stack: db, vector-db, ingestor, crawler, orchestrator, api, prometheus, grafana
```

Postgres/TimescaleDB is exposed on **localhost:5433** (container `ancillarynews-db-1`, db `energy`). One-off analysis scripts under `scripts/` read the DB directly and need the env set:

```bash
DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy PYTHONPATH=. \
  poetry run python scripts/<name>.py
```

These scripts are **read-only** (they call `run_backtest`, never `db.save_bess_run`).

## Layout

- `services/` — the four long-running services, each with its own `Dockerfile`:
  - `ingestor` — pulls hard market data into TimescaleDB (`shared/base_ingestor.py`, `shared/datasets.py` registry).
  - `crawler` — soft data (RSS/news), extraction, Qdrant embedding.
  - `orchestrator` — APScheduler jobs: rule engine, Morning Brief, synthesis.
  - `api` — FastAPI dashboard + endpoints (`services/api/main.py`).
- `shared/` — the library every service imports. Not a service. Key modules:
  - Data/infra: `db_manager.py`, `datasets.py`, `backfill.py`, `baselines.py`, `units.py`, `logging_config.py`, `metrics.py`.
  - Forecasting: `forecast_model.py` (LightGBM), `feature_store.py`.
  - **BESS stack** (see below).
- `scripts/` — one-off reports/analysis and `migrate.py`, `backfill_history.py`, `validate_datasets.py`.
- `init-db/` — numbered SQL schema files applied in order on DB init (`01-init.sql` … `09-bess-coverage-counts.sql`). New schema = a new numbered file, never an edit to an applied one.
- `tests/` — pytest suite (offline by default).
- `docs/` — design (`*-design.md`) and results (`*-results.md`) per subsystem.
- `grafana/`, `prometheus/` — monitoring config.

## BESS co-optimizer (most-touched subsystem)

- `shared/bess_simulator.py` — `run_backtest(db, zone, start, end, config)`, `BessConfig`, `BacktestResult`/`BessTick`. Two strategies via `config.strategy`: `"threshold"` (causal heuristic) and `"cooptimized"` (delegates to the LP below). `foresight`: `"perfect"` (oracle) vs `"forecast"` (deployable lag-24h policy).
- `shared/bess_dispatch_milp.py` — the single-joint-LP perfect/forecast co-optimizer. **Revenue only.**
- `shared/bess_estimator.py` — Morning Brief configs: `ILLUSTRATIVE_CONFIGS`, `_with_zone_capacity_markets`, `_with_cycle_cap`, `ZONE_CAPACITY_MARKETS` (DK1/DK2 stacks).
- `shared/economic_eval.py` — read-only allocation layer over `run_backtest` (forecast-vs-trailing value).
- `shared/bess_economics.py` — retailer cost/P&L layer turning revenue into net profit (tariffs, energy tax, service fee, spot cut).

**Invariant — never sum DKK and EUR.** Capacity legs are billed in their registry-native currency (`shared/units.py`, `currency_for`); DKK and EUR totals are reported in **separate buckets**, only combined at the fixed `DKK_PER_EUR` peg in explicit presentation-layer properties (`total_revenue_all_dkk/_eur`). Mixing them anywhere else is a bug the currency separation exists to prevent.

## Conventions & gotchas

- **Data is bitemporal/revisable.** Market data can be provisional and later revised; coverage gaps are explicit (backtests no longer silently price periods with no data — see `init-db/09-bess-coverage-counts.sql`). Don't assume a window is fully covered.
- **Dataclasses are frozen** where they're config/result carriers; build copies with `dataclasses.replace`, don't mutate.
- **Git workflow:** cut a fresh branch off `main` per unit of work (docs included); changes land on `main` via PR. Commit/push only when asked.
- **Live tests** hit the real Energi Data Service API and are excluded from the default run; gate any new networked test behind the `live` marker.
