"""
Supply/demand-event extraction for M6+ (docs/supply-event-features-design.md
§3), beside `shared/claim_extractor.py` and following the same conventions:

- **Model: `claude-haiku-4-5`** (`MODEL`, matching `claim_extractor.MODEL`
  exactly) -- structured extraction at volume, the same Haiku use-case as the
  claim path, consistent with it. Not a larger model.
- **`ANTHROPIC_API_KEY`-missing precedent, verbatim from
  `claim_extractor.py`:** a missing key or a failed call logs a warning and
  returns `None` (a sentinel distinct from "zero events found"), never
  raises -- the crawler must degrade to claims-only, never crash, on this
  path (design §4).
- **Two-tier trust model, as in `claim_extractor.py`:** a Tier-2 (sector
  media/analyst) source never yields a high-confidence event -- `confidence`
  is capped at `TIER2_CONFIDENCE_CAP` here, downgrading Claude's own
  confidence estimate rather than trusting the model's judgement over the
  trust model the design hard-codes (design §3), the same "we don't trust
  the model's own classification over our own trust model" precedent as
  `claim_extractor._parse_response`'s fact -> theory downgrade.

**What this module does NOT do, deliberately (design §1/§3):** it never
assigns `known_at`. The crawler (`services/crawler/main.py`) assigns
`known_at` from `ArticleRef.published` (falling back to crawl time) --
*never* the model, which must not be trusted to date events (a model
inferring "when this became public" from article text is exactly the kind
of judgement call the leak-safety guarantee in `shared/feature_store.py`
cannot depend on). `ExtractedEvent` below carries no `known_at`/`event_id`/
`source_*`/`extracted_at` field at all for the same reason -- those are
storage-layer concerns the crawler assembles, not extraction output.

`magnitude_mw` and `effective_from` are asked for as null rather than a
guess (design §3) -- a hallucinated MW figure becomes a numeric feature in
`shared/feature_store.py`, which is worse than a null one, because a null is
visibly absent and a wrong number looks like real signal.
"""

import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from typing import Literal

from anthropic import AsyncAnthropic
from prometheus_client import Counter, Histogram

from shared.llm_json import extract_json_object
from shared.rss_reader import ArticleRef

logger = logging.getLogger(__name__)

EVENT_EXTRACTION_LLM_CALL_TOTAL = Counter(
    "crawler_event_llm_calls_total", "Claude Haiku event-extraction calls, by outcome", ["status"]
)
EVENT_EXTRACTION_LLM_CALL_DURATION = Histogram(
    "crawler_event_llm_call_duration_seconds", "Claude Haiku event-extraction call latency"
)

# Matches `shared/claim_extractor.py`'s `MODEL` exactly (design §3: "this is
# structured extraction at volume, the exact Haiku use-case, and consistent
# with the claim path. Do not use a larger model.") -- not imported from
# there, to keep this module's own contract self-contained and independently
# testable, but must never drift from that value.
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1024

EventType = Literal[
    "prequalification",
    "capacity_commissioning",
    "capacity_retirement",
    "demand_volume_change",
    "outage",
    "regime_change",
    "other",
]
VALID_EVENT_TYPES = {
    "prequalification",
    "capacity_commissioning",
    "capacity_retirement",
    "demand_volume_change",
    "outage",
    "regime_change",
    "other",
}
VALID_DIRECTIONS = {"up", "down"}

# Design §3: "a Tier-2 (sector media/analyst) source never yields a
# high-confidence event -- cap Tier-2 confidence (e.g. ≤ 0.5)".
TIER2_CONFIDENCE_CAP = 0.5

SYSTEM_PROMPT = """\
You are extracting structured supply/demand/regime events for a Danish \
ancillary services (balancing market) monitoring agent. Given a news \
article, extract every discrete event about Danish or Nordic electricity \
balancing market supply or demand: battery/generator prequalification into \
a market, new capacity being commissioned or retired, a change to the \
demand volume a TSO procures, an outage, or a market-rule/regime change. \
Skip anything unrelated to these.

For each event, extract exactly these fields:
- "event_type": one of "prequalification", "capacity_commissioning", \
"capacity_retirement", "demand_volume_change", "outage", "regime_change", \
"other".
- "market": one of "FCR", "aFRR", "mFRR", "FFR", "day_ahead", or null if \
not stated or not applicable.
- "zone": a bidding zone such as "DK1", "DK2", "SE4", or null if not \
stated or not zone-specific.
- "direction": "up", "down", or null (FCR-D and similar products are \
directional; leave null if the event doesn't specify or isn't directional).
- "magnitude_mw": a number of megawatts, or null if the article does not \
state one. Do NOT estimate, infer, or guess a figure -- if it isn't \
explicitly stated, use null.
- "effective_from": an ISO date (YYYY-MM-DD) the change takes effect, or \
null if not stated. Do NOT guess a date -- if the article only gives a \
vague timeframe (e.g. "later this year") without a specific date, use null.
- "confidence": your own confidence (0.0-1.0) that this event is real and \
correctly extracted.
- "raw_excerpt": the exact sentence (verbatim, from the article) this event \
was extracted from. Every event must be traceable to source text -- never \
omit this.

Respond with ONLY a JSON object of the form:
{"events": [{"event_type": "...", "market": ..., "zone": ..., \
"direction": ..., "magnitude_mw": ..., "effective_from": ..., \
"confidence": 0.0, "raw_excerpt": "..."}]}

If the article contains no relevant events, return {"events": []}.
"""


@dataclass(frozen=True)
class ExtractedEvent:
    """
    One extracted event, deliberately carrying only what the model actually
    produces -- no `known_at`, `event_id`, `source_*`, or `extracted_at`
    (module docstring: those are the crawler's job, never the model's).
    """

    event_type: EventType
    market: str | None
    zone: str | None
    direction: str | None
    magnitude_mw: float | None
    effective_from: date | None
    confidence: float
    raw_excerpt: str


def _client() -> AsyncAnthropic | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set; skipping event extraction (article will still be "
            "processed for claims as usual)"
        )
        return None
    return AsyncAnthropic(api_key=api_key)


def _parse_magnitude(raw) -> float | None:
    """Never coerces/guesses -- anything that isn't cleanly numeric becomes `None` (design §3)."""
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is an int subclass; explicitly excluded
        return None
    if isinstance(raw, int | float):
        return float(raw)
    return None


def _parse_effective_from(raw) -> date | None:
    """Only a strict `YYYY-MM-DD` string parses -- anything else becomes `None` (design §3)."""
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _parse_confidence(raw) -> float:
    """Clamped to `[0.0, 1.0]`; anything unparseable defaults to the conservative floor `0.0`."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value))


def _parse_response(raw_text: str, article: ArticleRef) -> list[ExtractedEvent] | None:
    payload = extract_json_object(raw_text)
    if payload is None:
        logger.error("Claude returned non-JSON event extraction output for %s", article.url)
        return None

    raw_events = payload.get("events", [])
    events: list[ExtractedEvent] = []

    for raw_event in raw_events:
        raw_excerpt = raw_event.get("raw_excerpt")
        if not raw_excerpt:
            # Design §2: "every event must be traceable to source text" --
            # an event with no excerpt to point at is dropped, not stored
            # with a placeholder.
            logger.warning(
                "Dropping event with no raw_excerpt (untraceable to source text) for %s",
                article.url,
            )
            continue

        event_type = raw_event.get("event_type")
        if event_type not in VALID_EVENT_TYPES:
            logger.warning(
                "Claude returned unrecognised event_type=%r for %s; defaulting to 'other'",
                event_type,
                article.url,
            )
            event_type = "other"

        direction = raw_event.get("direction")
        if direction not in VALID_DIRECTIONS:
            direction = None

        confidence = _parse_confidence(raw_event.get("confidence"))

        # Design §3's two-tier trust model, mirroring
        # `claim_extractor._parse_response`'s fact -> theory downgrade: a
        # Tier-2 source's event confidence is capped here, regardless of
        # what the model itself returned, rather than trusting the model's
        # own judgement over the trust model the design hard-codes.
        if article.feed_tier == "tier2" and confidence > TIER2_CONFIDENCE_CAP:
            logger.info(
                "Capping confidence %.2f -> %.2f for Tier 2 source %s (%s)",
                confidence,
                TIER2_CONFIDENCE_CAP,
                article.feed_name,
                article.url,
            )
            confidence = TIER2_CONFIDENCE_CAP

        events.append(
            ExtractedEvent(
                event_type=event_type,
                market=raw_event.get("market") or None,
                zone=raw_event.get("zone") or None,
                direction=direction,
                magnitude_mw=_parse_magnitude(raw_event.get("magnitude_mw")),
                effective_from=_parse_effective_from(raw_event.get("effective_from")),
                confidence=confidence,
                raw_excerpt=raw_excerpt,
            )
        )

    return events


async def extract_events(
    article_text: str, article: ArticleRef, client: AsyncAnthropic | None = None
) -> list[ExtractedEvent] | None:
    """
    Extracts supply/demand/regime events from `article_text` via Claude
    Haiku.

    Returns `None` if `ANTHROPIC_API_KEY` isn't configured, or if the model
    call/response parsing fails outright -- the caller (`services/crawler/
    main.py:process_article`) must treat this as "skip the event path, but
    do not let it affect claim storage or the crawl cycle" (design §4). An
    empty list is a *successful* call that found nothing relevant, distinct
    from "skipped".
    """
    anthropic_client = client if client is not None else _client()
    if anthropic_client is None:
        return None

    call_start = time.monotonic()
    try:
        response = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Article title: {article.title}\n\nArticle text:\n{article_text}",
                }
            ],
        )
    except Exception:
        logger.exception("Claude event extraction call failed for %s", article.url)
        EVENT_EXTRACTION_LLM_CALL_TOTAL.labels(status="error").inc()
        EVENT_EXTRACTION_LLM_CALL_DURATION.observe(time.monotonic() - call_start)
        return None

    EVENT_EXTRACTION_LLM_CALL_TOTAL.labels(status="success").inc()
    EVENT_EXTRACTION_LLM_CALL_DURATION.observe(time.monotonic() - call_start)

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_response(raw_text, article)
