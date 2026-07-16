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
from datetime import datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from qdrant_client import AsyncQdrantClient

from shared.article_extractor import extract_markdown, fetch_article_html
from shared.claim_extractor import extract_claims
from shared.rss_feeds import RSS_FEEDS
from shared.rss_reader import fetch_feed_entries
from shared.vector_store import QdrantStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CRAWL_INTERVAL_MINUTES = 30
QDRANT_URL = "http://vector-db:6333"


async def process_article(article, http_client: httpx.AsyncClient, store: QdrantStore) -> None:
    """Fetches, extracts, and stores one article. Any failure is logged and swallowed."""
    if await store.is_processed(article.url):
        logger.debug("Already processed %s; skipping", article.url)
        return

    html = await fetch_article_html(article.url, http_client)
    if html is None:
        return

    text = extract_markdown(html, url=article.url)
    if text is None:
        return

    extraction = await extract_claims(text, article)
    if extraction is None:
        # No ANTHROPIC_API_KEY (or the call/parse failed outright): store the
        # raw article text without derived claims rather than losing it.
        await store.upsert_raw_article(article, text)
        logger.info("Stored raw article (no claim extraction) for %s", article.url)
        return

    saved = await store.upsert_claims(article, extraction.claims)
    logger.info("Extracted and stored %d claim(s) for %s", saved, article.url)


async def run_crawl_cycle() -> None:
    """
    Polls every feed declared in shared/rss_feeds.py and processes every new
    entry found. A failure processing one article, or fetching one feed,
    doesn't stop the rest of the cycle (same convention as
    services/ingestor/main.py's uptime-preserving cycle).
    """
    qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
    store = QdrantStore(qdrant_client)
    await store.ensure_collection()

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http_client:
        try:
            logger.info("Starting crawl cycle for %d feed(s)...", len(RSS_FEEDS))
            for feed in RSS_FEEDS:
                articles = await fetch_feed_entries(feed, http_client)
                for article in articles:
                    try:
                        await process_article(article, http_client, store)
                    except Exception:
                        logger.exception("Failed to process article %s", article.url)
        finally:
            await qdrant_client.close()


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_crawl_cycle,
        "interval",
        minutes=CRAWL_INTERVAL_MINUTES,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
