"""
LinkedIn "Embed this post" fetching for the manual-article paste-in tool
(services/api/main.py's `/manual-articles`).

LinkedIn has no public API for an arbitrary profile's posts, and scraping
its main authenticated site violates their ToS (see the module docstring
next to `ManualArticleSubmission` in services/api/main.py) -- both
correctly ruled out for automated ingestion. This module instead targets a
narrower, explicitly-public surface: the same embed page LinkedIn itself
serves for their official "Embed this post" `<iframe>` feature (e.g.
`https://www.linkedin.com/embed/feed/update/urn:li:share:<id>`). That page
is server-rendered with no JS execution or login required -- content
LinkedIn deliberately publishes for external embedding, not their
authenticated feed.

Confirmed live (2026-07) via `curl -A "Mozilla/5.0" .../embed/feed/update/urn:li:share:<id>`:
the response is real, multi-paragraph post text in
`<meta name="description" content="...">`, the canonical (stable, dedupable)
post URL in `<link rel="canonical" href="...">`, and the author's name as
the trailing "| Author Name" segment of `<title>`/`<meta property="og:title">`
-- there's no dedicated `<meta name="author">` tag on this page, so that's
the best available signal; the caller (services/api/main.py) still prefers
whatever the human typed into the form's optional author field over this.

Three ways a human might paste LinkedIn content into the manual-article
form, all detected here and resolved to the same embed URL to fetch:

1. The raw `<iframe ...>` HTML LinkedIn's "Embed this post" button copies
   to the clipboard -- pull the `src` attribute out of it.
2. A bare embed URL (that same `src` value, pasted directly).
3. A regular, human-facing LinkedIn post URL --
   `.../posts/someuser_..-activity-<id>-..` or
   `.../feed/update/urn:li:activity:<id>` -- extract the activity ID and
   construct the equivalent embed URL ourselves.

Anything that doesn't match one of these three shapes is left alone --
`resolve_linkedin_content` returns None and the caller falls back to
treating the submitted content as plain pasted text, exactly like before
this module existed. The same graceful-degradation applies to any fetch
failure (network error, private/deleted/embed-disabled post, unexpected
HTML shape): logged as a warning, never raised, same precedent as a
missing `ANTHROPIC_API_KEY`/`SLACK_WEBHOOK_URL` elsewhere in this codebase.
"""

import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# A generic desktop UA -- LinkedIn's embed endpoint returned nothing useful
# without one in live testing (default httpx/curl UAs got a different,
# JS-shell response), but doesn't require anything more specific.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FETCH_TIMEOUT_SECONDS = 10.0

_IFRAME_SRC_RE = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
_EMBED_URL_RE = re.compile(
    r'https?://(?:www\.)?linkedin\.com/embed/feed/update/urn:li:(?:share|activity):\d+[^\s"\'<>]*',
    re.IGNORECASE,
)
_POST_URL_ACTIVITY_ID_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/"
    r"(?:posts/[^\s\"'<>]*?-activity-(\d+)|feed/update/urn:li:activity:(\d+))",
    re.IGNORECASE,
)

_META_DESCRIPTION_RE = re.compile(
    r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']\s*/?>',
    re.IGNORECASE | re.DOTALL,
)
_CANONICAL_LINK_RE = re.compile(
    r'<link\s+rel=["\']canonical["\']\s+href=["\'](.*?)["\']\s*/?>', re.IGNORECASE
)
_OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']\s*/?>',
    re.IGNORECASE | re.DOTALL,
)
_TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class LinkedInEmbedContent:
    """What `resolve_linkedin_content` pulled out of a LinkedIn embed page."""

    text: str
    canonical_url: str
    author: str | None


def _unescape_html_entities(value: str) -> str:
    """LinkedIn's embed page HTML-escapes meta/link attribute values (`&quot;`,
    `&#39;`, `&amp;`, etc); undo that for the text we're about to store/send
    to Claude. Deliberately minimal (no external dep) -- just the entities
    actually observed in live testing."""
    return (
        value.replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def detect_linkedin_embed_url(raw_input: str) -> str | None:
    """
    Looks at pasted-in `raw_input` and, if it's recognizably LinkedIn embed
    iframe HTML, a bare embed URL, or a regular LinkedIn post URL, returns
    the embed URL to fetch. Returns None for anything else (plain text),
    which the caller treats as "no LinkedIn content detected."
    """
    if not raw_input or not raw_input.strip():
        return None

    iframe_match = _IFRAME_SRC_RE.search(raw_input)
    if iframe_match:
        src = _unescape_html_entities(iframe_match.group(1))
        if "linkedin.com/embed/" in src:
            return src

    embed_match = _EMBED_URL_RE.search(raw_input)
    if embed_match:
        return embed_match.group(0)

    activity_match = _POST_URL_ACTIVITY_ID_RE.search(raw_input)
    if activity_match:
        activity_id = activity_match.group(1) or activity_match.group(2)
        return (
            f"https://www.linkedin.com/embed/feed/update/urn:li:activity:{activity_id}?collapsed=1"
        )

    return None


def _extract_author_from_title(title: str | None) -> str | None:
    """LinkedIn's embed page has no dedicated author meta tag; the author's
    name is the trailing "| Author Name" segment of `<title>`/`og:title`
    (e.g. "The aFRR capacity market...| Andreas Barnekov Thingvad")."""
    if not title or "|" not in title:
        return None
    author = title.rsplit("|", 1)[-1].strip()
    return _unescape_html_entities(author) or None


def parse_embed_html(html: str) -> LinkedInEmbedContent | None:
    """
    Pulls post text, canonical URL, and (best-effort) author out of a
    LinkedIn embed page's raw HTML. Returns None if the two required pieces
    (description text, canonical URL) aren't both present -- an unexpected
    HTML shape, most likely a private/deleted/embed-disabled post.
    """
    description_match = _META_DESCRIPTION_RE.search(html)
    canonical_match = _CANONICAL_LINK_RE.search(html)
    if not description_match or not canonical_match:
        return None

    text = _unescape_html_entities(description_match.group(1)).strip()
    canonical_url = _unescape_html_entities(canonical_match.group(1)).strip()
    if not text or not canonical_url:
        return None

    title_match = _OG_TITLE_RE.search(html) or _TITLE_TAG_RE.search(html)
    author = _extract_author_from_title(title_match.group(1) if title_match else None)

    return LinkedInEmbedContent(text=text, canonical_url=canonical_url, author=author)


async def resolve_linkedin_content(
    raw_input: str, client: httpx.AsyncClient | None = None
) -> LinkedInEmbedContent | None:
    """
    End-to-end: detect whether `raw_input` is LinkedIn embed iframe HTML, a
    bare embed URL, or a regular post URL; if so, fetch the embed page and
    parse it. Returns None -- never raises -- for plain text, any fetch
    failure, or any unparseable response, so the caller can always fall
    back to treating `raw_input` as raw pasted text.
    """
    embed_url = detect_linkedin_embed_url(raw_input)
    if embed_url is None:
        return None

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS)
    try:
        response = await client.get(
            embed_url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )
        response.raise_for_status()
        html = response.text
    except Exception:
        logger.warning(
            "Failed to fetch LinkedIn embed page %s; falling back to raw pasted text",
            embed_url,
            exc_info=True,
        )
        return None
    finally:
        if owns_client:
            await client.aclose()

    content = parse_embed_html(html)
    if content is None:
        logger.warning(
            "LinkedIn embed page %s didn't have the expected description/canonical tags "
            "(private/deleted/embed-disabled post?); falling back to raw pasted text",
            embed_url,
        )
    return content
