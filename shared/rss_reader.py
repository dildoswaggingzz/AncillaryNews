"""
RSS polling for the M3 Insight Crawler (README §3B).

Fetching is done with our own `httpx` client (not `feedparser`'s built-in
URL-fetching) so tests can mock the HTTP layer with `respx` the same way the
rest of this repo does (see shared/base_ingestor.py) — `feedparser.parse()`
is then only handed already-downloaded bytes, which is a pure/offline
operation and trivial to unit test.
"""

import logging
from dataclasses import dataclass

import feedparser
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from shared.rss_feeds import FeedConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArticleRef:
    """One RSS entry, resolved down to what the rest of the pipeline needs."""

    url: str
    title: str
    author: str | None
    published: str | None
    feed_name: str
    feed_tier: str


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
)
async def _fetch_feed_bytes(url: str, client: httpx.AsyncClient) -> bytes:
    response = await client.get(url)
    response.raise_for_status()
    return response.content


async def fetch_feed_entries(feed: FeedConfig, client: httpx.AsyncClient) -> list[ArticleRef]:
    """
    Downloads and parses one RSS feed, returning every entry as an
    `ArticleRef`.

    A feed that's unreachable (HTTP error) or that parses to zero entries
    (dead/malformed, e.g. a feed URL that actually serves HTML — see
    shared/rss_feeds.py's EnergyWatch note) is logged and results in an
    empty list rather than raising, so one bad feed doesn't stop the crawl
    cycle from polling the rest (same "don't let one failure take down the
    cycle" convention as services/ingestor/main.py).
    """
    try:
        raw = await _fetch_feed_bytes(feed.url, client)
    except Exception:
        logger.exception("Failed to fetch RSS feed %s (%s)", feed.name, feed.url)
        return []

    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        logger.warning(
            "Feed %s (%s) parsed with no entries (bozo=%s, %s) — likely dead/non-RSS content",
            feed.name,
            feed.url,
            parsed.bozo,
            getattr(parsed, "bozo_exception", "unknown parse error"),
        )
        return []

    articles = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            continue
        articles.append(
            ArticleRef(
                url=link,
                title=entry.get("title", "").strip(),
                author=entry.get("author"),
                published=entry.get("published"),
                feed_name=feed.name,
                feed_tier=feed.tier,
            )
        )

    logger.info("Fetched %d entr(y/ies) from feed %s", len(articles), feed.name)
    return articles
