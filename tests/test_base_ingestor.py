import httpx
import pytest
import respx
from tenacity import RetryError, wait_none

from shared.base_ingestor import BaseIngestor

BASE_URL = "https://api.example.test"


@pytest.fixture
def ingestor():
    # Disable exponential backoff so retry tests run instantly.
    BaseIngestor.fetch_data.retry.wait = wait_none()
    ing = BaseIngestor(BASE_URL)
    yield ing


@respx.mock
async def test_fetch_data_returns_json(ingestor):
    route = respx.get(f"{BASE_URL}/dataset/mfrrRequest").mock(
        return_value=httpx.Response(200, json={"records": [{"PriceDKK": 100}]})
    )

    data = await ingestor.fetch_data("dataset/mfrrRequest", params={"limit": 100})

    assert data == {"records": [{"PriceDKK": 100}]}
    assert route.called
    assert route.calls.last.request.url.params["limit"] == "100"
    await ingestor.close()


@respx.mock
async def test_fetch_data_joins_url_with_stray_slashes(ingestor):
    route = respx.get(f"{BASE_URL}/some/endpoint").mock(return_value=httpx.Response(200, json={}))

    await ingestor.fetch_data("/some/endpoint")

    assert route.called
    await ingestor.close()


@respx.mock
async def test_fetch_data_retries_on_server_error(ingestor):
    route = respx.get(f"{BASE_URL}/flaky").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    data = await ingestor.fetch_data("flaky")

    assert data == {"ok": True}
    assert route.call_count == 3
    await ingestor.close()


@respx.mock
async def test_fetch_data_raises_after_exhausting_retries(ingestor):
    route = respx.get(f"{BASE_URL}/down").mock(return_value=httpx.Response(500))

    with pytest.raises(RetryError):
        await ingestor.fetch_data("down")

    assert route.call_count == 5
    await ingestor.close()
