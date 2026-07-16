# Deployment notes

Production-readiness pass (Phase 6, post-M4): structured logging, Prometheus
metrics, optional API-key auth, and this document. Written for whoever
deploys this beyond a laptop `docker-compose up --build`.

## Env vars that must be overridden before any non-local deployment

| Var | Default (local dev) | Why it must change |
|---|---|---|
| `DB_PASSWORD` | `secret` (docker-compose.yml fallback) | Hardcoded, well-known default. **Set this before deploying anywhere reachable outside your own machine.** |
| `GRAFANA_ADMIN_PASSWORD` | `admin` (docker-compose.yml fallback) | Same story -- Grafana's own well-known default admin password. |
| `API_KEY` | unset (API stays fully open) | Not required to *run* the stack, but if the API/dashboard (port 8000) is reachable by anyone other than you, set this. See "Auth posture" below. |

Everything else below is genuinely optional (the code already degrades
gracefully if unset):

| Var | Used by | Effect if unset |
|---|---|---|
| `ANTHROPIC_API_KEY` | crawler (`shared/claim_extractor.py`), orchestrator (`shared/event_synthesizer.py`) | Crawler stores raw article text without derived claims; orchestrator skips LLM synthesis (the raw trigger still reaches Slack via `shared/rule_engine.py`). Neither crashes. |
| `SLACK_WEBHOOK_URL` | rule engine + orchestrator (`shared/slack_notifier.py`) | Logs a warning and skips the Slack post instead of crashing. |
| `METRICS_PORT` | ingestor (default `9100`), crawler (`9101`), orchestrator (`9102`) | Falls back to the documented default; the API always serves `/metrics` on its main port (`8000`). |

## What each service needs to run

| Service | Requires | Optional |
|---|---|---|
| `ingestor` | `DATABASE_URL` (set by compose from `DB_PASSWORD`), reachable `db` | `METRICS_PORT` |
| `crawler` | reachable `vector-db` (Qdrant) | `ANTHROPIC_API_KEY`, `METRICS_PORT` |
| `orchestrator` | `DATABASE_URL`, reachable `db` and `vector-db` | `ANTHROPIC_API_KEY`, `SLACK_WEBHOOK_URL`, `METRICS_PORT` |
| `api` | `DATABASE_URL`, reachable `db` | `API_KEY` |
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
