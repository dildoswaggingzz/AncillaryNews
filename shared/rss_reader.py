"""
RSS polling for the M3 Insight Crawler (README §3B).

Fetching is done with our own `httpx` client (not `feedparser`'s built-in
URL-fetching) so tests can mock the HTTP layer with `respx` the same way the
rest of this repo does (see shared/base_ingestor.py) — `feedparser.parse()`
is then only handed already-downloaded bytes, which is a pure/offline
operation and trivial to unit test.
"""

import hashlib
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
    # Set only for `self_contained` feeds (shared/rss_feeds.py): the item's
    # full body as it arrived inline in the RSS `<description>` (HTML). When
    # present, the crawler extracts from this directly instead of fetching
    # `url` over HTTP — which for these feeds is a synthetic identity anchor,
    # not a fetchable page. `None` for ordinary feeds (the crawler fetches
    # `url` as before).
    content: str | None = None


def _synthetic_url(feed: FeedConfig, entry) -> str:
    """
    A stable, unique identity URL for a `self_contained` feed item that
    publishes no `<link>`/`<guid>` (the ENTSO-E news feed — see
    shared/rss_feeds.py). This URL is the item's identity everywhere the
    pipeline keys on `ArticleRef.url` (Qdrant dedup via
    `QdrantStore.is_processed`, event IDs via `services/crawler/main.py`), so
    it must be deterministic: the same announcement must hash to the same URL
    on every crawl, or dedup breaks and each cycle re-extracts it.

    Prefers the entry's own `id`/`guid` when one exists; otherwise derives a
    digest from the item's title + publish date. Anchored under the ENTSO-E
    news base so the value is recognisable in stored payloads, even though it
    does not resolve to a per-item page (the feed offers none).
    """
    identity = entry.get("id") or f"{entry.get('title', '')}|{entry.get('published', '')}"
    digest = hashlib.sha256(f"{feed.url}|{identity}".encode()).hexdigest()[:16]
    return f"https://transparency.entsoe.eu/news#{digest}"


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
        content = None
        if feed.self_contained:
            # These items carry their body inline and publish no link/guid;
            # skip only when there's no body to work with, and give the item a
            # deterministic synthetic identity (see `_synthetic_url`).
            content = entry.get("description") or entry.get("summary")
            if not content:
                continue
            link = link or _synthetic_url(feed, entry)
        elif not link:
            # Ordinary feeds need a real per-article URL to fetch; an item
            # without one is dead weight (see the EnergyWatch note in
            # shared/rss_feeds.py).
            continue
        articles.append(
            ArticleRef(
                url=link,
                title=entry.get("title", "").strip(),
                author=entry.get("author"),
                published=entry.get("published"),
                feed_name=feed.name,
                feed_tier=feed.tier,
                content=content,
            )
        )

    logger.info("Fetched %d entr(y/ies) from feed %s", len(articles), feed.name)
    return articles
