"""
M3 Insight Crawler (README §3B / §9): polls RSS feeds (shared/rss_feeds.py),
extracts each new article to Markdown (shared/article_extractor.py),
extracts claims via Claude Haiku (shared/claim_extractor.py), and embeds +
upserts into Qdrant (shared/vector_store.py).

Follow-ups explicitly deferred, per the M3 brief:
- JS-rendered sources (Playwright) — every feed evaluated for this pass
  extracts fine statically via trafilatura; revisit if/when a feed is added
  whose articles clearly need JS rendering to extract (see
  shared/article_extractor.py's MIN_EXTRACTED_CHARS heuristic).
- Nothing here talks to Postgres/TimescaleDB — dedup is tracked entirely in
  Qdrant (QdrantStore.is_processed), matching docker-compose.yml's
  `depends_on: vector-db` for this service (no `db` dependency).
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Counter, Histogram
from qdrant_client import AsyncQdrantClient

from shared.article_extractor import extract_markdown, fetch_article_html
from shared.claim_extractor import extract_claims
from shared.db_manager import DatabaseManager
from shared.event_extractor import ExtractedEvent, extract_events
from shared.logging_config import configure_logging
from shared.metrics import start_metrics_server
from shared.rss_feeds import RSS_FEEDS
from shared.rss_reader import ArticleRef, fetch_feed_entries
from shared.vector_store import QdrantStore

configure_logging()
logger = logging.getLogger(__name__)

CRAWL_INTERVAL_MINUTES = 30
QDRANT_URL = "http://vector-db:6333"

# Port for this service's standalone Prometheus exposition endpoint. See
# docker-compose.yml / prometheus/prometheus.yml.
METRICS_PORT = int(os.getenv("METRICS_PORT", "9101"))

CYCLE_DURATION = Histogram("crawler_cycle_duration_seconds", "Duration of one full crawl cycle")
ARTICLE_PROCESSED_TOTAL = Counter(
    "crawler_article_processed_total",
    "Per-article processing outcomes",
    ["status"],
)
# M6+ supply-event features (docs/supply-event-features-design.md §4): counts
# the additive, non-fatal event-storage path separately from
# ARTICLE_PROCESSED_TOTAL, since one article can both succeed at claim
# storage and fail at event storage (or vice versa -- see `_store_events`).
EVENT_STORAGE_TOTAL = Counter(
    "crawler_event_storage_total",
    "Per-article event-storage outcomes (design §4: additive, non-fatal)",
    ["status"],
)


def _known_at(article: ArticleRef) -> datetime:
    """
    Design §1: an event's leak-safe availability key (`known_at`) is the
    article's publish time, falling back to crawl time when `published` is
    null or unparseable. Assigned HERE, by the crawler -- never by
    `shared/event_extractor.py`'s model, which must not be trusted to date
    events (design §1/§3).

    `ArticleRef.published` is whatever raw string `feedparser` handed back
    (shared/rss_reader.py) -- typically an RFC 822 date
    (`email.utils.parsedate_to_datetime` is the same format Python's own
    `email` module parses feed dates with), but feeds are not obligated to
    conform, so any parse failure falls back to crawl time rather than
    raising. A naive (no explicit offset) parse result is treated as UTC --
    conservative and consistent with every other timestamp this module
    produces.
    """
    if article.published:
        try:
            parsed = parsedate_to_datetime(article.published)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    return datetime.now(UTC)


def _event_id(url: str, index: int) -> str:
    """
    Deterministic per-event ID (design §2), mirroring
    `shared/vector_store.py:_claim_point_id`'s `uuid5(NAMESPACE_URL, ...)`
    construction exactly -- re-crawling the same article regenerates the
    same IDs, so `DatabaseManager.save_market_event`'s
    `ON CONFLICT (event_id) DO NOTHING` makes storage idempotent rather than
    duplicating.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#event:{index}"))


async def _store_events(article: ArticleRef, text: str, db: DatabaseManager) -> int:
    """
    Extracts supply/demand/regime events from `text` (design §4: "the same
    text" already used for claim extraction) and stores them in Postgres
    (init-db/08-market-events.sql). Returns the number of events stored.

    Deliberately narrow: every exception this raises is caught by
    `process_article`'s caller (never here) -- see that function's own
    handling for why event-path failures must never affect claim storage.
    """
    events: list[ExtractedEvent] | None = await extract_events(text, article)
    if not events:
        return 0

    known_at = _known_at(article)
    extracted_at = datetime.now(UTC)
    stored = 0
    for i, event in enumerate(events):
        db.save_market_event(
            event_id=_event_id(article.url, i),
            event_type=event.event_type,
            market=event.market,
            zone=event.zone,
            direction=event.direction,
            magnitude_mw=event.magnitude_mw,
            effective_from=event.effective_from,
            known_at=known_at,
            confidence=event.confidence,
            source_url=article.url,
            source_title=article.title,
            source_tier=article.feed_tier,
            raw_excerpt=event.raw_excerpt,
            extracted_at=extracted_at,
        )
        stored += 1

    logger.info("Extracted and stored %d event(s) for %s", stored, article.url)
    return stored


async def process_article(
    article, http_client: httpx.AsyncClient, store: QdrantStore, db: DatabaseManager | None = None
) -> int:
    """
    Fetches, extracts, and stores one article. Any failure is logged and
    swallowed. Returns the number of claims extracted and stored for this
    article (`0` for every skip/raw-stored/failure path) -- used by
    `run_crawl_cycle` to build its `claims_extracted` on-demand-run summary.

    `db` (M6+, design §4) is optional and additive: when provided, the same
    article `text` is also passed through `shared/event_extractor.py` and
    stored in Postgres (init-db/08-market-events.sql), *after* the existing
    claim path above runs, over both the "stored raw" and "stored claims"
    branches -- claim extraction success/failure is independent of whether
    events are found. Any failure on this path (extraction or storage) is
    logged and swallowed here, exactly like the article-fetch/markdown-
    extraction failures above, and never affects the `saved` claims count
    this function returns or the calling crawl cycle. `db=None` (no
    `DATABASE_URL` configured, see `run_crawl_cycle`) skips the event path
    entirely -- claim storage is unaffected either way.
    """
    if await store.is_processed(article.url):
        logger.debug("Already processed %s; skipping", article.url)
        ARTICLE_PROCESSED_TOTAL.labels(status="skipped_already_processed").inc()
        return 0

    # `self_contained` feeds (shared/rss_feeds.py, e.g. ENTSO-E news) deliver
    # the body inline on the `ArticleRef`; `article.url` is a synthetic
    # identity anchor, not a fetchable page, so extract from `content`
    # directly instead of making an HTTP request. All other feeds fetch and
    # extract their per-article page as before.
    if article.content is not None:
        html = article.content
    else:
        html = await fetch_article_html(article.url, http_client)
        if html is None:
            ARTICLE_PROCESSED_TOTAL.labels(status="skipped_fetch_failed").inc()
            return 0

    text = extract_markdown(html, url=article.url)
    if text is None:
        ARTICLE_PROCESSED_TOTAL.labels(status="skipped_extract_failed").inc()
        return 0

    extraction = await extract_claims(text, article)
    if extraction is None:
        # No ANTHROPIC_API_KEY (or the call/parse failed outright): store the
        # raw article text without derived claims rather than losing it.
        await store.upsert_raw_article(article, text)
        logger.info("Stored raw article (no claim extraction) for %s", article.url)
        ARTICLE_PROCESSED_TOTAL.labels(status="stored_raw").inc()
        saved = 0
    else:
        saved = await store.upsert_claims(article, extraction.claims)
        logger.info("Extracted and stored %d claim(s) for %s", saved, article.url)
        ARTICLE_PROCESSED_TOTAL.labels(status="stored_claims").inc()

    # M6+ event path (design §4): additive and non-fatal by construction --
    # this except-block is the entire contract. Whatever goes wrong here
    # (a bad LLM response, a DB error, anything) is logged and swallowed;
    # `saved` (this function's return value, driving claim-storage bookkeeping
    # in `run_crawl_cycle`) is never touched by this block.
    if db is not None:
        try:
            events_stored = await _store_events(article, text, db)
            EVENT_STORAGE_TOTAL.labels(
                status="stored" if events_stored else "no_events_found"
            ).inc()
        except Exception:
            logger.exception(
                "Event extraction/storage failed for %s (claims unaffected)", article.url
            )
            EVENT_STORAGE_TOTAL.labels(status="failed").inc()

    return saved


def _get_db() -> DatabaseManager | None:
    """
    Best-effort `DatabaseManager` construction for the M6+ event-storage
    path (design §4: "wire it in following the existing dependency
    pattern" -- `services/ingestor/main.py`/`services/orchestrator/main.py`
    both construct one fresh per cycle). Unlike those services, the crawler
    has run without a Postgres dependency since M3, and `DatabaseManager()`
    raises `ValueError` when `DATABASE_URL` isn't set -- letting that raise
    uncaught here would turn "no DB configured" into "no crawling happens
    at all", which is exactly the kind of event-path failure design §4
    requires to be non-fatal. Returns `None` (event storage skipped for
    this cycle, claims unaffected) rather than raising.
    """
    try:
        return DatabaseManager()
    except Exception:
        logger.warning(
            "DatabaseManager unavailable (DATABASE_URL not set?) -- event storage disabled "
            "for this crawl cycle; claim extraction/storage is unaffected."
        )
        return None


async def run_crawl_cycle() -> dict:
    """
    Polls every feed declared in shared/rss_feeds.py and processes every new
    entry found. A failure processing one article, or fetching one feed,
    doesn't stop the rest of the cycle (same convention as
    services/ingestor/main.py's uptime-preserving cycle).

    Returns a small `{"articles_processed": int, "claims_extracted": int}`
    summary -- used by the on-demand `POST /crawler/run-now` route
    (`services/api/main.py`) and its dashboard button to report back what
    one cycle actually did; the automatic scheduler
    (`scheduled_crawl_cycle` below) ignores the return value, same as
    before this was added. `claims_extracted` only counts articles that
    guard against a mocked `process_article` (which defaults to returning a
    truthy `MagicMock`, not an `int`) miscounting in tests -- see
    `process_article`'s own docstring.

    M6+ (design §4): also constructs a `DatabaseManager` (`_get_db`, `None`
    if unavailable) and passes it to every `process_article` call, so the
    additive event-storage path can run alongside the existing Qdrant claim
    path. Closed in the same `finally` this function already uses to close
    the Qdrant client.
    """
    qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
    store = QdrantStore(qdrant_client)
    db = _get_db()
    cycle_start = time.monotonic()
    articles_processed = 0
    claims_extracted = 0

    try:
        await store.ensure_collection()

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
            try:
                logger.info("Starting crawl cycle for %d feed(s)...", len(RSS_FEEDS))
                for feed in RSS_FEEDS:
                    articles = await fetch_feed_entries(feed, http_client)
                    for article in articles:
                        articles_processed += 1
                        try:
                            result = await process_article(article, http_client, store, db)
                            if isinstance(result, int):
                                claims_extracted += result
                        except Exception:
                            logger.exception("Failed to process article %s", article.url)
                            ARTICLE_PROCESSED_TOTAL.labels(status="failed").inc()
            finally:
                await qdrant_client.close()
    finally:
        if db is not None:
            db.close()
        CYCLE_DURATION.observe(time.monotonic() - cycle_start)

    return {"articles_processed": articles_processed, "claims_extracted": claims_extracted}


def _auto_run_enabled() -> bool:
    """
    Reads `AUTO_RUN_ENABLED` fresh on every call (same "read the env var
    live, don't freeze it at import time" convention as
    `services/api/main.py`'s `require_api_key`). Defaults to `False`:
    automatic scheduled crawl cycles are opt-in, not opt-out, since every new
    article this cycle finds costs one Claude Haiku call
    (`shared/claim_extractor.py`) -- see `.env.example` / `DEPLOYMENT.md`.
    Set `AUTO_RUN_ENABLED=true` to restore the fully-automatic behavior this
    service had before this gate existed.
    """
    return os.getenv("AUTO_RUN_ENABLED", "false").strip().lower() == "true"


async def scheduled_crawl_cycle() -> None:
    """
    The APScheduler job entrypoint (see `main` below) -- wraps
    `run_crawl_cycle` behind the `AUTO_RUN_ENABLED` gate so the scheduler
    itself, this service's process, and its `/metrics` exposition endpoint
    all stay up and healthy regardless, while no Claude Haiku calls happen
    automatically unless explicitly opted in. Firing one real cycle on
    demand, independent of this gate, is always available via
    `POST /crawler/run-now` on the API service (`services/api/main.py`),
    which calls `run_crawl_cycle` directly.
    """
    if not _auto_run_enabled():
        logger.info(
            "AUTO_RUN_ENABLED is unset/false; skipping this scheduled crawl cycle "
            "(no Claude Haiku calls made). POST /crawler/run-now on the API service to "
            "run one cycle on demand, or set AUTO_RUN_ENABLED=true for automatic "
            "scheduled cycles."
        )
        return
    await run_crawl_cycle()


async def main():
    start_metrics_server(METRICS_PORT)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_crawl_cycle,
        "interval",
        minutes=CRAWL_INTERVAL_MINUTES,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
