"""
Page-to-Markdown extraction for the M3 Insight Crawler (README §3B).

Uses `httpx` (consistent with the rest of the repo, and easy to mock with
`respx` in tests) to fetch the raw page, then hands the HTML to
`trafilatura` for content extraction — no JS rendering. Playwright/browser
automation for JS-heavy sites is explicitly out of scope for this pass (see
module docstring in services/crawler/main.py); if a future feed's articles
only extract as empty/near-empty text here, that's the signal a JS-rendering
fetcher is actually needed for that source.
"""

import logging

import httpx
import trafilatura
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Below this length, extracted "content" is almost always a nav/cookie-banner
# fragment rather than a real article body — most likely a JS-rendered page
# trafilatura couldn't get anything useful out of statically.
MIN_EXTRACTED_CHARS = 200


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
)
async def fetch_article_html(url: str, client: httpx.AsyncClient) -> str | None:
    """Fetches the raw page HTML for `url`, or None if the fetch ultimately fails."""
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.text
    except Exception:
        logger.exception("Failed to fetch article page %s", url)
        return None


def extract_markdown(html: str, url: str | None = None) -> str | None:
    """
    Converts raw article HTML to clean Markdown via trafilatura.

    Returns None if trafilatura can't find a plausible article body, or if
    what it finds is implausibly short (see MIN_EXTRACTED_CHARS) — most
    likely a JS-rendered page with no server-rendered content to extract.
    """
    text = trafilatura.extract(html, url=url, output_format="markdown", with_metadata=False)
    if not text or len(text) < MIN_EXTRACTED_CHARS:
        logger.warning(
            "trafilatura extracted little/no content for %s (len=%d) — may need JS rendering",
            url,
            len(text) if text else 0,
        )
        return None
    return text
