import httpx
import pytest
import respx
from tenacity import RetryError, wait_none

from shared.base_ingestor import (
    DEFAULT_RATE_LIMIT_FALLBACK_SECONDS,
    BaseIngestor,
    TokenBucket,
    _wait_energinet_rate_limit,
    parse_retry_after_seconds,
)

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


# --- parse_retry_after_seconds: exact live message format + fallback --------
# docs/forecast-datasets-scope.md §1.4: live 429 body is
# `{"statusCode": 429, "message": "Rate limit is exceeded. Try again in 197
# seconds."}` -- these tests pin the parser to that exact wire format.


def _response(status_code: int = 429, json: dict | None = None, headers: dict | None = None):
    return httpx.Response(status_code, json=json or {}, headers=headers or {})


def test_parse_retry_after_seconds_from_the_exact_live_message_format():
    response = _response(
        json={"statusCode": 429, "message": "Rate limit is exceeded. Try again in 197 seconds."}
    )
    assert parse_retry_after_seconds(response) == 197.0


def test_parse_retry_after_seconds_prefers_retry_after_header_over_body():
    response = _response(
        json={"statusCode": 429, "message": "Rate limit is exceeded. Try again in 197 seconds."},
        headers={"Retry-After": "30"},
    )
    assert parse_retry_after_seconds(response) == 30.0


def test_parse_retry_after_seconds_falls_back_to_body_when_header_unparseable():
    response = _response(
        json={"statusCode": 429, "message": "Rate limit is exceeded. Try again in 197 seconds."},
        headers={"Retry-After": "not-a-number"},
    )
    assert parse_retry_after_seconds(response) == 197.0


def test_parse_retry_after_seconds_malformed_message_falls_back_to_default():
    response = _response(json={"statusCode": 429, "message": "Rate limit is exceeded."})
    assert parse_retry_after_seconds(response) == DEFAULT_RATE_LIMIT_FALLBACK_SECONDS


def test_parse_retry_after_seconds_non_json_body_falls_back_to_default():
    response = httpx.Response(429, text="not json at all")
    assert parse_retry_after_seconds(response) == DEFAULT_RATE_LIMIT_FALLBACK_SECONDS


def test_parse_retry_after_seconds_custom_fallback_is_honored():
    response = _response(json={"message": "nonsense"})
    assert parse_retry_after_seconds(response, fallback=12.5) == 12.5


# --- _wait_energinet_rate_limit: 429 honors advertised delay, others don't --


class _FakeOutcome:
    def __init__(self, exc):
        self._exc = exc

    def exception(self):
        return self._exc


class _FakeRetryState:
    def __init__(self, exc, attempt_number: int = 1):
        self.outcome = _FakeOutcome(exc)
        self.attempt_number = attempt_number


def test_wait_energinet_rate_limit_honors_429_advertised_delay():
    response = _response(
        json={"statusCode": 429, "message": "Rate limit is exceeded. Try again in 197 seconds."}
    )
    exc = httpx.HTTPStatusError("429", request=httpx.Request("GET", BASE_URL), response=response)
    retry_state = _FakeRetryState(exc)

    assert _wait_energinet_rate_limit(retry_state) == 197.0


def test_wait_energinet_rate_limit_uses_exponential_backoff_for_non_429():
    response = _response(status_code=500)
    exc = httpx.HTTPStatusError("500", request=httpx.Request("GET", BASE_URL), response=response)
    retry_state = _FakeRetryState(exc, attempt_number=1)

    # wait_exponential(multiplier=1, min=2, max=10) at attempt 1 -- unrelated
    # to any advertised-delay parsing, just the pre-existing behavior.
    delay = _wait_energinet_rate_limit(retry_state)
    assert 2.0 <= delay <= 10.0


async def test_fetch_data_honors_429_server_advertised_delay_not_exponential_backoff():
    """
    End-to-end: a 429 with the exact live message format is retried after
    exactly the advertised delay (not the exponential-backoff curve used for
    other errors). `.retry.wait` is left as the real production callable
    here (unlike the `ingestor` fixture's `wait_none()`) -- only `.retry.sleep`
    is swapped for a delay-capturing fake so the test doesn't actually pause.
    """
    BaseIngestor.fetch_data.retry.wait = _wait_energinet_rate_limit
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    BaseIngestor.fetch_data.retry.sleep = fake_sleep

    ing = BaseIngestor(BASE_URL)
    with respx.mock:
        respx.get(f"{BASE_URL}/rate-limited").mock(
            side_effect=[
                httpx.Response(
                    429,
                    json={
                        "statusCode": 429,
                        "message": "Rate limit is exceeded. Try again in 197 seconds.",
                    },
                ),
                httpx.Response(200, json={"ok": True}),
            ]
        )

        data = await ing.fetch_data("rate-limited")

    assert data == {"ok": True}
    assert sleeps == [197.0]
    await ing.close()


# --- TokenBucket: proactive self-pacing --------------------------------------


async def test_token_bucket_starts_full_and_does_not_block_within_capacity(monkeypatch):
    async def unexpected_sleep(seconds):
        raise AssertionError(f"should not have needed to sleep, got {seconds}s")

    monkeypatch.setattr("shared.base_ingestor.asyncio.sleep", unexpected_sleep)

    bucket = TokenBucket(rate=1.0, capacity=3.0)
    for _ in range(3):
        await bucket.acquire()


async def test_token_bucket_throttles_once_capacity_is_exhausted(monkeypatch):
    clock = {"now": 0.0}

    def fake_clock():
        return clock["now"]

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr("shared.base_ingestor.asyncio.sleep", fake_sleep)

    bucket = TokenBucket(rate=1.0, capacity=2.0, clock=fake_clock)
    await bucket.acquire()
    await bucket.acquire()
    # Bucket is now empty; the third acquire needs exactly one more token at
    # rate=1.0/s -- i.e. a 1.0s wait.
    await bucket.acquire()

    assert sleeps == [1.0]


async def test_base_ingestor_fetch_data_consumes_a_bucket_token(ingestor):
    starting_tokens = ingestor._bucket._tokens
    with respx.mock:
        respx.get(f"{BASE_URL}/paced").mock(return_value=httpx.Response(200, json={"ok": True}))
        await ingestor.fetch_data("paced")

    assert ingestor._bucket._tokens == pytest.approx(starting_tokens - 1.0, abs=1e-6)
    await ingestor.close()
