"""
Shared HTTP client for every ingestion service/script in this repo
(services/ingestor/main.py's live poller, shared/backfill.py's historical
backfill). Owns two distinct concerns, both aimed at the same problem --
`api.energidataservice.dk` rate-limits aggressively (see
docs/forecast-datasets-scope.md §1.4, verified live: ~20-30 rapid requests
triggers `{"statusCode": 429, "message": "Rate limit is exceeded. Try again
in 197 seconds."}`):

1. **Proactive pacing** (`TokenBucket`): a sustained caller (a multi-year,
   multi-dataset backfill in particular -- shared/backfill.py) self-paces
   its own request rate so it approaches, but doesn't cross, the point where
   the API starts responding 429, rather than firing requests as fast as
   possible and relying entirely on reactive retry to recover.
2. **Reactive, server-directed retry** (`fetch_data`'s `@retry`): if a 429
   happens anyway (a burst race across pacing layers, a stricter limit than
   expected, etc.), wait *exactly* the delay the API itself advertises --
   the `Retry-After` header if present, otherwise the "Try again in N
   seconds" text in the JSON body -- rather than our own exponential curve.
   Exponential backoff is the wrong tool for a 429 whose cooldown the server
   already tells you: guessing a shorter delay just re-triggers the limit
   and burns through the retry budget; guessing a much longer one wastes
   time the server didn't ask for. Every other retried error (5xx,
   timeouts, connection errors) keeps the original exponential-backoff
   behavior unchanged -- those don't come with a server-advertised cooldown
   to honor.
"""

import asyncio
import logging
import re
import time

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Matches the exact live message format (docs/forecast-datasets-scope.md
# §1.4): "Rate limit is exceeded. Try again in 197 seconds." Tolerant of a
# decimal seconds value and singular "second" even though neither has been
# observed live, since neither costs anything to accept.
_RETRY_AFTER_MESSAGE_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*seconds?", re.IGNORECASE)

# Fallback delay when a 429 response carries neither a parseable
# `Retry-After` header nor a parseable "Try again in N seconds" body message
# (a malformed/changed message shouldn't crash the retry -- see
# `parse_retry_after_seconds`'s docstring). Deliberately generous (the
# advertised cooldowns observed live are ~197s) so a fallback under-wait
# doesn't just immediately retrigger the same limit.
DEFAULT_RATE_LIMIT_FALLBACK_SECONDS = 60.0

# Token-bucket defaults for `TokenBucket`/`BaseIngestor`. Refill rate mirrors
# shared/backfill.py's `RATE_LIMIT_SECONDS = 3.0` -- already empirically
# tuned there (that module's docstring: "3.0s empirically kept 429s rare
# across a real 30-day/3-dataset backfill run") -- expressed here as ~1
# token every 3 seconds so a caller that paces itself at that rate never
# blocks on the bucket at all; a burstier caller (e.g. a tight backfill
# chunk loop with no external sleep) gets throttled down to this sustained
# rate automatically instead of hitting 429. Capacity of 20 sits just under
# the "~20-30 rapid requests" live-observed trigger point (§1.4), so an
# initial burst (e.g. one poll cycle's ~17 sequential dataset fetches) still
# passes straight through while a longer burst gets throttled before it can
# reach the point that actually triggers a 429.
DEFAULT_RATE_LIMIT_TOKENS_PER_SECOND = 1.0 / 3.0
DEFAULT_RATE_LIMIT_BURST_CAPACITY = 20.0


def parse_retry_after_seconds(
    response: httpx.Response, fallback: float = DEFAULT_RATE_LIMIT_FALLBACK_SECONDS
) -> float:
    """
    Determines how long to wait before retrying a 429 response, in order of
    preference:

    1. The `Retry-After` header (RFC 7231), if present and a bare integer/
       float seconds value -- the standard HTTP mechanism for this, so it
       wins over the body when both are present. (This API has not been
       observed to send an HTTP-date `Retry-After` value; that form is not
       handled here.)
    2. The "Rate limit is exceeded. Try again in N seconds." message in the
       JSON body (the format actually observed live -- see module
       docstring), via `_RETRY_AFTER_MESSAGE_RE`.
    3. `fallback`, logged as a warning -- a malformed/changed message must
       never raise or block retry entirely, it should just fall back to a
       conservative wait.
    """
    retry_after_header = response.headers.get("Retry-After")
    if retry_after_header is not None:
        try:
            return float(retry_after_header)
        except ValueError:
            logger.warning(
                "429 response had an unparseable Retry-After header %r -- falling back to "
                "the response body",
                retry_after_header,
            )

    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            message = str(body.get("message", ""))
    except Exception:
        pass

    match = _RETRY_AFTER_MESSAGE_RE.search(message)
    if match:
        return float(match.group(1))

    logger.warning(
        "429 response with no usable Retry-After header and an unparseable body message "
        "(%r) -- falling back to a %.0fs wait",
        message,
        fallback,
    )
    return fallback


def _wait_energinet_rate_limit(retry_state):
    """
    Tenacity `wait` callable for `BaseIngestor.fetch_data`: if the failure
    being retried is a 429, wait exactly the server-advertised delay
    (`parse_retry_after_seconds`); for every other retried error, fall back
    to the pre-existing exponential backoff (module docstring point 2).
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        delay = parse_retry_after_seconds(exc.response)
        logger.warning(
            "Rate limited (429); honoring server-advertised cooldown of %.1fs before retrying "
            "(attempt %d)",
            delay,
            retry_state.attempt_number,
        )
        return delay
    return wait_exponential(multiplier=1, min=2, max=10)(retry_state)


class TokenBucket:
    """
    Async token-bucket rate limiter: `acquire()` blocks (without busy-
    waiting -- it sleeps for exactly the computed deficit) until a token is
    available, then consumes it. Used by `BaseIngestor` to make a sustained
    caller (a multi-chunk/multi-dataset backfill in particular) self-pace
    its request rate rather than firing as fast as it can and depending
    entirely on reactive 429 retry to recover (see module docstring).

    Starts full (`tokens = capacity`) so a short-lived caller (a handful of
    ad-hoc requests, or the existing test suite) never observes any
    throttling at all -- only a sustained run that outpaces the refill rate
    does.
    """

    def __init__(self, rate: float, capacity: float, clock=time.monotonic):
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._last_refill = clock()
        self._lock = asyncio.Lock()

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Blocks until `tokens` are available, then consumes them."""
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait_time = (tokens - self._tokens) / self.rate
            await asyncio.sleep(wait_time)


class BaseIngestor:
    """
    Base class for all data ingestion services.
    Implements rate-limited, retrying HTTP handling for
    api.energidataservice.dk (see module docstring for the two-layer
    pacing/retry strategy).
    """

    def __init__(
        self,
        base_url: str,
        rate_limit_per_second: float = DEFAULT_RATE_LIMIT_TOKENS_PER_SECOND,
        rate_limit_burst_capacity: float = DEFAULT_RATE_LIMIT_BURST_CAPACITY,
        timeout: float = 30.0,
    ):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=timeout)
        self._bucket = TokenBucket(rate=rate_limit_per_second, capacity=rate_limit_burst_capacity)

    @retry(
        stop=stop_after_attempt(5),
        wait=_wait_energinet_rate_limit,
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
    )
    async def fetch_data(self, endpoint: str, params: dict = None):
        """
        Fetches data from Energinet/ENTSO-E, self-pacing via `TokenBucket`
        and retrying with a server-advertised (429) or exponential (other
        errors) delay -- see module docstring.
        """
        await self._bucket.acquire()
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise

    async def close(self):
        await self.client.aclose()
