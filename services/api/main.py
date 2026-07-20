"""
Phase 5 read API + dashboard (follow-on to README §9 M4, per
init-db/02-event-reports.sql's "Phase 5 API/dashboard" comment): a FastAPI
service serving the Event Reports, market data, and rule-engine triggers
built up by M0-M4 as both JSON endpoints and simple server-rendered HTML
pages (README §7: "dashboard later" -- this is that later).

Read-only for Postgres: this service never writes to `market_data_history`,
`event_reports`, or `triggers` -- those tables remain owned by the
ingestor/orchestrator (see shared/db_manager.py, shared/rule_engine.py). It
does, however, *write* to Qdrant via `/manual-articles` (and its dashboard
form counterpart) -- a paste-in entry point that runs manually-submitted
LinkedIn posts through the exact same claim-extraction/storage pipeline
services/crawler/main.py uses for RSS articles (see that route's docstring
below for the full rationale).

Auth (Phase 6 production readiness): optional API-key gating via the
`API_KEY` env var -- see `require_api_key` below. If `API_KEY` is unset the
service stays exactly as open as it always was (local dev/tests
unaffected). The gate applies only to the JSON API routes
(`/series*`, `/event-reports*`, `/triggers`, `/manual-articles`); the
server-rendered dashboard HTML pages (including the manual-article
submission form) stay open regardless, since they're meant for humans
clicking around in a browser (which can't easily attach a custom header)
rather than programmatic API clients -- see DEPLOYMENT.md for the full
rationale. `/health` and `/metrics` are always unauthenticated (needed for
healthchecks/Prometheus scraping).
"""

import importlib.util
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient

from shared.backfill import DEFAULT_BACKFILL_DAYS, DEFAULT_CHUNK_DAYS, run_backfill
from shared.bess_simulator import BessConfig, run_backtest
from shared.claim_extractor import extract_claims
from shared.db_manager import DatabaseManager
from shared.linkedin_embed import resolve_linkedin_content
from shared.logging_config import configure_logging
from shared.rss_reader import ArticleRef
from shared.units import unit_for
from shared.vector_store import QdrantStore

configure_logging()
logger = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# services/crawler/main.py hardcodes this same default (the container DNS
# name from docker-compose.yml); an env override is added here purely so
# this can be pointed elsewhere in tests/non-Docker runs without touching
# the crawler.
QDRANT_URL = os.getenv("QDRANT_URL", "http://vector-db:6333")

# Fixed `feed_name` for every manually-submitted post (see
# `_process_manual_article` below) -- always Tier 2 per README §6, and kept
# as one constant value (rather than folding the author into it) so
# `QdrantStore.scroll_by_source` can filter for "everything manually
# submitted" with one exact-match query; the actual author is carried
# separately on `ArticleRef.author` / `ClaimPayload.author`.
MANUAL_SUBMISSION_SOURCE = "LinkedIn"

REQUEST_COUNT = Counter(
    "api_requests_total", "API HTTP requests, by route/method/status", ["method", "path", "status"]
)
REQUEST_DURATION = Histogram(
    "api_request_duration_seconds", "API HTTP request duration, by route/method", ["method", "path"]
)


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """
    FastAPI dependency gating the JSON API routes on `API_KEY`. Reads the env
    var on every call (not once at import time) so it stays consistent with
    however a given deployment/test sets it, rather than freezing whatever
    value happened to be set when this module was first imported.
    """
    api_key = os.getenv("API_KEY")
    if api_key and x_api_key != api_key:
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


# A single pooled DatabaseManager, built lazily on first use and reused for
# the lifetime of the process (unlike the orchestrator's per-cycle instance
# -- this process is long-lived, so there's no reason to reconnect per
# request). Tests override `get_db` via `app.dependency_overrides` instead of
# touching this module-level state directly.
_db: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    global _db
    if _db is None:
        _db = DatabaseManager()
    return _db


# Same lazy-singleton pattern as `_db`/`get_db` above, for the manual-article
# submission tool's Qdrant access (point 2 of the M3 follow-on brief). Tests
# override `get_vector_store` via `app.dependency_overrides`, same as `get_db`.
_qdrant_client: AsyncQdrantClient | None = None
_vector_store: QdrantStore | None = None


def get_vector_store() -> QdrantStore:
    global _qdrant_client, _vector_store
    if _vector_store is None:
        _qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
        _vector_store = QdrantStore(_qdrant_client)
    return _vector_store


# Lazy singletons over the *real* services/orchestrator/main.py and
# services/crawler/main.py modules, loaded by file path via `importlib`
# (this repo has no `__init__.py`/package-mode -- see pyproject.toml's
# `package-mode = false` -- so a normal `import services.orchestrator.main`
# isn't available; this mirrors the exact convention
# tests/test_orchestrator_main.py and tests/test_crawler_main.py already use
# to import those files). Backing the on-demand `POST /orchestrator/run-now`
# / `POST /crawler/run-now` routes below (see DEPLOYMENT.md's "Cost control:
# AUTO_RUN_ENABLED" for the full rationale): this process has no message
# queue/RPC layer to reach into the separate orchestrator/crawler containers,
# so it loads their exact `run_synthesis_cycle`/`run_crawl_cycle` functions
# in-process instead, against the same DATABASE_URL/QDRANT_URL/
# ANTHROPIC_API_KEY/SLACK_WEBHOOK_URL those services use
# (docker-compose.yml's `api` environment; services/api/Dockerfile now also
# COPYs those two `main.py` files in for this). Loaded lazily (on first
# on-demand trigger, not at import time) so this module stays importable
# even without those two files present, and tests override these via
# `app.dependency_overrides` (same pattern as `get_db`/`get_vector_store`)
# rather than ever triggering the real load.
_orchestrator_main = None
_crawler_main = None


def _load_sibling_service_module(service_dir: str, module_name: str):
    """Loads `services/<service_dir>/main.py` by file path. `Path(__file__).parent.parent`
    is `services/` (this file is `services/api/main.py`), matching both a local
    checkout and the Docker image's `/app/services/` layout."""
    path = Path(__file__).resolve().parent.parent / service_dir / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_orchestrator_main():
    global _orchestrator_main
    if _orchestrator_main is None:
        _orchestrator_main = _load_sibling_service_module("orchestrator", "orchestrator_main")
    return _orchestrator_main


def get_crawler_main():
    global _crawler_main
    if _crawler_main is None:
        _crawler_main = _load_sibling_service_module("crawler", "crawler_main")
    return _crawler_main


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    global _db, _qdrant_client, _vector_store
    if _db is not None:
        _db.close()
        _db = None
    if _qdrant_client is not None:
        await _qdrant_client.close()
        _qdrant_client = None
        _vector_store = None


app = FastAPI(
    title="AncillaryNews API",
    description="Read surface over Event Reports, market data, and rule-engine triggers.",
    lifespan=lifespan,
)


@app.middleware("http")
async def _record_request_metrics(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start
    # `request.scope["route"].path` (the matched route template, e.g.
    # "/series/{market}/{zone}/{product}") rather than the raw URL path,
    # so per-path cardinality in Prometheus stays bounded regardless of how
    # many distinct (market, zone, product) values get requested.
    route = request.scope.get("route")
    path = route.path if route is not None else request.url.path
    REQUEST_COUNT.labels(method=request.method, path=path, status=response.status_code).inc()
    REQUEST_DURATION.labels(method=request.method, path=path).observe(duration)
    return response


# --- JSON API ------------------------------------------------------------


@app.get("/health")
def health():
    """Trivial liveness check -- deliberately does not touch the database,
    so it stays green even if `db` is briefly unreachable. Always
    unauthenticated (needed for container healthchecks)."""
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Prometheus text-format exposition. Always unauthenticated (needed for
    scraping) -- see prometheus/prometheus.yml."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/series", dependencies=[Depends(require_api_key)])
def list_series(db: DatabaseManager = Depends(get_db)):
    """Distinct (market, zone, product) series available for charting."""
    rows = db.fetch_distinct_series()
    return {"series": [{"market": m, "zone": z, "product": p} for m, z, p in rows]}


@app.get("/series/{market}/{zone}/{product}", dependencies=[Depends(require_api_key)])
def series_data(
    market: str,
    zone: str,
    product: str,
    limit: int = Query(500, ge=1, le=5000),
    history: bool = False,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    db: DatabaseManager = Depends(get_db),
):
    """
    Time-series data for one series, for charting. Reads the latest value
    per `time` from the `market_data` view by default; pass `?history=true`
    to see every revision from `market_data_history` instead.
    """
    rows = db.fetch_series_values(
        market,
        zone,
        product,
        limit=limit,
        time_from=time_from,
        time_to=time_to,
        history=history,
    )
    return {
        "market": market,
        "zone": zone,
        "product": product,
        "history": history,
        "count": len(rows),
        "data": rows,
    }


@app.get("/event-reports", dependencies=[Depends(require_api_key)])
def list_event_reports(
    market: str | None = None,
    zone: str | None = None,
    product: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DatabaseManager = Depends(get_db),
):
    """Published Event Reports, most-recent-first, filterable and paginated."""
    rows = db.fetch_event_reports(
        market=market,
        zone=zone,
        product=product,
        time_from=time_from,
        time_to=time_to,
        limit=limit,
        offset=offset,
    )
    return {"count": len(rows), "limit": limit, "offset": offset, "reports": rows}


@app.get("/event-reports/{event_id}", dependencies=[Depends(require_api_key)])
def get_event_report(event_id: str, db: DatabaseManager = Depends(get_db)):
    """Single Event Report by ID; 404 if unknown."""
    row = db.fetch_event_report(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Event report {event_id!r} not found")
    return row


@app.get("/triggers", dependencies=[Depends(require_api_key)])
def list_triggers(
    market: str | None = None,
    zone: str | None = None,
    product: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DatabaseManager = Depends(get_db),
):
    """Persisted rule-engine triggers (shared/rule_engine.py), most-recent-first."""
    rows = db.fetch_triggers(market=market, zone=zone, product=product, limit=limit, offset=offset)
    return {"count": len(rows), "limit": limit, "offset": offset, "triggers": rows}


# --- BESS backtest simulator (shared/bess_simulator.py) --------------------
#
# Triggers and reads back BESS (Battery Energy Storage System) backtest
# runs: a simple threshold-rule simulation of a battery's charge/discharge
# and capacity-reservation decisions over *real* historical
# `market_data_history` data, with estimated revenue by stream. This is a
# **backtest over history**, not a live/forward dispatch service -- there is
# no scheduled job here, only an on-demand trigger endpoint. Both revenue
# streams the simulator computes are explicitly estimates, not a real
# co-optimized dispatch (see shared/bess_simulator.py's module docstring for
# every simplification).


class BessBacktestRequest(BaseModel):
    zone: str
    start_time: datetime
    end_time: datetime
    # Optional BessConfig overrides -- unset fields fall back to
    # shared.bess_simulator.BessConfig's own defaults (a generic 1 MW / 2
    # MWh unit), not anything hardcoded here.
    power_mw: float | None = None
    capacity_mwh: float | None = None
    round_trip_efficiency: float | None = None
    soc_min_fraction: float | None = None
    soc_max_fraction: float | None = None
    starting_soc_fraction: float | None = None
    arbitrage_lookback_periods: int | None = None
    arbitrage_z_threshold: float | None = None
    capacity_commit_mw: float | None = None
    max_cycles_per_day: float | None = None
    afrr_activation_participation_rate: float | None = None
    # e.g. [["FCR", "price"], ["FCR", "up"], ["FCR", "down"], ["aFRR_capacity", "up"]]
    # to opt a DK2 run into FCR-D on top of the defaults -- unset falls back
    # to BessConfig.capacity_markets' own default (FCR/aFRR_capacity only).
    capacity_markets: list[tuple[str, str]] | None = None


def _build_bess_config(overrides: dict) -> BessConfig:
    """Builds a BessConfig from a dict of possibly-None overrides, keeping BessConfig's own defaults
    for any field left unset (None)."""
    kwargs = {k: v for k, v in overrides.items() if v is not None}
    # Pydantic deserializes capacity_markets as a list of tuples, but
    # BessConfig (frozen) declares it as tuple[tuple[str,str],...] -- coerce
    # the outer container too so a JSON-API-triggered BessConfig matches the
    # type its own dataclass declares, not just its dashboard-form-triggered
    # sibling (which already builds a tuple directly).
    if "capacity_markets" in kwargs:
        kwargs["capacity_markets"] = tuple(tuple(leg) for leg in kwargs["capacity_markets"])
    return BessConfig(**kwargs)


def _run_and_save_bess_backtest(
    db: DatabaseManager, zone: str, start_time: datetime, end_time: datetime, config: BessConfig
) -> dict:
    """Runs one backtest and persists it (init-db/04-bess-simulations.sql),
    returning the run summary dict `save_bess_run`'s caller needs for both
    the JSON API and the dashboard form."""
    result = run_backtest(db, zone, start_time, end_time, config)
    run_id = db.save_bess_run(result)
    return {
        "run_id": run_id,
        "zone": result.zone,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "tick_count": len(result.ticks),
        "total_arbitrage_revenue_dkk": result.total_arbitrage_revenue_dkk,
        "total_capacity_revenue_dkk": result.total_capacity_revenue_dkk,
        "total_revenue_dkk": result.total_revenue_dkk,
        "full_cycle_equivalents": result.full_cycle_equivalents,
        "total_afrr_activation_revenue_eur": result.total_afrr_activation_revenue_eur,
        # DK2's EUR-denominated capacity legs (shared/bess_simulator.py's
        # per-currency buckets, module docstring §2) -- always 0.0 for an
        # all-DKK DK1 run. `len(currencies_present) > 1` is this run's
        # "not summable to one number" signal, surfaced to the dashboard
        # template rather than recomputed there.
        "total_capacity_revenue_eur": result.total_capacity_revenue_eur,
        "currencies_present": sorted(result.currencies_present),
        # How many periods each capacity leg cleared at exactly 0 (e.g.
        # "FFR cleared at 0 for 720/720 hours in this window") -- see
        # BacktestResult.zero_price_periods_by_leg's docstring. Not
        # persisted (init-db/04-bess-simulations.sql has no column for it),
        # so only available here, on the freshly-computed result, not on a
        # later re-fetch of this run_id.
        "zero_price_periods_by_leg": result.zero_price_periods_by_leg,
        "capacity_allocation_fell_back_to_even": result.capacity_allocation_fell_back_to_even,
    }


@app.post("/bess/backtest", dependencies=[Depends(require_api_key)])
def trigger_bess_backtest(req: BessBacktestRequest, db: DatabaseManager = Depends(get_db)):
    """
    Runs a BESS backtest over `[start_time, end_time]` for `zone` against
    real historical `market_data_history` data and persists the result
    (init-db/04-bess-simulations.sql). Synchronous: a backtest over a
    bounded historical window is a bounded amount of work, unlike the
    crawler's fire-and-forget cycle, so the caller gets the run's summary
    directly rather than having to poll for it.
    """
    overrides = req.model_dump(exclude={"zone", "start_time", "end_time"})
    try:
        config = _build_bess_config(overrides)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return _run_and_save_bess_backtest(db, req.zone, req.start_time, req.end_time, config)


@app.get("/bess/runs", dependencies=[Depends(require_api_key)])
def list_bess_runs(
    zone: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DatabaseManager = Depends(get_db),
):
    """Persisted BESS backtest run headers, most-recently-created-first."""
    rows = db.fetch_bess_runs(zone=zone, limit=limit, offset=offset)
    return {"count": len(rows), "limit": limit, "offset": offset, "runs": rows}


@app.get("/bess/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_bess_run(run_id: int, include_ticks: bool = False, db: DatabaseManager = Depends(get_db)):
    """One BESS backtest run's header, optionally with every tick (`?include_ticks=true`); 404 if
    unknown."""
    row = db.fetch_bess_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"BESS run {run_id} not found")
    if include_ticks:
        row = {**row, "ticks": db.fetch_bess_ticks(run_id)}
    return row


# --- Morning Brief (M5) ----------------------------------------------------
#
# Read surface + on-demand trigger over the Morning Brief pipeline
# (shared/morning_brief_editor.py, services/orchestrator/main.py:
# run_morning_brief, init-db/05-morning-briefs.sql). Like the BESS backtest
# routes above, there's no scheduled job *here* -- the automatic daily cron
# trigger lives on the orchestrator service (MORNING_BRIEF_AUTO_RUN_ENABLED);
# this API service only ever reads persisted briefs and exposes the same
# on-demand run-now escape hatch as /orchestrator/run-now.


@app.get("/morning-briefs", dependencies=[Depends(require_api_key)])
def list_morning_briefs(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DatabaseManager = Depends(get_db),
):
    """Persisted Morning Briefs, most-recent-brief_date-first."""
    rows = db.fetch_morning_briefs(limit=limit, offset=offset)
    return {"count": len(rows), "limit": limit, "offset": offset, "briefs": rows}


@app.get("/morning-briefs/{brief_id}", dependencies=[Depends(require_api_key)])
def get_morning_brief(brief_id: int, db: DatabaseManager = Depends(get_db)):
    """Single Morning Brief by id; 404 if unknown."""
    row = db.fetch_morning_brief(brief_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Morning brief {brief_id} not found")
    return row


@app.post("/morning-briefs/run-now", dependencies=[Depends(require_api_key)])
async def trigger_morning_brief_run_now(orchestrator_main=Depends(get_orchestrator_main)):
    """
    Fires one real Morning Brief run right now: price recap -> forecasts ->
    illustrative BESS estimates -> compose -> persist -> Slack + email
    delivery, independent of `MORNING_BRIEF_AUTO_RUN_ENABLED` -- mirrors
    `trigger_orchestrator_run_now`'s exact on-demand-escape-hatch pattern
    (calls `orchestrator_main.run_morning_brief()` directly, bypassing the
    gate). Costs one Claude Opus call per forecast horizon actually
    refreshed (usually a cache-hit for most horizons) plus the price-recap
    call. Returns `run_morning_brief`'s own summary dict.
    """
    return await orchestrator_main.run_morning_brief()


# --- manual article submission (LinkedIn paste-in tool) -------------------
#
# A new entry point into the *existing* crawler pipeline
# (shared/claim_extractor.py + shared/vector_store.py), for content that
# can't be polled via RSS (README §6 two-tier trust model: LinkedIn has no
# public API for an arbitrary profile's posts, and scraping it violates
# their ToS -- both correctly ruled out). Instead, a human pastes in a
# post's URL/author/text when they see something worth capturing, and it
# runs through the exact same Claude Haiku extraction + Qdrant storage as
# every RSS article, just skipping the HTML-fetch/trafilatura step (the
# text is already plain, already copied -- see shared/article_extractor.py,
# which only applies to the RSS pipeline's web-fetching).
#
# Always Tier 2 (`ArticleRef.feed_tier="tier2"`): an individual's LinkedIn
# commentary is never citable as bare fact regardless of what Claude
# classifies it as -- this reuses shared/claim_extractor.py's existing
# fact->theory downgrade rather than building a new mechanism.
#
# `text` also doubles as an auto-detecting input (shared/linkedin_embed.py):
# a human can paste LinkedIn's own "Embed this post" iframe HTML, a bare
# embed URL, or a regular post URL into it instead of plain text, and
# `_process_manual_article` will fetch+use the real post content and its
# canonical URL (overriding whatever's in `url` -- the canonical URL is the
# real, stable dedup key) rather than treating the pasted markup as the
# post body itself. Anything that doesn't match one of those shapes, or any
# fetch failure, falls back to today's plain-pasted-text behavior --
# `resolve_linkedin_content` never raises.


class ManualArticleSubmission(BaseModel):
    url: str
    author: str | None = None
    title: str | None = None
    text: str


async def _process_manual_article(submission: ManualArticleSubmission, store: QdrantStore) -> dict:
    """
    Runs one manually-submitted post through the crawler's claim-extraction
    + storage pipeline, dedup'd by URL (same `is_processed` convention as
    services/crawler/main.py's `process_article`). Unlike the crawler's
    fire-and-forget background cycle, this returns the outcome directly --
    a human is actively submitting and wants to see what was captured.

    `submission.text` is first run through `resolve_linkedin_content`
    (shared/linkedin_embed.py) in case it's LinkedIn embed iframe HTML/URL
    rather than plain text; when that resolves, the fetched post text and
    its canonical URL are used in place of `submission.text`/`submission.url`
    for everything below.
    """
    text = submission.text
    url = submission.url
    author = submission.author

    linkedin_content = await resolve_linkedin_content(submission.text)
    if linkedin_content is not None:
        text = linkedin_content.text
        url = linkedin_content.canonical_url
        author = author or linkedin_content.author

    await store.ensure_collection()

    article = ArticleRef(
        url=url,
        title=submission.title or "",
        author=author,
        published=None,
        feed_name=MANUAL_SUBMISSION_SOURCE,
        feed_tier="tier2",
    )

    if await store.is_processed(article.url):
        logger.info("Manual article %s already processed; skipping re-extraction", article.url)
        return {
            "url": article.url,
            "title": article.title,
            "author": article.author,
            "feed_tier": article.feed_tier,
            "already_processed": True,
            "stored_raw": False,
            "summary": None,
            "claims": [],
        }

    extraction = await extract_claims(text, article)
    if extraction is None:
        # No ANTHROPIC_API_KEY (or the call/parse failed outright): store the
        # raw pasted text without derived claims -- same fallback precedent
        # as services/crawler/main.py's process_article.
        await store.upsert_raw_article(article, text)
        return {
            "url": article.url,
            "title": article.title,
            "author": article.author,
            "feed_tier": article.feed_tier,
            "already_processed": False,
            "stored_raw": True,
            "summary": None,
            "claims": [],
        }

    await store.upsert_claims(article, extraction.claims)
    return {
        "url": article.url,
        "title": article.title,
        "author": article.author,
        "feed_tier": article.feed_tier,
        "already_processed": False,
        "stored_raw": False,
        "summary": extraction.summary,
        "claims": [{"claim": c.claim, "claim_type": c.claim_type} for c in extraction.claims],
    }


@app.post("/manual-articles", dependencies=[Depends(require_api_key)])
async def submit_manual_article(
    submission: ManualArticleSubmission, store: QdrantStore = Depends(get_vector_store)
):
    """
    Paste-in entry point for LinkedIn (or any other non-RSS-able) posts.
    `url` is the dedup key -- resubmitting the same post URL is a no-op
    rather than a duplicate (see `_process_manual_article`). Gated behind
    `API_KEY` like every other mutating/protected route: submitting data is
    at least as sensitive as the read-only JSON routes above.
    """
    return await _process_manual_article(submission, store)


# --- on-demand orchestrator/crawler triggers (cost-control escape hatch) --
#
# services/orchestrator/main.py and services/crawler/main.py both default to
# AUTO_RUN_ENABLED=false (their automatic scheduled cycles are opt-in, not
# opt-out -- see DEPLOYMENT.md's "Cost control: AUTO_RUN_ENABLED"), since
# every fired rule-engine trigger burns a Claude Opus call (orchestrator) and
# every new article burns a Claude Haiku call (crawler). These two routes are
# the always-available on-demand escape hatch, independent of
# AUTO_RUN_ENABLED -- they call the exact same run_synthesis_cycle /
# run_crawl_cycle functions the schedulers use (via get_orchestrator_main /
# get_crawler_main above), never a duplicated copy of that pipeline logic.
# Gated behind API_KEY like every other mutating route (/manual-articles,
# /bess/backtest).


@app.post("/orchestrator/run-now", dependencies=[Depends(require_api_key)])
async def trigger_orchestrator_run_now(orchestrator_main=Depends(get_orchestrator_main)):
    """
    Fires one real orchestrator synthesis cycle right now: evaluates the
    rule engine, then runs RAG + Claude Opus synthesis for every trigger
    that fires. Costs one Opus call per trigger fired this cycle. Returns
    `run_synthesis_cycle`'s own `{"triggers_fired": ..., "reports_published":
    ...}` summary.
    """
    return await orchestrator_main.run_synthesis_cycle()


@app.post("/crawler/run-now", dependencies=[Depends(require_api_key)])
async def trigger_crawler_run_now(crawler_main=Depends(get_crawler_main)):
    """
    Fires one real crawler cycle right now: polls every RSS feed and runs
    Claude Haiku claim extraction on every new article found. Costs one
    Haiku call per new article this cycle. Returns `run_crawl_cycle`'s own
    `{"articles_processed": ..., "claims_extracted": ...}` summary.
    """
    return await crawler_main.run_crawl_cycle()


# --- on-demand historical backfill (shared/backfill.py) --------------------
#
# Unlike the orchestrator/crawler run-now routes above, this makes zero
# Anthropic API calls -- pure Energinet HTTP polling + Postgres writes, the
# same free, always-on nature as services/ingestor/main.py's scheduled
# poller (see DEPLOYMENT.md's "Cost control: AUTO_RUN_ENABLED" -- ingestor
# has no such gate and never needs one). So this route is always available,
# with no LLM-spend concern to gate on; it's still gated behind API_KEY like
# every other mutating route, since triggering database writes is at least
# as sensitive as the read-only JSON routes above.


class BackfillRequest(BaseModel):
    start_time: datetime | None = None
    end_time: datetime | None = None
    # Subset of shared.backfill.backfillable_datasets() names, e.g.
    # ["fcr_dk1"] -- also accepts the M6 fundamentals-forecasting datasets
    # (shared.backfill.FORECASTING_DATASET_NAMES), which must be named
    # explicitly here. Unset (None) backfills every BESS-relevant dataset
    # only (shared.backfill.bess_datasets()) -- the forecasting datasets are
    # deliberately excluded from that default, see FORECASTING_DATASET_NAMES'
    # docstring.
    datasets: list[str] | None = None
    chunk_days: int = DEFAULT_CHUNK_DAYS


@app.post("/ingestor/backfill", dependencies=[Depends(require_api_key)])
async def trigger_backfill(req: BackfillRequest, db: DatabaseManager = Depends(get_db)):
    """
    Fires a one-time/on-demand historical backfill (shared/backfill.py) of
    the datasets shared/bess_simulator.py reads, paging through
    api.energidataservice.dk's start/end date-range query params rather than
    the live ingestor's "most recent N records" pattern -- see
    shared/backfill.py's module docstring for the full mechanism and its
    idempotency/safe-to-re-run notes. Defaults to the trailing 30 days
    ending now if start_time/end_time are omitted. Reuses this process's own
    pooled DatabaseManager (`db`) rather than opening a second connection
    pool the way the standalone scripts/backfill_history.py script does.
    """
    end_time = req.end_time or datetime.now(UTC)
    start_time = req.start_time or (end_time - timedelta(days=DEFAULT_BACKFILL_DAYS))
    try:
        return await run_backfill(
            start_time,
            end_time,
            dataset_names=req.datasets,
            chunk_days=req.chunk_days,
            db=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# --- dashboard (server-rendered Jinja2 HTML, no JS build toolchain) ------


@app.get("/", response_class=HTMLResponse)
def dashboard_home(request: Request, db: DatabaseManager = Depends(get_db)):
    """Recent Event Reports list -- the dashboard's landing page. Also hosts the
    "Run orchestrator now" / "Run crawler now" on-demand buttons (see the two POST routes
    below) -- `orchestrator_result`/`crawler_result` are `None` on a plain `GET`."""
    reports = db.fetch_event_reports(limit=25, offset=0)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "reports": reports,
            "orchestrator_result": None,
            "crawler_result": None,
            "backfill_result": None,
            "morning_brief_result": None,
        },
    )


@app.post("/dashboard/orchestrator/run-now", response_class=HTMLResponse)
async def dashboard_trigger_orchestrator_run_now(
    request: Request,
    db: DatabaseManager = Depends(get_db),
    orchestrator_main=Depends(get_orchestrator_main),
):
    """
    Dashboard counterpart to `POST /orchestrator/run-now` above -- same
    synchronous trigger-and-show-result flow as `/dashboard/bess/new`: runs
    the real cycle, then re-renders the dashboard home with the run's
    summary shown inline. Stays open regardless of `API_KEY` (same
    "dashboard HTML is for humans clicking around a browser" exception as
    every other dashboard route, see module docstring) -- the underlying
    JSON `/orchestrator/run-now` route this shares logic with is what's
    gated; see DEPLOYMENT.md if you want this button itself gated too (e.g.
    a reverse-proxy auth layer), since a click here spends real Anthropic
    credit.
    """
    result = await orchestrator_main.run_synthesis_cycle()
    reports = db.fetch_event_reports(limit=25, offset=0)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "reports": reports,
            "orchestrator_result": result,
            "crawler_result": None,
            "backfill_result": None,
            "morning_brief_result": None,
        },
    )


@app.post("/dashboard/crawler/run-now", response_class=HTMLResponse)
async def dashboard_trigger_crawler_run_now(
    request: Request,
    db: DatabaseManager = Depends(get_db),
    crawler_main=Depends(get_crawler_main),
):
    """Dashboard counterpart to `POST /crawler/run-now` above -- see
    `dashboard_trigger_orchestrator_run_now`'s docstring for the full rationale."""
    result = await crawler_main.run_crawl_cycle()
    reports = db.fetch_event_reports(limit=25, offset=0)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "reports": reports,
            "orchestrator_result": None,
            "crawler_result": result,
            "backfill_result": None,
            "morning_brief_result": None,
        },
    )


@app.post("/dashboard/ingestor/backfill", response_class=HTMLResponse)
async def dashboard_trigger_backfill(
    request: Request,
    days: int = Form(DEFAULT_BACKFILL_DAYS),
    datasets: str = Form(""),
    db: DatabaseManager = Depends(get_db),
):
    """
    Dashboard counterpart to `POST /ingestor/backfill` -- fires a real
    historical backfill for the trailing `days` days (optionally restricted
    to a comma-separated `datasets` subset) across the BESS-relevant
    datasets (shared/backfill.py), then re-renders the dashboard home with
    the run's summary shown inline. Stays open regardless of `API_KEY` (same
    "dashboard HTML is for humans clicking around a browser" exception as
    every other dashboard route). Unlike the orchestrator/crawler buttons
    above, this makes zero Anthropic API calls, so there's no LLM-spend
    warning here -- see shared/backfill.py's module docstring.
    """
    end_time = datetime.now(UTC)
    start_time = end_time - timedelta(days=days)
    dataset_names = [n.strip() for n in datasets.split(",") if n.strip()] or None
    try:
        result = await run_backfill(start_time, end_time, dataset_names=dataset_names, db=db)
    except ValueError as e:
        result = {"error": str(e)}
    reports = db.fetch_event_reports(limit=25, offset=0)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "reports": reports,
            "orchestrator_result": None,
            "crawler_result": None,
            "backfill_result": result,
            "morning_brief_result": None,
        },
    )


@app.get("/dashboard/event-reports/{event_id}", response_class=HTMLResponse)
def dashboard_event_report(request: Request, event_id: str, db: DatabaseManager = Depends(get_db)):
    """Full README §2 shape for one Event Report, with a link to the report
    it corrects if this one is a correction."""
    row = db.fetch_event_report(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Event report {event_id!r} not found")

    corrects = None
    if row.get("corrects_event_id"):
        corrects = db.fetch_event_report(row["corrects_event_id"])

    return templates.TemplateResponse(
        request, "event_report.html", {"row": row, "corrects": corrects}
    )


@app.get("/dashboard/series", response_class=HTMLResponse)
def dashboard_series_picker(request: Request, db: DatabaseManager = Depends(get_db)):
    """Lists every available series, linking to each one's detail page."""
    series = db.fetch_distinct_series()
    return templates.TemplateResponse(request, "series_list.html", {"series": series})


@app.get("/dashboard/series/{market}/{zone}/{product}", response_class=HTMLResponse)
def dashboard_series_detail(
    request: Request,
    market: str,
    zone: str,
    product: str,
    limit: int = 200,
    db: DatabaseManager = Depends(get_db),
):
    """Recent values for one series as a table, plus a Chart.js line chart
    (CDN-loaded, no local JS build step) when there's data to plot."""
    rows = db.fetch_series_values(market, zone, product, limit=limit)
    return templates.TemplateResponse(
        request,
        "series_detail.html",
        {"market": market, "zone": zone, "product": product, "rows": rows},
    )


@app.get("/dashboard/triggers", response_class=HTMLResponse)
def dashboard_triggers(
    request: Request,
    market: str | None = None,
    zone: str | None = None,
    product: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: DatabaseManager = Depends(get_db),
):
    """
    Manual-pull listing of recent persisted rule-engine triggers
    (shared/rule_engine.py, same `DatabaseManager.fetch_triggers` backing
    the JSON `GET /triggers` route above), most-recently-detected-first,
    with optional market/zone/product filters and offset/limit pagination.

    This page exists because `run_rule_engine` no longer auto-posts every
    fired trigger to Slack (that was M2 behavior the user experienced as
    spam — most raw triggers never survive citation validation into a
    synthesized Event Report, which is the only thing still auto-posted via
    `send_event_report_alert`). Triggers are still fully persisted; this is
    where a human checks them whenever they want, instead of every single
    one landing in Slack unprompted.
    """
    triggers = db.fetch_triggers(
        market=market, zone=zone, product=product, limit=limit, offset=offset
    )
    return templates.TemplateResponse(
        request,
        "triggers_list.html",
        {
            "triggers": triggers,
            "market": market,
            "zone": zone,
            "product": product,
            "limit": limit,
            "offset": offset,
        },
    )


@app.get("/dashboard/manual-articles", response_class=HTMLResponse)
def dashboard_manual_article_form(request: Request):
    """The LinkedIn paste-in form -- URL, author, title, and a content field
    that auto-detects embed iframe HTML/URL vs. plain text (see
    shared/linkedin_embed.py, `_process_manual_article`), posting to the
    handler below. Stays open regardless of `API_KEY` (same "dashboard HTML
    is for humans clicking around a browser" exception as every other
    dashboard route, see module docstring); the underlying JSON
    `/manual-articles` route this shares logic with is what's gated."""
    return templates.TemplateResponse(request, "manual_article_form.html", {"result": None})


@app.post("/dashboard/manual-articles", response_class=HTMLResponse)
async def dashboard_submit_manual_article(
    request: Request,
    url: str = Form(...),
    author: str = Form(""),
    title: str = Form(""),
    text: str = Form(...),
    store: QdrantStore = Depends(get_vector_store),
):
    """Runs the pasted-in form through the same `_process_manual_article`
    logic the JSON API uses, then re-renders the form with the extracted
    claims shown as confirmation (README brief point 4/5: a human is
    actively submitting and wants to see the result immediately)."""
    submission = ManualArticleSubmission(
        url=url, author=author or None, title=title or None, text=text
    )
    result = await _process_manual_article(submission, store)
    return templates.TemplateResponse(request, "manual_article_form.html", {"result": result})


@app.get("/dashboard/manual-articles/recent", response_class=HTMLResponse)
async def dashboard_recent_manual_claims(
    request: Request, store: QdrantStore = Depends(get_vector_store)
):
    """Minimal browsing surface over manually-submitted claims in Qdrant --
    the dashboard otherwise has no way to browse anything the crawler
    pipeline has stored (Phase 5's dashboard only ever covered
    `market_data`/`event_reports`, never the vector store)."""
    claims = await store.scroll_by_source(MANUAL_SUBMISSION_SOURCE, limit=100)
    return templates.TemplateResponse(request, "manual_articles_recent.html", {"claims": claims})


# --- BESS backtest dashboard ------------------------------------------------


@app.get("/dashboard/bess", response_class=HTMLResponse)
def dashboard_bess_list(request: Request, db: DatabaseManager = Depends(get_db)):
    """Lists recent BESS backtest runs, linking to each one's detail page."""
    runs = db.fetch_bess_runs(limit=25)
    return templates.TemplateResponse(request, "bess_list.html", {"runs": runs})


@app.get("/dashboard/bess/new", response_class=HTMLResponse)
def dashboard_bess_new_form(request: Request):
    """Form to trigger a new backtest run over a given zone/time window, with the battery's
    defaults pre-filled (shared.bess_simulator.BessConfig)."""
    return templates.TemplateResponse(
        request, "bess_new.html", {"error": None, "defaults": BessConfig()}
    )


@app.post("/dashboard/bess/new", response_class=HTMLResponse)
def dashboard_bess_trigger(
    request: Request,
    zone: str = Form(...),
    start_time: datetime = Form(...),
    end_time: datetime = Form(...),
    power_mw: float = Form(...),
    capacity_mwh: float = Form(...),
    round_trip_efficiency: float = Form(...),
    soc_min_fraction: float = Form(...),
    soc_max_fraction: float = Form(...),
    starting_soc_fraction: float = Form(...),
    arbitrage_lookback_periods: int = Form(...),
    arbitrage_z_threshold: float = Form(...),
    capacity_commit_mw: float = Form(...),
    afrr_activation_participation_rate: float = Form(...),
    # Checkbox group rather than asking the user to type raw
    # capacity_markets tuples -- each translated into its own extra leg(s)
    # server-side below. Every one of these is meaningless for at least one
    # zone (FCR-D and FFR are DK2-only; see shared/datasets.py's fcr_dk2/
    # ffr_dk2 entries) -- previously that just silently earned nothing
    # (empty series, not an error); now rejected outright below via
    # `unit_for(...) is None`, since silently-zero legs are exactly the
    # kind of "looks configured but isn't" gap this repo tries to avoid
    # elsewhere (see shared/bess_simulator.py's leg_currency ValueError).
    include_fcr_d: bool = Form(False),
    include_ffr: bool = Form(False),
    include_afrr_down: bool = Form(False),
    db: DatabaseManager = Depends(get_db),
):
    """Runs the submitted form through the same trigger logic the JSON API uses, then redirects to
    the new run's detail page."""
    capacity_markets = [("FCR", "price"), ("aFRR_capacity", "up")]
    if include_fcr_d:
        capacity_markets += [("FCR", "up"), ("FCR", "down")]
    if include_afrr_down:
        capacity_markets += [("aFRR_capacity", "down")]
    if include_ffr:
        capacity_markets += [("FFR", "price")]
    try:
        # Reject a market/zone combination the registry itself already
        # knows is meaningless (e.g. FFR ticked for a DK1 run) -- `unit_for`
        # returning None means shared/units.py has no entry at all for that
        # (market, zone, product), not just "no data yet". Driven entirely
        # by the registry, not a hardcoded per-market zone list, so this
        # needs no new market literals here.
        #
        # Known gap, not a new one: `fcr_dk2`'s "up"/"down" (FCR-D) products
        # are registered *zone-agnostically* in shared/units.py (their
        # source dataset's real PriceArea values span DK2 and several
        # Swedish zones, not one fixed zone -- see shared/datasets.py's
        # fcr_dk2 comment), so `unit_for("FCR", "DK1", "up")` still resolves
        # to a real unit even though DK1 never actually publishes FCR-D
        # data -- this check does not catch that specific case (unlike
        # `ffr_dk2`, which IS registered under a fixed zone="DK2" and so
        # correctly returns None for any other zone). A DK1 run with FCR-D
        # ticked still just silently earns nothing on those legs, exactly
        # as it always has -- not a regression Stage 4 introduces, just a
        # gap it doesn't happen to close.
        unavailable = [
            (market, product)
            for market, product in capacity_markets
            if unit_for(market, zone, product) is None
        ]
        if unavailable:
            raise ValueError(
                f"the following capacity market(s) are not available in zone {zone!r}: "
                f"{unavailable}"
            )
        config = BessConfig(
            power_mw=power_mw,
            capacity_mwh=capacity_mwh,
            round_trip_efficiency=round_trip_efficiency,
            soc_min_fraction=soc_min_fraction,
            soc_max_fraction=soc_max_fraction,
            starting_soc_fraction=starting_soc_fraction,
            arbitrage_lookback_periods=arbitrage_lookback_periods,
            arbitrage_z_threshold=arbitrage_z_threshold,
            capacity_commit_mw=capacity_commit_mw,
            afrr_activation_participation_rate=afrr_activation_participation_rate,
            capacity_markets=tuple(capacity_markets),
            # FFR clears at/near 0 today (shared/datasets.py's ffr_dk2
            # entry) -- "even" allocation would silently dilute the other,
            # genuinely-earning legs' shares purely from adding it as a
            # group (shared/bess_simulator.py's module docstring §2).
            # "price_ranked" avoids that; only switched on when FFR is
            # actually in the stack, so every other run's numbers stay
            # exactly as reproducible as before.
            capacity_allocation="price_ranked" if include_ffr else "even",
        )
        summary = _run_and_save_bess_backtest(db, zone, start_time, end_time, config)
    except ValueError as e:
        return templates.TemplateResponse(
            request, "bess_new.html", {"error": str(e), "defaults": BessConfig()}
        )
    return RedirectResponse(f"/dashboard/bess/{summary['run_id']}", status_code=303)


@app.get("/dashboard/bess/{run_id}", response_class=HTMLResponse)
def dashboard_bess_detail(request: Request, run_id: int, db: DatabaseManager = Depends(get_db)):
    """Full detail for one BESS backtest run: revenue-by-stream summary, full-cycle-equivalents, a
    SoC-over-time chart, and the tick-level action table."""
    run = db.fetch_bess_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"BESS run {run_id} not found")
    ticks = db.fetch_bess_ticks(run_id)
    return templates.TemplateResponse(request, "bess_detail.html", {"run": run, "ticks": ticks})


# --- Morning Brief dashboard -------------------------------------------------


@app.get("/dashboard/morning-briefs", response_class=HTMLResponse)
def dashboard_morning_briefs_list(request: Request, db: DatabaseManager = Depends(get_db)):
    """Lists recent Morning Briefs, linking to each one's detail page (mirrors
    `dashboard_bess_list` above)."""
    briefs = db.fetch_morning_briefs(limit=25)
    return templates.TemplateResponse(request, "morning_briefs_list.html", {"briefs": briefs})


@app.get("/dashboard/morning-briefs/{brief_id}", response_class=HTMLResponse)
def dashboard_morning_brief_detail(
    request: Request, brief_id: int, db: DatabaseManager = Depends(get_db)
):
    """Full detail for one Morning Brief: price recap, three forecast cards with
    confidence badges, BESS estimate cards linking to `/dashboard/bess/{run_id}`,
    and delivery status."""
    row = db.fetch_morning_brief(brief_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Morning brief {brief_id} not found")
    return templates.TemplateResponse(request, "morning_brief_detail.html", {"row": row})


@app.post("/dashboard/morning-briefs/run-now", response_class=HTMLResponse)
async def dashboard_trigger_morning_brief_run_now(
    request: Request,
    db: DatabaseManager = Depends(get_db),
    orchestrator_main=Depends(get_orchestrator_main),
):
    """
    Dashboard counterpart to `POST /morning-briefs/run-now` -- same
    synchronous trigger-and-show-result flow as
    `dashboard_trigger_orchestrator_run_now` (see that route's docstring for
    the full rationale). Redirects straight to the new brief's detail page
    on success, same UX as `dashboard_bess_trigger`.
    """
    result = await orchestrator_main.run_morning_brief()
    if result.get("brief_id") is not None:
        return RedirectResponse(f"/dashboard/morning-briefs/{result['brief_id']}", status_code=303)
    reports = db.fetch_event_reports(limit=25, offset=0)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "reports": reports,
            "orchestrator_result": None,
            "crawler_result": None,
            "backfill_result": None,
            "morning_brief_result": result,
        },
    )
