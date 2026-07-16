"""
LLM synthesis for the M4 Intelligence Orchestrator (README §3C step 4, §2
output contract, §5 revision handling, §6 citation validation).

Follows the exact `ANTHROPIC_API_KEY`-missing precedent as
`shared/claim_extractor.py`: a missing key logs a warning and returns None
rather than raising, so a trigger's raw Slack alert (already sent by
`shared/rule_engine.py`) is never blocked on synthesis being available.

Citation validation (README §6: "Reports failing citation validation ...
are rejected before publication") is enforced *programmatically* on the
parsed JSON, not left to prompt instructions alone — prompt instructions are
a strong hint, not a guarantee, and this module treats the LLM output as
untrusted input that must pass structural + numeric-provenance checks before
anything is published.
"""

import json
import logging
import os
import re
import time

from anthropic import AsyncAnthropic
from prometheus_client import Counter, Histogram

from shared.rule_engine import Trigger

logger = logging.getLogger(__name__)

# README §7: "LLM latency/cost" for the orchestrator's synthesis calls.
# `status` distinguishes a failed API call from a successful one; citation
# rejections (README §6) are counted separately since a rejected report is a
# *successful* API call that failed post-hoc validation, not an API error.
LLM_CALL_TOTAL = Counter(
    "orchestrator_llm_calls_total", "Claude Opus synthesis calls, by outcome", ["status"]
)
LLM_CALL_DURATION = Histogram(
    "orchestrator_llm_call_duration_seconds", "Claude Opus synthesis call latency"
)
CITATION_REJECTED_TOTAL = Counter(
    "orchestrator_citation_rejected_total",
    "Synthesized Event Reports rejected by programmatic citation validation (README §6)",
)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 2048

VALID_CONFIDENCE = {"low", "medium", "high"}
REQUIRED_REPORT_KEYS = {
    "event_id",
    "market",
    "zone",
    "direction",
    "observation",
    "hard_data_correlates",
    "market_theories",
    "synthesis",
    "confidence",
    "data_maturity",
}

# A hard_data_correlate's numeric "value" must match a context-window number
# within this tolerance (the LLM may reasonably round/reformat, e.g.
# "4850.0" -> "4,850", or report a delta) to still count as traceable.
NUMBER_MATCH_ABS_TOLERANCE = 0.5
NUMBER_MATCH_REL_TOLERANCE = 0.01

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

SYSTEM_PROMPT = """\
You are the synthesis engine for an autonomous news agent monitoring Danish \
ancillary services (balancing) markets. You are given one fired anomaly \
trigger, a window of hard time-series data around it, and a set of \
semantically related claims retrieved from a corpus of crawled market news.

Produce ONE JSON object — and nothing else, no markdown fences, no prose \
before or after — with EXACTLY this shape:

{
  "event_id": "<echo the event_id given to you verbatim>",
  "market": "<echo the market given to you verbatim>",
  "zone": "<echo the zone given to you verbatim>",
  "direction": "<echo the direction given to you verbatim>",
  "observation": "<one sentence describing what happened, in plain language, \
citing the actual numbers from the hard data window>",
  "hard_data_correlates": [
    {"signal": "<short label>", "value": "<number(s) + unit, taken ONLY from \
the hard data window you were given>", "source": "<the source given in the \
hard data window, e.g. 'Energinet'>"}
  ],
  "market_theories": [
    {"claim": "<the claim text, taken ONLY from the retrieved claims you \
were given, optionally lightly paraphrased but not invented>", \
"source": "<the exact source given for that claim>", "type": "theory | \
forecast | fact"}
  ],
  "synthesis": "<2-4 sentence explanation of why this happened, citing every \
number to the hard data window and every market theory to its source using \
phrasing like 'according to <source>' or '<source> reports that ...' -- \
NEVER assert a Tier 2 (media/analyst) claim as bare fact>",
  "confidence": "low | medium | high",
  "data_maturity": "<one short phrase declaring whether these figures are \
provisional (still subject to Energinet revision) or settled -- note that \
the only revision-tracking dimension available is 'fetched_at', a proxy for \
true published_at, so figures should generally be described as provisional \
unless you were told otherwise>"
}

Hard rules (README §6 two-tier trust model):
- EVERY number in "hard_data_correlates" MUST come from the hard data window \
you were given -- never invent, estimate, or infer a number that isn't \
present there.
- EVERY entry in "market_theories" MUST carry a non-empty "source" field \
copied from the retrieved claims -- an unattributed theory is not allowed. \
If no retrieved claims are relevant, return an empty "market_theories" list \
rather than inventing one.
- If there is genuinely nothing useful to synthesize beyond the raw trigger, \
still return the full JSON shape with an honest, low-confidence synthesis \
and an empty market_theories list -- never omit a required key.
"""


def _client() -> AsyncAnthropic | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set; skipping event synthesis (the raw trigger was already "
            "sent to Slack by the rule engine)"
        )
        return None
    return AsyncAnthropic(api_key=api_key)


def infer_direction(trigger: Trigger) -> str:
    """
    Derives an "up"/"down" direction for the Event Report from a Trigger
    that has no explicit direction field: `product` itself is "up"/"down"
    for most activation/capacity series; otherwise we infer from whether the
    fired value moved above or below its baseline.
    """
    if trigger.product in ("up", "down"):
        return trigger.product
    if trigger.baseline is not None:
        return "up" if trigger.value > trigger.baseline else "down"
    return "n/a"


def build_event_id(trigger: Trigger, direction: str) -> str:
    """
    Builds a README §2-style event_id, e.g.
    "2026-07-14T17:15:00+00:00-DK1-mFRR_capacity-up". Deterministic per
    (market, zone, product, time, direction) so a trigger re-fired for the
    same already-published key is a harmless no-op INSERT
    (`DatabaseManager.save_event_report` uses ON CONFLICT DO NOTHING), not a
    duplicate report.
    """
    market_slug = trigger.market.replace(" ", "-")
    return f"{trigger.time}-{trigger.zone}-{market_slug}-{trigger.product}-{direction}"


def _known_numbers(trigger: Trigger, context_window: list[dict]) -> list[float]:
    known = []
    for row in context_window:
        if row.get("value") is not None:
            known.append(float(row["value"]))
    for v in (trigger.value, trigger.baseline, trigger.threshold):
        if v is not None:
            known.append(float(v))
    return known


def _extract_numbers(text: str) -> list[float]:
    numbers = []
    for match in _NUMBER_RE.findall(text or ""):
        try:
            numbers.append(float(match.replace(",", "")))
        except ValueError:
            continue
    return numbers


def _number_is_traceable(number: float, known_numbers: list[float]) -> bool:
    for k in known_numbers:
        tolerance = max(NUMBER_MATCH_ABS_TOLERANCE, NUMBER_MATCH_REL_TOLERANCE * abs(k))
        if abs(number - k) <= tolerance:
            return True
    return False


def _validate_report(report: dict, trigger: Trigger, context_window: list[dict]) -> str | None:
    """
    Programmatic citation validation (README §6). Returns a human-readable
    rejection reason string if the report must be rejected, or None if it
    passes.
    """
    if not isinstance(report, dict):
        return "parsed output is not a JSON object"

    missing_keys = REQUIRED_REPORT_KEYS - report.keys()
    if missing_keys:
        return f"missing required key(s): {sorted(missing_keys)}"

    if report.get("confidence") not in VALID_CONFIDENCE:
        return f"invalid confidence value: {report.get('confidence')!r}"

    hard_data_correlates = report.get("hard_data_correlates")
    if not isinstance(hard_data_correlates, list):
        return "hard_data_correlates is not a list"

    known_numbers = _known_numbers(trigger, context_window)
    for i, correlate in enumerate(hard_data_correlates):
        if not isinstance(correlate, dict) or not correlate.get("source"):
            return f"hard_data_correlates[{i}] is missing a source"
        for number in _extract_numbers(correlate.get("value", "")):
            if not _number_is_traceable(number, known_numbers):
                return (
                    f"hard_data_correlates[{i}] cites number {number} not traceable to the "
                    "pulled context window"
                )

    market_theories = report.get("market_theories")
    if not isinstance(market_theories, list):
        return "market_theories is not a list"

    for i, theory in enumerate(market_theories):
        if not isinstance(theory, dict) or not theory.get("source"):
            return f"market_theories[{i}] is missing a source (README §6 citation rule)"
        if not theory.get("claim"):
            return f"market_theories[{i}] is missing a claim"

    return None


def _parse_response(raw_text: str) -> dict | None:
    try:
        return json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        logger.error("Claude returned non-JSON synthesis output")
        return None


def _build_user_prompt(
    trigger: Trigger,
    event_id: str,
    direction: str,
    context_window: list[dict],
    retrieved_claims: list[dict],
) -> str:
    window_lines = (
        "\n".join(
            f"- time={row['time']} value={row['value']} source={row.get('source', 'Energinet')} "
            f"is_provisional={row.get('is_provisional', True)}"
            for row in context_window
        )
        or "(no hard data window rows available)"
    )
    claim_lines = (
        "\n".join(
            f'- claim="{c.get("claim")}" source="{c.get("source")}" '
            f"claim_type={c.get('claim_type')} retrieved_at={c.get('retrieved_at')}"
            for c in retrieved_claims
        )
        or "(no related claims retrieved)"
    )

    return f"""\
Trigger fired:
- event_id: {event_id}
- trigger_type: {trigger.trigger_type}
- market: {trigger.market}
- zone: {trigger.zone}
- product: {trigger.product}
- direction: {direction}
- fired value: {trigger.value}
- baseline: {trigger.baseline}
- threshold: {trigger.threshold}
- time: {trigger.time}
- details: {trigger.details}

Hard data window (Tier 1 -- Energinet, citable as fact):
{window_lines}

Retrieved related claims (Tier 2 unless claim_type is "fact" from a Tier 1 \
source -- always cite the given source, always frame as attributed):
{claim_lines}

Respond with only the JSON object described in the system prompt.
"""


async def synthesize_event_report(
    trigger: Trigger,
    context_window: list[dict],
    retrieved_claims: list[dict],
    client: AsyncAnthropic | None = None,
) -> dict | None:
    """
    Runs the full LLM synthesis + programmatic citation-validation pipeline
    for one fired Trigger.

    Returns the validated Event Report dict (README §2 shape), ready for
    `DatabaseManager.save_event_report` / Slack, or None if:
    - `ANTHROPIC_API_KEY` isn't configured (skip, already logged as a
      warning -- the raw trigger already reached Slack via the rule engine).
    - the model call itself fails.
    - the parsed output fails schema or citation validation (rejected --
      logged clearly, never published, never raised).
    """
    anthropic_client = client if client is not None else _client()
    if anthropic_client is None:
        return None

    direction = infer_direction(trigger)
    event_id = build_event_id(trigger, direction)
    user_prompt = _build_user_prompt(trigger, event_id, direction, context_window, retrieved_claims)

    call_start = time.monotonic()
    try:
        response = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:
        logger.exception(
            "Claude event synthesis call failed for trigger_type=%s market=%s zone=%s product=%s",
            trigger.trigger_type,
            trigger.market,
            trigger.zone,
            trigger.product,
        )
        LLM_CALL_TOTAL.labels(status="error").inc()
        LLM_CALL_DURATION.observe(time.monotonic() - call_start)
        return None

    LLM_CALL_TOTAL.labels(status="success").inc()
    LLM_CALL_DURATION.observe(time.monotonic() - call_start)

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    report = _parse_response(raw_text)
    if report is None:
        return None

    # event_id/market/zone/direction are ours to own, not the model's --
    # overwrite whatever it echoed back so a paraphrase/typo can never
    # desync the report from the trigger it was synthesized for.
    report["event_id"] = event_id
    report["market"] = trigger.market
    report["zone"] = trigger.zone
    report["direction"] = direction

    rejection_reason = _validate_report(report, trigger, context_window)
    if rejection_reason is not None:
        logger.warning(
            "Rejecting synthesized Event Report for %s (event_id=%s): %s",
            trigger.trigger_type,
            event_id,
            rejection_reason,
        )
        CITATION_REJECTED_TOTAL.inc()
        return None

    return report
