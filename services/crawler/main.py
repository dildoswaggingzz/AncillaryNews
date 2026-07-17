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
from datetime import datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Counter, Histogram
from qdrant_client import AsyncQdrantClient

from shared.article_extractor import extract_markdown, fetch_article_html
from shared.claim_extractor import extract_claims
from shared.logging_config import configure_logging
from shared.metrics import start_metrics_server
from shared.rss_feeds import RSS_FEEDS
from shared.rss_reader import fetch_feed_entries
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


async def process_article(article, http_client: httpx.AsyncClient, store: QdrantStore) -> int:
    """
    Fetches, extracts, and stores one article. Any failure is logged and
    swallowed. Returns the number of claims extracted and stored for this
    article (`0` for every skip/raw-stored/failure path) -- used by
    `run_crawl_cycle` to build its `claims_extracted` on-demand-run summary.
    """
    if await store.is_processed(article.url):
        logger.debug("Already processed %s; skipping", article.url)
        ARTICLE_PROCESSED_TOTAL.labels(status="skipped_already_processed").inc()
        return 0

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
        return 0

    saved = await store.upsert_claims(article, extraction.claims)
    logger.info("Extracted and stored %d claim(s) for %s", saved, article.url)
    ARTICLE_PROCESSED_TOTAL.labels(status="stored_claims").inc()
    return saved


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
    """
    qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
    store = QdrantStore(qdrant_client)
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
                            result = await process_article(article, http_client, store)
                            if isinstance(result, int):
                                claims_extracted += result
                        except Exception:
                            logger.exception("Failed to process article %s", article.url)
                            ARTICLE_PROCESSED_TOTAL.labels(status="failed").inc()
            finally:
                await qdrant_client.close()
    finally:
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
