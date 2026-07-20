# Deployment notes

Production-readiness pass (Phase 6, post-M4): structured logging, Prometheus
metrics, optional API-key auth, and this document. Written for whoever
deploys this beyond a laptop `docker-compose up --build`.

## Env vars that must be overridden before any non-local deployment

| Var | Default (local dev) | Why it must change |
|---|---|---|
| `DB_PASSWORD` | `secret` (docker-compose.yml fallback) | Hardcoded, well-known default. **Set this before deploying anywhere reachable outside your own machine.** |
| `GRAFANA_ADMIN_PASSWORD` | `admin` (docker-compose.yml fallback) | Same story -- Grafana's own well-known default admin password. |
| `API_KEY` | unset (API stays fully open) | Not required to *run* the stack, but if the API/dashboard (host port `8080`, container port `8000` -- see `docker-compose.yml`) is reachable by anyone other than you, set this. See "Auth posture" below. |

Everything else below is genuinely optional (the code already degrades
gracefully if unset):

| Var | Used by | Effect if unset |
|---|---|---|
| `ANTHROPIC_API_KEY` | crawler (`shared/claim_extractor.py`), orchestrator (`shared/event_synthesizer.py`) | Crawler stores raw article text without derived claims; orchestrator skips LLM synthesis (the raw trigger still reaches Slack via `shared/rule_engine.py`). Neither crashes. |
| `SLACK_WEBHOOK_URL` | rule engine + orchestrator (`shared/slack_notifier.py`) | Logs a warning and skips the Slack post instead of crashing. |
| `METRICS_PORT` | ingestor (default `9100`), crawler (`9101`), orchestrator (`9102`) | Falls back to the documented default; the API always serves `/metrics` on its main port (`8000`). |
| `AUTO_RUN_ENABLED` | crawler, orchestrator | Falls back to `false` -- their *automatic* scheduled cycles don't fire (no Claude API calls happen on a schedule). See "Cost control: AUTO_RUN_ENABLED" below. |

## Required deploy step: `scripts/migrate.py` (schema migrations)

`docker-compose.yml` mounts `./init-db` at `/docker-entrypoint-initdb.d`,
which Postgres only executes automatically against a **brand-new, empty**
data directory. On any deployment whose `pgdata` volume already existed
before a new `init-db/*.sql` file was added -- e.g. pulling a release that
adds a new `ALTER TABLE ... ADD COLUMN` migration -- that file never applies
itself. This is not hypothetical: it is exactly how
`init-db/06-bess-afrr-activation.sql`'s columns went unapplied on every
pre-existing deployment until this was found and fixed.

**Run this after every `docker-compose up`/deploy, on every environment,
including a fresh one** (idempotent -- a no-op there, since Postgres already
applied every file itself):

```bash
DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \
    poetry run python scripts/migrate.py
```

Applies every `init-db/*.sql` file, in filename order, against
`DATABASE_URL`. Every statement in `init-db/*.sql` is idempotent
(`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `CREATE OR
REPLACE VIEW`, `create_hypertable(..., if_not_exists => TRUE)`, `ALTER TABLE
... ADD COLUMN IF NOT EXISTS`) -- verified by running the full sequence
twice in a row against a live TimescaleDB container while building this
script (see `scripts/migrate.py`'s module docstring for the file-by-file
breakdown), so re-running it is always safe.

**Deliberately not auto-applied at service startup.** Every service instead
calls `DatabaseManager.check_expected_columns()` once at boot and only *logs
a warning* (`Database schema is missing N expected column(s): ...`) if
columns from `init-db/*.sql`'s `ALTER TABLE ... ADD COLUMN` files are
missing -- it never mutates schema itself. Auto-applying schema changes from
inside an application process, unattended, against a database a whole fleet
of services shares, is exactly the kind of surprise a production deployment
shouldn't have to reason about; running `scripts/migrate.py` is meant to be
an explicit, on-purpose step (a human, or a deploy pipeline stage), not an
implicit side effect of a service starting.

## Cost control: `AUTO_RUN_ENABLED`

Two of this stack's four services call the Anthropic API on every scheduled
cycle, and both cycles run unattended, indefinitely, by default:

- **`orchestrator`** (`services/orchestrator/main.py`, 15-minute interval):
  evaluates the rule engine (`shared/rule_engine.py`), then runs the full
  RAG + **Claude Opus** synthesis pipeline (`shared/event_synthesizer.py`,
  the most expensive model this repo calls) for *every* trigger that fires
  that cycle. In observed live runs this fired 2-7 triggers per 15-minute
  cycle -- i.e. up to 7 Opus calls every 15 minutes, unattended.
- **`crawler`** (`services/crawler/main.py`, 30-minute interval): polls every
  RSS feed (`shared/rss_feeds.py`) and runs **Claude Haiku** claim extraction
  (`shared/claim_extractor.py`) on every new article found. Haiku is cheaper
  per-call than Opus, but still scales with feed volume and cycle count.
- **`ingestor`** (`services/ingestor/main.py`) makes **zero** LLM calls --
  pure Energinet HTTP polling + Postgres writes -- and has no such gate; it's
  the free, always-on data-collection layer and is meant to keep running
  unconditionally regardless of `AUTO_RUN_ENABLED`.

`AUTO_RUN_ENABLED` (`docker-compose.yml`, defaults to `false` if unset --
see `.env.example`) makes `crawler`'s and `orchestrator`'s automatic
scheduled cycles **opt-in, not opt-out**:

- **Unset / `false` (the default)**: both services still start, still run
  their APScheduler instance, and still stay healthy for their Docker
  healthcheck and `/metrics` endpoint -- but the scheduled job
  (`scheduled_crawl_cycle` / `scheduled_synthesis_cycle`) no-ops with a
  clear `AUTO_RUN_ENABLED is unset/false; skipping...` log line each time it
  would otherwise have fired, instead of calling `run_crawl_cycle` /
  `run_synthesis_cycle` (and therefore Anthropic) at all.
- **`true`**: both services behave exactly as before this gate existed --
  fully automatic scheduled cycles, no manual intervention needed.

Regardless of `AUTO_RUN_ENABLED`, you can always fire **one real cycle on
demand**:

- `POST /orchestrator/run-now` and `POST /crawler/run-now` on the `api`
  service -- gated behind `API_KEY` like the other mutating JSON routes
  (`/manual-articles`, `/bess/backtest`). Each returns a small JSON summary
  of what that one cycle did (`{"triggers_fired": ..., "reports_published":
  ...}` / `{"articles_processed": ..., "claims_extracted": ...}`).
- The matching "Run orchestrator now" / "Run crawler now" buttons on the
  dashboard homepage (`/`), which POST to the routes above and render the
  same summary inline -- same synchronous trigger-and-show-result pattern as
  `/dashboard/bess/new`. These dashboard buttons stay open regardless of
  `API_KEY`, same exception as every other dashboard HTML route (see "Auth
  posture" below) -- so put the dashboard behind a reverse proxy/VPN if you
  don't want anyone who can reach it able to spend Anthropic credit on
  demand.

**Implementation note**: the `api` service has no message queue or RPC layer
to reach into the separate `orchestrator`/`crawler` containers, so
`services/api/main.py` loads `services/orchestrator/main.py` and
`services/crawler/main.py` directly (by file path, via `importlib` -- this
repo has no `__init__.py`/package-mode) and calls their exact
`run_synthesis_cycle` / `run_crawl_cycle` functions in-process, against the
same `DATABASE_URL`/`QDRANT_URL`/`ANTHROPIC_API_KEY`/`SLACK_WEBHOOK_URL` the
real services use (`docker-compose.yml`'s `api` environment; its Dockerfile
now also `COPY`s those two `main.py` files in for this reason). This is the
pragmatic choice for this codebase's existing architecture (everything talks
to Postgres/Qdrant directly, no RPC), not a general-purpose pattern -- it
does mean an on-demand run happens synchronously inside the API request
(fine for the cycle volumes this repo sees; would need to move to a
background task/queue if cycles ever grew large enough to risk request
timeouts).

## Historical backfill for the BESS backtest simulator

`services/ingestor/main.py`'s scheduled cycle only ever fetches the most
recent handful of records per dataset (`shared/datasets.py`'s
`limit`/`sort`-based `params`), so `market_data_history` only has real depth
back to whenever the stack was first brought up. The BESS backtest simulator
(`shared/bess_simulator.py`, `POST /bess/backtest`) needs weeks of price
history to build a meaningful rolling baseline and see varied market
conditions, so `shared/backfill.py` adds a separate, occasional/manual
mechanism that pages through Energinet's `start`/`end` date-range query
params (confirmed live against `api.energidataservice.dk`; not documented in
`docs/dataset-catalogue.md`) instead of the live poller's "most recent N
records" pattern. Unlike `ingestor`, this makes **zero** Anthropic API
calls -- pure Energinet HTTP polling + Postgres writes -- so there's no
LLM-spend concern to gate here, and it's always available.

Two ways to run it:

- **Standalone script** (`scripts/backfill_history.py`) -- for a one-off
  local backfill, e.g. against docker-compose's mapped Postgres port:

  ```bash
  DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \
      poetry run python scripts/backfill_history.py --days 30
  ```

  `--start`/`--end` (ISO 8601) for an explicit window instead of `--days`,
  `--datasets fcr_dk1,day_ahead_prices` to restrict to a subset, `--chunk-days`
  to change the per-request date-range chunk size. Not part of any
  `docker-compose.yml` service or scheduled job -- run it manually whenever a
  wider window is needed.

- **`POST /ingestor/backfill`** on the `api` service -- same on-demand
  pattern as `/orchestrator/run-now`/`/crawler/run-now` above (runs
  `shared.backfill.run_backfill` in-process against the `api` service's own
  pooled `DatabaseManager`), gated behind `API_KEY` like the other mutating
  routes. Body: `{"start_time": ..., "end_time": ..., "datasets": [...],
  "chunk_days": ...}`, all optional (defaults to the trailing 30 days ending
  now, every BESS-relevant dataset). The dashboard homepage (`/`) has a
  matching "Run backfill now" form (days + optional comma-separated dataset
  list) that stays open regardless of `API_KEY`, same exception as every
  other dashboard HTML route.

Both backfill `shared.backfill.BESS_DATASET_NAMES` (`fcr_dk1`, `fcr_dk2`,
`afrr_reserves_nordic`, `afrr_energy_activation`, `day_ahead_prices`,
`imbalance_price`, `ffr_dk2`, `ffr_demand_dk2`, `inertia_nordic`) -- the
datasets `shared/bess_simulator.py` reads today, plus `ffr_dk2`/
`ffr_demand_dk2`/`inertia_nordic`, included ahead of a near-term BESS-
stacking change so historical depth is already available the day that
wiring lands (see `shared/backfill.py`'s comment on the constant) -- never
`mfrr_capacity`/`mfrr_eam`/`mfrr_capacity_extra` (excluded by the battery
market-participation constraint, same as the simulator itself).

**Idempotent / safe to re-run**: every chunk's records are saved via the
exact same `DatabaseManager.save_market_data` the live ingestor uses, tagged
with a fresh `fetched_at`, `ON CONFLICT (time, market, zone, product,
fetched_at) DO NOTHING`. Re-running a backfill over an overlapping or
identical window does **not** dedupe against the live ingestor's earlier
fetches of the same `time`/`market`/`zone`/`product` -- it adds new rows with
their own `fetched_at`, exactly like any other independent fetch of
already-known data. This is consistent with (not a bug in)
`market_data_history`'s deliberately append-only, revision-preserving
design; nothing here mutates or dedupes existing rows, and it never
disturbs/duplicates the live ingestor's own data in a destructive way.

**Real Energinet data depth (observed 2026-07-17, will drift over time)**:
retention varies a lot per dataset and isn't documented anywhere --
discovered empirically by querying the live API. At the time this was built:
`day_ahead_prices`/`imbalance_price`/`afrr_reserves_nordic`/`fcr_dk1`/`fcr_dk2`
each had multiple months to 4+ years of real history available; `afrr_energy_activation`
(sub-second `TimeMsUTC` resolution) only had a few months. A 30-day default
window comfortably fits every one of them.

**Rate limiting**: `docs/dataset-catalogue.md` documents ~1 request/second
observed during the original M0 bulk discovery; a live backfill run while
building this found the real limit noticeably stricter in short bursts
(repeated HTTP 429s), and the `FcrDK1`/`FcrNdDK2` endpoints specifically
appeared to tolerate even less request volume than the others in practice.
`BaseIngestor.fetch_data` already retries `429`s with exponential backoff (5
attempts, 2-10s), so an occasional 429 self-heals and a failed chunk is
logged and skipped (not fatal to the rest of that dataset's backfill or any
other dataset's) rather than aborting the whole run -- but a wide backfill
over several datasets can still end up with some chunks (particularly for
the FCR datasets) failing to fetch within the retry budget. Re-run the
script/route for just the affected dataset(s) (`--datasets fcr_dk1`) to fill
in any gaps -- safe per the idempotency note above.

## What each service needs to run

| Service | Requires | Optional |
|---|---|---|
| `ingestor` | `DATABASE_URL` (set by compose from `DB_PASSWORD`), reachable `db` | `METRICS_PORT` |
| `crawler` | reachable `vector-db` (Qdrant) | `ANTHROPIC_API_KEY`, `METRICS_PORT`, `AUTO_RUN_ENABLED` |
| `orchestrator` | `DATABASE_URL`, reachable `db` and `vector-db` | `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`, `METRICS_PORT`, `AUTO_RUN_ENABLED` |
| `api` | `DATABASE_URL`, reachable `db`; reachable `vector-db` and `ANTHROPIC_API_KEY` for `/manual-articles` and the on-demand `/crawler/run-now`/`/orchestrator/run-now` routes | `API_KEY`, `SLACK_WEBHOOK_URL` (for `/orchestrator/run-now`'s Slack post) |
| `prometheus` | reachable `ingestor:9100`, `crawler:9101`, `orchestrator:9102`, `api:8000/metrics` | -- |
| `grafana` | reachable `prometheus:9090` | `GRAFANA_ADMIN_PASSWORD` |

## Healthcheck / dependency graph

```
db (TimescaleDB, healthchecked via pg_isready)
vector-db (Qdrant, healthchecked via /healthz)

ingestor        depends_on: db (service_healthy)
crawler         depends_on: vector-db (service_healthy)
orchestrator    depends_on: db, vector-db (both service_healthy)
api             depends_on: db (service_healthy)

prometheus      depends_on: ingestor, crawler, orchestrator, api (started)
grafana         depends_on: prometheus (started)
```

`db` and `vector-db` are the only two services with real container
healthchecks (`docker-compose.yml`); everything downstream of them waits on
`condition: service_healthy` before starting, so a cold `docker compose up`
never races ingestion/synthesis against a database that isn't accepting
connections yet. `prometheus`/`grafana` only wait on the other services
having *started* (not on their own internal readiness), since scraping is
naturally tolerant of a target being briefly unreachable on the first tick.

`api`'s own `/health` endpoint deliberately never touches the database, so
it stays green (for container/orchestrator healthchecks built on top of it)
even if `db` is briefly unreachable after startup.

## Auth posture

- **API key**: optional, via the `API_KEY` env var (`services/api/main.py`).
  If unset, the API/dashboard is exactly as open as it always was --
  suitable for local dev only. If set, every JSON API route
  (`/series*`, `/event-reports*`, `/triggers`) requires the `X-API-Key`
  header to match; a missing or wrong key gets a `401`. The
  server-rendered dashboard HTML pages (`/`, `/dashboard/*`) stay open
  regardless of `API_KEY` -- a deliberate choice, since they're meant for a
  human clicking around in a browser (which can't easily attach a custom
  header on a normal navigation) rather than a programmatic API client. If
  you need the dashboard itself gated, put it behind a reverse proxy with
  its own auth (e.g. an nginx/Caddy basic-auth layer, or a VPN) rather than
  extending this app's own auth model. `/health` and `/metrics` are always
  unauthenticated, since container healthchecks and Prometheus scraping
  need to reach them without credentials.
- **Slack / Anthropic keys**: both optional, both degrade gracefully if
  unset (see the table above) -- there is no functionality that silently
  breaks or crashes if either is missing, only reduced functionality
  (no Slack alerts; no LLM-derived claims/synthesis).
- **Database**: a single shared Postgres user/password
  (`postgres` / `DB_PASSWORD`) across all four services -- no per-service
  credentials or row-level security. Fine for this deployment's threat
  model (a small internal tool with no multi-tenant data), but worth
  knowing if this ever needs to be exposed more broadly.

In short: nothing in this stack currently enforces network-level isolation
between services -- `API_KEY` is the one credential-based gate on the one
service (the API) meant to be reachable from outside the docker-compose
network. Everything else assumes it's running on a trusted internal
network; if that assumption stops holding, put the whole stack behind a
VPN or equivalent rather than relying on `API_KEY` alone.

## Observability

- **Logs**: every service emits one JSON line per log record on stdout
  (`shared/logging_config.py`), with at least `timestamp`, `level`,
  `logger`, `message`, plus whatever structured `extra={...}` fields a
  given call site supplies. Feed stdout to whatever log aggregation a given
  deployment already uses (Loki, CloudWatch Logs, etc.) -- no code changes
  needed on that front.
- **Metrics**: every service exposes a Prometheus-text-format `/metrics`
  endpoint -- `ingestor:9100`, `crawler:9101`, `orchestrator:9102` via a
  standalone `prometheus_client.start_http_server`, and `api:8000/metrics`
  directly from the existing FastAPI app. `prometheus/prometheus.yml`
  scrapes all four; `grafana/provisioning/` auto-provisions the Prometheus
  datasource and a starter dashboard
  (`grafana/dashboards/ancillarynews.json`) covering ingestion success
  rate, trigger-fired rate by type, LLM synthesis latency, citation
  rejections, crawl cycle duration, and API request rate. Grafana at
  `http://localhost:3000` (anonymous viewer access enabled for local dev;
  admin login is `admin` / `GRAFANA_ADMIN_PASSWORD`).
