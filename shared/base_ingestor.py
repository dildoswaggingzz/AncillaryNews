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

**Honest assessment of what actually carries a backfill (2026-07-20, M6
rate-limit cleanup):** a live 91-day/4-dataset backfill measured over the
same 60-minute window as the live poller: the poller (small `limit`,
100-2000) saw a ~5% 429 rate; the backfill (`shared/backfill.py`'s much
larger `limit`, up to `CHUNK_LIMIT`=300000) saw a ~37% 429 rate, with nearly
every backfill request getting a 429 on its *first* attempt and succeeding
after the server-advertised cooldown (~36-60s) -- i.e. concern 2 (reactive
retry) absorbed essentially all of it, 0 chunks failed outright, at the cost
of a ~60s penalty per retried chunk. Concern 1 (`TokenBucket`) did not
prevent that -- it paces *request count*, and a backfill's problem in
practice is response *volume* (a `limit=300000` request vs. the poller's
`limit=100-2000`), which the bucket does not currently account for (each
`fetch_data` call costs exactly 1 token regardless of `limit`). Whether
Energinet's quota is actually volume-sensitive (and a volume-aware bucket
would meaningfully help) versus purely request-count (in which case the
bucket's calibration is what would need retuning) has not been established
live -- a planned controlled probe to distinguish the two was blocked by a
backfill already in flight against the same API from the same IP at the
time (any 429 measurement taken then would be contaminated by concurrent
load, not usable to isolate volume-sensitivity). So: treat `TokenBucket` as
a politeness measure only -- it keeps a *fast* burst of many small requests
from tripping the limit, which is real and worth keeping -- not as the
thing that makes backfills succeed. The thing that actually makes backfills
succeed today is concern 2, the reactive server-directed retry; do not
assume retuning `TokenBucket` alone would reduce the backfill's 429 rate
without first confirming (via that probe, run when the API is quiet) what
the quota is actually keyed on.
"""

import asyncio
import logging
import re
import time

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

    Deliberately does not log -- `_log_before_retry` (this module's
    `before_sleep` hook) is the single place that logs "a retry is about to
    happen", so every retry (429 or otherwise) gets exactly one WARNING
    line instead of this function and that hook each logging their own.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return parse_retry_after_seconds(exc.response)
    return wait_exponential(multiplier=1, min=2, max=10)(retry_state)


def _log_before_retry(retry_state) -> None:
    """
    Tenacity `before_sleep` hook for `fetch_data`: the single place that
    logs "this request failed but is about to be retried". Tenacity only
    invokes `before_sleep` when it has already decided to retry (i.e. never
    on the final, exhausted attempt -- see `_log_retry_exhausted` for that
    case), so this is naturally WARNING-level, not ERROR: a 429 (or
    transient 5xx/timeout) that self-heals via retry is expected, routine
    behavior against this API (module docstring), not an incident. Logging
    it at ERROR -- the previous behavior, one `logger.error` line per
    attempt inside `fetch_data` itself -- made an unattended backfill run
    read as a stream of failures when nothing was actually wrong, and would
    bury a genuine, retry-exhausted failure in the noise.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
    logger.warning(
        "fetch_data attempt %d failed (%s); retrying in %.1fs",
        retry_state.attempt_number,
        f"HTTP {status_code}" if status_code is not None else repr(exc),
        delay,
    )


def _log_retry_exhausted(retry_state):
    """
    Tenacity `retry_error_callback` for `fetch_data`: fires exactly once,
    only when the retry budget (`stop_after_attempt(5)`) is actually
    exhausted -- i.e. every attempt failed and there will be no more, as
    opposed to `_log_before_retry`'s WARNING for a failure that is still
    going to be retried. This is the one case in `fetch_data`'s retry
    lifecycle that genuinely warrants ERROR.

    Re-raises the same `tenacity.RetryError` tenacity would raise on its
    own if no `retry_error_callback` were configured (see tenacity's
    `BaseRetrying._post_stop_check_actions`: with no callback, and
    `reraise` left at its default `False`, it raises `self.retry_error_cls
    (fut) from fut.exception()` -- this mirrors that exactly), so
    `fetch_data`'s existing exhausted-retries contract
    (tests/test_base_ingestor.py::test_fetch_data_raises_after_exhausting_retries)
    is unchanged; only the logging moves from per-attempt ERROR to a single
    ERROR at the point retries are actually given up on.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    logger.error(
        "fetch_data exhausted its retry budget after %d attempt(s): %r",
        retry_state.attempt_number,
        exc,
    )
    raise RetryError(retry_state.outcome) from exc


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
        before_sleep=_log_before_retry,
        retry_error_callback=_log_retry_exhausted,
    )
    async def fetch_data(self, endpoint: str, params: dict = None):
        """
        Fetches data from Energinet/ENTSO-E, self-pacing via `TokenBucket`
        and retrying with a server-advertised (429) or exponential (other
        errors) delay -- see module docstring.

        Logging: a failed attempt that tenacity is going to retry logs at
        DEBUG here (the raw HTTP/exception line) and WARNING once from
        `_log_before_retry`; only a failure that survives all 5 attempts
        logs at ERROR, from `_log_retry_exhausted`. See those two
        functions' docstrings -- the previous behavior (ERROR on every
        single attempt, including ones about to self-heal via retry) made
        routine, expected 429s indistinguishable from a genuine failure in
        an unattended run's logs.
        """
        await self._bucket.acquire()
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.debug("HTTP error occurred: %s", e.response.status_code)
            raise
        except Exception as e:
            logger.debug("Unexpected error: %s", e)
            raise

    async def close(self):
        await self.client.aclose()
