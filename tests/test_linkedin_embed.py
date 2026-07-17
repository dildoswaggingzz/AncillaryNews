import httpx
import respx

from shared.linkedin_embed import (
    detect_linkedin_embed_url,
    parse_embed_html,
    resolve_linkedin_content,
)

# Shaped like the real response observed live from
# https://www.linkedin.com/embed/feed/update/urn:li:share:7479918035902959616?collapsed=1
# (curl -A "Mozilla/5.0" ..., 2026-07): a `<meta name="description">` with the
# real post text, a `<link rel="canonical">` with the stable post URL, and no
# dedicated author meta tag -- the author's name is the trailing "| Name"
# segment of `<title>`/`og:title` instead.
_TITLE = (
    "The aFRR capacity market (CM) and energy activation market&hellip; | Andreas Barnekov Thingvad"
)
_DESCRIPTION = (
    "The aFRR capacity market (CM) and energy activation market (EAM) prices in DK2 are "
    "set to drop significantly from July 13th to August 2nd 2026 due to a sharp decline "
    "in aFRR demand."
)
_CANONICAL_URL = "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"

EMBED_HTML = f"""
<html>
<head>
<title>{_TITLE}</title>
<meta property="og:title" content="{_TITLE}">
<meta name="description" content="{_DESCRIPTION}">
<link rel="canonical" href="{_CANONICAL_URL}">
</head>
<body></body>
</html>
"""

EMBED_URL = (
    "https://www.linkedin.com/embed/feed/update/urn:li:share:7479918035902959616?collapsed=1"
)
IFRAME_HTML = (
    f'<iframe src="{EMBED_URL}" height="647" width="504" '
    'frameborder="0" allowfullscreen="" title="Embedded post"></iframe>'
)
POST_URL = (
    "https://www.linkedin.com/posts/janedoe_dk1-afrr-pricing-activity-7479918037115047936-ab3d"
)
FEED_UPDATE_URL = "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"


# --- detection ---------------------------------------------------------


def test_detect_linkedin_embed_url_from_iframe_html():
    assert detect_linkedin_embed_url(IFRAME_HTML) == EMBED_URL


def test_detect_linkedin_embed_url_from_bare_embed_url():
    assert detect_linkedin_embed_url(EMBED_URL) == EMBED_URL


def test_detect_linkedin_embed_url_from_regular_post_url():
    result = detect_linkedin_embed_url(POST_URL)
    assert result == (
        "https://www.linkedin.com/embed/feed/update/urn:li:activity:7479918037115047936?collapsed=1"
    )


def test_detect_linkedin_embed_url_from_feed_update_url():
    result = detect_linkedin_embed_url(FEED_UPDATE_URL)
    assert result == (
        "https://www.linkedin.com/embed/feed/update/urn:li:activity:7479918037115047936?collapsed=1"
    )


def test_detect_linkedin_embed_url_returns_none_for_plain_text():
    text = "DK1 aFRR capacity prices hit a new high this week, driven by reduced wind."
    assert detect_linkedin_embed_url(text) is None


def test_detect_linkedin_embed_url_returns_none_for_empty_string():
    assert detect_linkedin_embed_url("") is None
    assert detect_linkedin_embed_url("   ") is None


# --- HTML parsing --------------------------------------------------------


def test_parse_embed_html_extracts_text_canonical_url_and_author():
    content = parse_embed_html(EMBED_HTML)

    assert content is not None
    assert "aFRR capacity market" in content.text
    assert "DK2" in content.text
    assert content.canonical_url == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"
    )
    assert content.author == "Andreas Barnekov Thingvad"


def test_parse_embed_html_returns_none_when_description_missing():
    html = '<html><head><link rel="canonical" href="https://www.linkedin.com/feed/update/x"></head></html>'
    assert parse_embed_html(html) is None


def test_parse_embed_html_returns_none_when_canonical_missing():
    html = '<html><head><meta name="description" content="Some post text"></head></html>'
    assert parse_embed_html(html) is None


def test_parse_embed_html_author_none_when_title_has_no_pipe():
    html = (
        "<html><head><title>No pipe here</title>"
        '<meta name="description" content="Some post text">'
        '<link rel="canonical" href="https://www.linkedin.com/feed/update/x"></head></html>'
    )
    content = parse_embed_html(html)
    assert content is not None
    assert content.author is None


# --- end-to-end resolve (mocked HTTP) -------------------------------------


@respx.mock
async def test_resolve_linkedin_content_from_iframe_html():
    respx.get(EMBED_URL).mock(return_value=httpx.Response(200, text=EMBED_HTML))

    content = await resolve_linkedin_content(IFRAME_HTML)

    assert content is not None
    assert "aFRR capacity market" in content.text
    assert content.canonical_url == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"
    )
    assert content.author == "Andreas Barnekov Thingvad"


@respx.mock
async def test_resolve_linkedin_content_from_post_url():
    embed_url = (
        "https://www.linkedin.com/embed/feed/update/urn:li:activity:7479918037115047936?collapsed=1"
    )
    respx.get(embed_url).mock(return_value=httpx.Response(200, text=EMBED_HTML))

    content = await resolve_linkedin_content(POST_URL)

    assert content is not None
    assert content.canonical_url == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"
    )


async def test_resolve_linkedin_content_returns_none_for_plain_text():
    text = "DK1 aFRR capacity prices hit a new high this week."
    assert await resolve_linkedin_content(text) is None


@respx.mock
async def test_resolve_linkedin_content_returns_none_on_http_error():
    respx.get(EMBED_URL).mock(return_value=httpx.Response(404))

    content = await resolve_linkedin_content(IFRAME_HTML)

    assert content is None


@respx.mock
async def test_resolve_linkedin_content_returns_none_on_network_error():
    respx.get(EMBED_URL).mock(side_effect=httpx.ConnectError("boom"))

    content = await resolve_linkedin_content(IFRAME_HTML)

    assert content is None


@respx.mock
async def test_resolve_linkedin_content_returns_none_for_unparseable_response():
    # Private/deleted/embed-disabled post: 200 OK but no description/canonical tags.
    respx.get(EMBED_URL).mock(
        return_value=httpx.Response(200, text="<html><body>Sign in to view</body></html>")
    )

    content = await resolve_linkedin_content(IFRAME_HTML)

    assert content is None
