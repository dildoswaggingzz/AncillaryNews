"""
Claim extraction for the M3 Insight Crawler (README §3B / §6).

Summarizes an article and extracts discrete market theses via Claude Haiku
(`claude-haiku-4-5`, README §3C), each labelled `fact | theory | forecast`
per the two-tier trust model:

- **Tier 1** (Energinet, ENTSO-E, NBM) claims may be labelled `fact`.
- **Tier 2** (sector media, analysts) claims are *never* asserted as bare
  fact — if Claude's own classification comes back `fact` for a Tier 2
  article, we downgrade it to `theory` here rather than trust the model's
  judgement over the trust model the README hard-codes (§6: "Tier 2 content
  always framed as 'according to …'").

`ANTHROPIC_API_KEY` follows the same precedent as `SLACK_WEBHOOK_URL` in
`shared/slack_notifier.py`: unset in most dev/CI environments, so a missing
key logs a warning and returns None (a distinct sentinel from "zero claims
found") rather than raising, letting the caller fall back to storing the raw
article text.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Literal

from anthropic import AsyncAnthropic
from prometheus_client import Counter, Histogram

from shared.llm_json import extract_json_object
from shared.rss_reader import ArticleRef

logger = logging.getLogger(__name__)

# README §7: "LLM latency/cost" for the crawler's bulk Haiku extraction.
CLAIM_EXTRACTION_LLM_CALL_TOTAL = Counter(
    "crawler_llm_calls_total", "Claude Haiku claim-extraction calls, by outcome", ["status"]
)
CLAIM_EXTRACTION_LLM_CALL_DURATION = Histogram(
    "crawler_llm_call_duration_seconds", "Claude Haiku claim-extraction call latency"
)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 1024
ClaimType = Literal["fact", "theory", "forecast"]
VALID_CLAIM_TYPES = {"fact", "theory", "forecast"}

SYSTEM_PROMPT = """\
You are extracting structured market intelligence for a Danish ancillary \
services (balancing market) monitoring agent. Given a news article, do two \
things:

1. Write a one-to-two sentence summary of the article.
2. Extract each discrete market thesis / claim the article makes about \
Danish or Nordic electricity balancing markets (mFRR, aFRR, FCR, imbalance \
prices, interconnectors, wind/solar output, market-rule changes, etc). \
Skip claims unrelated to these markets. For each claim, classify it as \
exactly one of:
   - "fact": a plain factual statement of something that happened or a \
published figure.
   - "theory": an analyst's or commentator's explanation/interpretation of \
why something happened.
   - "forecast": a prediction about future prices, volumes, or market \
conditions.

Respond with ONLY a JSON object of the form:
{"summary": "...", "claims": [{"claim": "...", "claim_type": "fact|theory|forecast"}]}

If the article contains no relevant claims, return {"summary": "...", "claims": []}.
"""


@dataclass(frozen=True)
class ExtractedClaim:
    claim: str
    claim_type: ClaimType


@dataclass(frozen=True)
class ExtractionResult:
    summary: str
    claims: list[ExtractedClaim]


def _client() -> AsyncAnthropic | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set; skipping claim extraction (raw article text will be "
            "stored without derived claims)"
        )
        return None
    return AsyncAnthropic(api_key=api_key)


def _parse_response(raw_text: str, article: ArticleRef) -> ExtractionResult | None:
    payload = extract_json_object(raw_text)
    if payload is None:
        logger.error("Claude returned non-JSON claim extraction output for %s", article.url)
        return None

    summary = payload.get("summary", "")
    raw_claims = payload.get("claims", [])

    claims = []
    for raw_claim in raw_claims:
        text = raw_claim.get("claim")
        claim_type = raw_claim.get("claim_type")
        if not text:
            continue
        if claim_type not in VALID_CLAIM_TYPES:
            logger.warning(
                "Claude returned unrecognised claim_type=%r for %s; defaulting to 'theory'",
                claim_type,
                article.url,
            )
            claim_type = "theory"

        # README §6 two-tier trust model: Tier 2 (media/analyst) sources are
        # never citable as bare fact, regardless of what the model returns.
        if article.feed_tier == "tier2" and claim_type == "fact":
            logger.info(
                "Downgrading fact -> theory for Tier 2 source %s (%s)",
                article.feed_name,
                article.url,
            )
            claim_type = "theory"

        claims.append(ExtractedClaim(claim=text, claim_type=claim_type))

    return ExtractionResult(summary=summary, claims=claims)


async def extract_claims(
    article_text: str, article: ArticleRef, client: AsyncAnthropic | None = None
) -> ExtractionResult | None:
    """
    Summarizes `article_text` and extracts its market claims via Claude
    Haiku.

    Returns None if `ANTHROPIC_API_KEY` isn't configured (the caller should
    then store the raw article text without derived claims) or if the model
    call/response parsing fails outright. An `ExtractionResult` with an
    empty `claims` list is a *successful* call that simply found nothing
    relevant to extract — a different case from "skipped".
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
        logger.exception("Claude claim extraction call failed for %s", article.url)
        CLAIM_EXTRACTION_LLM_CALL_TOTAL.labels(status="error").inc()
        CLAIM_EXTRACTION_LLM_CALL_DURATION.observe(time.monotonic() - call_start)
        return None

    CLAIM_EXTRACTION_LLM_CALL_TOTAL.labels(status="success").inc()
    CLAIM_EXTRACTION_LLM_CALL_DURATION.observe(time.monotonic() - call_start)

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_response(raw_text, article)
