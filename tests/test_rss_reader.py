import httpx
import pytest
import respx

from shared.rss_feeds import FeedConfig
from shared.rss_reader import fetch_feed_entries

FEED = FeedConfig(name="Test Feed", url="https://feeds.example.test/feed/", tier="tier1")

VALID_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Wind shortfall lifts DK1 balancing prices</title>
      <link>https://feeds.example.test/article-1</link>
      <author>Jane Analyst</author>
      <pubDate>Fri, 03 Jul 2026 07:11:50 +0000</pubDate>
    </item>
    <item>
      <title>NBM publishes MARI accession update</title>
      <link>https://feeds.example.test/article-2</link>
      <pubDate>Sat, 04 Jul 2026 09:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

# What EnergyWatch's catalogued "RSS" URL actually returns in practice: an
# HTML document, not XML. feedparser should surface this as zero entries
# rather than raising.
NOT_ACTUALLY_RSS_HTML = (
    b"<!DOCTYPE html><html><head><title>Not RSS</title></head><body></body></html>"
)


@pytest.fixture
def client():
    return httpx.AsyncClient()


@respx.mock
async def test_fetch_feed_entries_parses_valid_rss(client):
    respx.get(FEED.url).mock(return_value=httpx.Response(200, content=VALID_RSS))

    articles = await fetch_feed_entries(FEED, client)

    assert len(articles) == 2
    first = articles[0]
    assert first.url == "https://feeds.example.test/article-1"
    assert first.title == "Wind shortfall lifts DK1 balancing prices"
    assert first.author == "Jane Analyst"
    assert first.feed_name == "Test Feed"
    assert first.feed_tier == "tier1"


@respx.mock
async def test_fetch_feed_entries_handles_dead_html_feed_gracefully(client):
    respx.get(FEED.url).mock(return_value=httpx.Response(200, content=NOT_ACTUALLY_RSS_HTML))

    articles = await fetch_feed_entries(FEED, client)

    assert articles == []


@respx.mock
async def test_fetch_feed_entries_handles_http_error_gracefully(client):
    respx.get(FEED.url).mock(return_value=httpx.Response(500))

    articles = await fetch_feed_entries(FEED, client)

    assert articles == []


@respx.mock
async def test_fetch_feed_entries_skips_entries_without_link(client):
    rss_missing_link = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>No link here</title></item>
    </channel></rss>
    """
    respx.get(FEED.url).mock(return_value=httpx.Response(200, content=rss_missing_link))

    articles = await fetch_feed_entries(FEED, client)

    assert articles == []


# The ENTSO-E news feed shape: full body inline in <description>, no per-item
# <link> or <guid> (see shared/rss_feeds.py).
SELF_CONTAINED_FEED = FeedConfig(
    name="ENTSO-E Transparency Platform",
    url="https://external-api.tp.entsoe.eu/news/feed",
    tier="tier1",
    self_contained=True,
)

SELF_CONTAINED_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Transparency Platform News</title>
    <item>
      <title>Publication issue on Wind offshore generation for Belgium</title>
      <description>&lt;p&gt;Dear users, Elia has noticed deltas.&lt;/p&gt;</description>
      <pubDate>Mon, 20 Jul 2026 14:17:09 GMT</pubDate>
    </item>
    <item>
      <title>Scheduled maintenance on the Transparency Platform</title>
      <description>&lt;p&gt;A maintenance window is planned this weekend.&lt;/p&gt;</description>
      <pubDate>Sun, 19 Jul 2026 08:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


@respx.mock
async def test_self_contained_feed_keeps_linkless_items_with_inline_content(client):
    respx.get(SELF_CONTAINED_FEED.url).mock(
        return_value=httpx.Response(200, content=SELF_CONTAINED_RSS)
    )

    articles = await fetch_feed_entries(SELF_CONTAINED_FEED, client)

    assert len(articles) == 2
    first = articles[0]
    assert first.title == "Publication issue on Wind offshore generation for Belgium"
    assert first.content is not None
    assert "Elia has noticed deltas" in first.content
    assert first.feed_tier == "tier1"
    # Linkless items get a synthetic, unique identity URL.
    assert first.url.startswith("https://transparency.entsoe.eu/news#")
    assert articles[0].url != articles[1].url


@respx.mock
async def test_self_contained_feed_synthetic_url_is_stable_across_crawls(client):
    respx.get(SELF_CONTAINED_FEED.url).mock(
        return_value=httpx.Response(200, content=SELF_CONTAINED_RSS)
    )

    first_crawl = await fetch_feed_entries(SELF_CONTAINED_FEED, client)
    second_crawl = await fetch_feed_entries(SELF_CONTAINED_FEED, client)

    # Same announcement must hash to the same URL every cycle, or Qdrant dedup
    # (QdrantStore.is_processed) re-extracts it endlessly.
    assert [a.url for a in first_crawl] == [a.url for a in second_crawl]


@respx.mock
async def test_self_contained_feed_skips_items_with_no_body(client):
    rss_no_body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item><title>Headline only, no description</title></item>
    </channel></rss>
    """
    respx.get(SELF_CONTAINED_FEED.url).mock(
        return_value=httpx.Response(200, content=rss_no_body)
    )

    articles = await fetch_feed_entries(SELF_CONTAINED_FEED, client)

    assert articles == []
