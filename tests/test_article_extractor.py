import httpx
import pytest
import respx

from shared.article_extractor import extract_markdown, fetch_article_html

ARTICLE_HTML = """
<html>
<head><title>Analyst: wind shortfall drove DK1 mFRR prices up</title></head>
<body>
<article>
<h1>Analyst: wind shortfall drove DK1 mFRR prices up</h1>
<p>Balancing energy prices in DK1 spiked to 4,850 DKK/MWh on Tuesday evening,
according to Energinet's published data, as offshore wind output fell sharply
below forecast during the evening peak.</p>
<p>"We saw a near-complete collapse in offshore wind generation combined with
the Karlshamn HVDC link being unavailable for maintenance," said energy
analyst Jane Doe at EnergyWatch. "That combination explains most of the
price spike we observed."</p>
<p>Energinet has not yet commented on whether the figures will be revised.</p>
</article>
</body>
</html>
"""

# Simulates a JS-rendered SPA shell with no server-rendered article body —
# what trafilatura sees when hitting a page that needs a real browser.
JS_SHELL_HTML = """
<html><head><title>App</title></head>
<body><div id="root"></div><script src="/app.js"></script></body></html>
"""


@pytest.fixture
def client():
    return httpx.AsyncClient()


@respx.mock
async def test_fetch_article_html_returns_body_text(client):
    url = "https://feeds.example.test/article-1"
    respx.get(url).mock(return_value=httpx.Response(200, text=ARTICLE_HTML))

    html = await fetch_article_html(url, client)

    assert html is not None
    assert "wind shortfall" in html


@respx.mock
async def test_fetch_article_html_returns_none_on_http_error(client):
    url = "https://feeds.example.test/gone"
    respx.get(url).mock(return_value=httpx.Response(404))

    html = await fetch_article_html(url, client)

    assert html is None


def test_extract_markdown_returns_clean_text_from_fixed_html():
    text = extract_markdown(ARTICLE_HTML, url="https://feeds.example.test/article-1")

    assert text is not None
    assert "wind shortfall" in text.lower()
    assert "Jane Doe" in text
    # trafilatura should have dropped HTML tags entirely.
    assert "<p>" not in text
    assert "<article>" not in text


def test_extract_markdown_returns_none_for_js_shell_page():
    text = extract_markdown(JS_SHELL_HTML, url="https://feeds.example.test/spa")

    assert text is None


def test_extract_markdown_returns_none_for_empty_html():
    assert extract_markdown("", url="https://feeds.example.test/empty") is None
