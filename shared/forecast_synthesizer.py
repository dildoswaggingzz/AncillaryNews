"""
LLM synthesis of the Morning Brief's (M5) month/quarter/year forecasts.

Produces a plain-language, non-numeric-point-prediction outlook per horizon
("month" | "quarter" | "year"), each with a confidence tag and 1-2 "swing
factors" (the handful of things that could move the market either way) --
this is explicitly *not* a numeric price forecast (README "Brainstorming"
§ / the M5 plan's confirmed product decision), since a bare "DKK 450/MWh in
August" style prediction reads as false precision this pipeline has no basis
for. Citation-style validation here rejects exactly that shape rather than
the hard-data-traceability check `shared/event_synthesizer.py` runs (there's
no fixed "hard data window" for a forward-looking forecast to cite numbers
from) -- see `_rejects_bare_numeric_prediction` below.

Refresh cadence (confirmed product decision, see the M5 plan): month and
quarter forecasts cache/refresh weekly, year forecasts monthly -- none of
the three regenerate daily, so a normal morning brief run is a cache-hit for
all three except right after a refresh boundary.

Follows the exact `ANTHROPIC_API_KEY`-missing precedent as
`shared/event_synthesizer.py`: a missing key logs a warning and returns None
from `synthesize_forecast`, and `get_or_refresh_forecast` falls back to the
last-known-good cached forecast (even if stale) rather than failing the
whole Morning Brief pipeline over a missing key or a transient API failure.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta

from anthropic import AsyncAnthropic
from prometheus_client import Counter, Histogram

from shared.db_manager import DatabaseManager
from shared.event_synthesizer import extract_numbers
from shared.llm_json import extract_json_object
from shared.units import unit_for

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024

VALID_CONFIDENCE = {"low", "medium", "high"}
VALID_HORIZONS = {"month", "quarter", "year"}
REQUIRED_FORECAST_KEYS = {"narrative", "confidence", "swing_factors", "horizon"}

# Confirmed product decision (see module docstring / the M5 plan): month and
# quarter forecasts refresh weekly, year forecasts monthly.
REFRESH_INTERVALS = {
    "month": timedelta(days=7),
    "quarter": timedelta(days=7),
    "year": timedelta(days=30),
}

# How much daily-aggregate history (shared/db_manager.py:fetch_daily_aggregates)
# feeds the synthesis prompt's context, per horizon -- wider horizons look
# further back for seasonal context.
CONTEXT_WINDOW_DAYS = {"month": 90, "quarter": 180, "year": 365}

FORECAST_ZONE = "DK1"
FORECAST_MARKET = "day_ahead"
FORECAST_PRODUCT = "price"

LLM_CALL_TOTAL = Counter(
    "forecast_llm_calls_total", "Claude Opus forecast synthesis calls, by outcome", ["status"]
)
LLM_CALL_DURATION = Histogram(
    "forecast_llm_call_duration_seconds", "Claude Opus forecast synthesis call latency"
)
FORECAST_REJECTED_TOTAL = Counter(
    "forecast_rejected_total",
    "Synthesized forecasts rejected by programmatic validation (e.g. false-precision numeric "
    "price predictions)",
)

# A "bare numeric price prediction" is a number immediately adjacent to a
# price unit (DKK/EUR, optionally per MWh) -- e.g. "450 DKK/MWh" or "EUR 60"
# -- presented without any hedging language nearby. Reusing
# shared/event_synthesizer.py's `extract_numbers` for the actual number
# extraction, this module only needs to additionally check for a nearby
# price unit + absence of a hedge word, since (unlike event synthesis) there
# is no fixed hard-data window a forecast number could be traceable to.
_PRICE_UNIT_RE = re.compile(r"(DKK|EUR)\s*/?\s*(MWh)?", re.IGNORECASE)
_HEDGE_WORDS = (
    "around",
    "roughly",
    "approximately",
    "about",
    "could",
    "may",
    "might",
    "likely",
    "expect",
    "range",
    "between",
    "estimate",
    "possibly",
    "typically",
    "historically",
    "on average",
    "up to",
    "at least",
    "in the ballpark",
)
_HEDGE_WINDOW_CHARS = 40


def _client() -> AsyncAnthropic | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set; skipping forecast synthesis (falling back to the last "
            "known-good cached forecast, if any)"
        )
        return None
    return AsyncAnthropic(api_key=api_key)


def _rejects_bare_numeric_prediction(narrative: str) -> str | None:
    """
    Returns a rejection reason if `narrative` states a numeric price
    prediction as bare fact (no hedging language nearby), or None if it
    passes. Only flags numbers adjacent to a price unit (DKK/EUR) -- a
    forecast narrative can and should still cite real historical numbers
    (e.g. "prices averaged 450 DKK/MWh over the past quarter") as long as
    forward-looking claims are hedged, not presented as point predictions.
    """
    for match in _PRICE_UNIT_RE.finditer(narrative or ""):
        window_start = max(0, match.start() - _HEDGE_WINDOW_CHARS)
        window = narrative[window_start : match.end() + _HEDGE_WINDOW_CHARS].lower()
        # Does a number actually sit next to this unit? (avoids flagging
        # "DKK" mentioned with no adjacent figure at all.)
        if not extract_numbers(window):
            continue
        if not any(hedge in window for hedge in _HEDGE_WORDS):
            return (
                f"narrative states a numeric price figure near {match.group(0)!r} without "
                "hedging language -- reads as a bare point prediction"
            )
    return None


def _validate_forecast(forecast: dict, horizon: str) -> str | None:
    """Programmatic validation, mirroring `shared/event_synthesizer.py`'s
    "treat LLM output as untrusted input" posture. Returns a rejection
    reason string, or None if the forecast passes."""
    if not isinstance(forecast, dict):
        return "parsed output is not a JSON object"

    missing_keys = REQUIRED_FORECAST_KEYS - forecast.keys()
    if missing_keys:
        return f"missing required key(s): {sorted(missing_keys)}"

    if forecast.get("confidence") not in VALID_CONFIDENCE:
        return f"invalid confidence value: {forecast.get('confidence')!r}"

    swing_factors = forecast.get("swing_factors")
    if not isinstance(swing_factors, list) or not (1 <= len(swing_factors) <= 2):
        return f"swing_factors must be a list of 1-2 items, got {swing_factors!r}"

    narrative = forecast.get("narrative")
    if not narrative or not isinstance(narrative, str):
        return "narrative is missing or empty"

    rejection = _rejects_bare_numeric_prediction(narrative)
    if rejection is not None:
        return rejection
    for factor in swing_factors:
        if isinstance(factor, str):
            rejection = _rejects_bare_numeric_prediction(factor)
            if rejection is not None:
                return rejection

    return None


SYSTEM_PROMPT = """\
You are the forecasting engine for a plain-language "morning brief" aimed at \
a non-technical BESS (battery energy storage) operator in the Danish \
ancillary services / day-ahead power markets. You are given a summary of \
recent historical day-ahead price behaviour and asked to produce a \
qualitative, reasoned outlook for ONE horizon.

Produce ONE JSON object -- and nothing else, no markdown fences, no prose \
before or after -- with EXACTLY this shape:

{
  "horizon": "<echo the horizon given to you verbatim>",
  "narrative": "<2-4 sentences of plain-language outlook. NEVER state a bare \
numeric price prediction as fact (e.g. never write something like '450 \
DKK/MWh in August' without hedging language like 'around', 'could', \
'likely', 'roughly', or similar) -- describe DIRECTION and DRIVERS, not \
point forecasts. You MAY cite real historical numbers from the context you \
were given (clearly framed as historical, e.g. 'prices averaged X over the \
past quarter'), but never present a specific future number as certain.>",
  "confidence": "low | medium | high",
  "swing_factors": ["<1-2 short phrases naming the biggest things that could \
move this outlook either way, e.g. 'wind output', 'gas prices', \
'interconnector availability'>"]
}

Hard rules:
- Never assert a specific future price figure as fact -- always hedge \
forward-looking numeric claims, or better, avoid stating a specific future \
number at all and describe direction/magnitude qualitatively instead.
- "swing_factors" must contain exactly 1 or 2 items, each a short phrase, \
not a full sentence.
- If you are genuinely uncertain, say so honestly with "confidence": "low" \
rather than manufacturing false precision.
"""


def _build_user_prompt(horizon: str, context: dict) -> str:
    aggregates = context.get("daily_aggregates", [])
    if aggregates:
        values = [a["mean_value"] for a in aggregates if a.get("mean_value") is not None]
        # FORECAST_MARKET/FORECAST_ZONE/FORECAST_PRODUCT are fixed (module
        # constants below), so this is always the same registry-declared
        # unit -- resolved via shared/units.py rather than hardcoded, same
        # fix as shared/price_recap_synthesizer.py's equivalent line, so a
        # future FORECAST_ZONE/FORECAST_MARKET change can't silently
        # mislabel this summary.
        unit = unit_for(FORECAST_MARKET, context.get("zone", FORECAST_ZONE), FORECAST_PRODUCT)
        summary_line = (
            f"{len(aggregates)} day(s) of historical daily mean day-ahead price data, "
            f"ranging from {min(values):.1f} to {max(values):.1f} "
            f"(overall mean {sum(values) / len(values):.1f}) {unit}"
            if values
            else f"{len(aggregates)} day(s) of historical data, but no usable values"
        )
    else:
        summary_line = "no historical daily aggregate data was available for this window"

    return f"""\
Horizon: {horizon}
Zone: {context.get("zone", FORECAST_ZONE)}
Historical context window: {context.get("window_days")} day(s) ending {context.get("as_of")}
Historical summary: {summary_line}

Respond with only the JSON object described in the system prompt.
"""


def _build_context(db: DatabaseManager, horizon: str) -> dict:
    """
    Pulls the daily-aggregate historical window for `horizon`
    (shared/db_manager.py:fetch_daily_aggregates) as the forecast prompt's
    context -- there is no fixed "hard data window" the way there is for
    event synthesis (README §3C), since a forecast is inherently
    forward-looking; this is the closest analogue, a seasonal/statistical
    backdrop rather than a citable fact set.
    """
    window_days = CONTEXT_WINDOW_DAYS.get(horizon, 90)
    now = datetime.now(UTC)
    start_time = now - timedelta(days=window_days)
    aggregates = db.fetch_daily_aggregates(
        FORECAST_MARKET, FORECAST_ZONE, FORECAST_PRODUCT, start_time, now
    )
    return {
        "zone": FORECAST_ZONE,
        "window_days": window_days,
        "as_of": now.isoformat(),
        "daily_aggregates": aggregates,
    }


async def synthesize_forecast(
    horizon: str, context: dict, client: AsyncAnthropic | None = None
) -> dict | None:
    """
    Runs one LLM synthesis + programmatic validation pass for `horizon`
    ("month" | "quarter" | "year"). Returns the validated forecast dict
    `{narrative, confidence, swing_factors, horizon}`, or None if:
    - `ANTHROPIC_API_KEY` isn't configured (already logged as a warning by
      `_client`).
    - the model call itself fails.
    - the parsed output fails schema validation or the false-precision
      numeric-prediction check.
    """
    if horizon not in VALID_HORIZONS:
        raise ValueError(f"unknown forecast horizon {horizon!r}; expected one of {VALID_HORIZONS}")

    anthropic_client = client if client is not None else _client()
    if anthropic_client is None:
        return None

    user_prompt = _build_user_prompt(horizon, context)

    call_start = time.monotonic()
    try:
        response = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:
        logger.exception("Claude forecast synthesis call failed for horizon=%s", horizon)
        LLM_CALL_TOTAL.labels(status="error").inc()
        LLM_CALL_DURATION.observe(time.monotonic() - call_start)
        return None

    LLM_CALL_TOTAL.labels(status="success").inc()
    LLM_CALL_DURATION.observe(time.monotonic() - call_start)

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    forecast = extract_json_object(raw_text)
    if forecast is None:
        logger.error("Claude returned non-JSON forecast output for horizon=%s", horizon)
        return None

    forecast["horizon"] = horizon  # ours to own, not the model's -- see event_synthesizer precedent

    rejection_reason = _validate_forecast(forecast, horizon)
    if rejection_reason is not None:
        logger.warning(
            "Rejecting synthesized forecast for horizon=%s: %s", horizon, rejection_reason
        )
        FORECAST_REJECTED_TOTAL.inc()
        return None

    return forecast


async def get_or_refresh_forecast(
    db: DatabaseManager, horizon: str, client: AsyncAnthropic | None = None
) -> dict | None:
    """
    Cache-first entrypoint for one forecast horizon (README-style "hit the
    cache unless stale" pattern): returns `db.fetch_latest_forecast(horizon)`
    unchanged if it's still within its `valid_until` window; otherwise
    synthesizes a fresh one (`synthesize_forecast`), persists it
    (`db.save_forecast`), and returns that.

    If synthesis fails or is skipped (missing API key, model error, or
    validation rejection), falls back to the last-known-good cached
    forecast (even if stale) rather than failing the whole Morning Brief
    pipeline -- an old-but-still-reasonable forecast is better than none.
    Returns None only if there is truly no cache to fall back to.

    Returned dict shape matches `fetch_latest_forecast`'s row shape: `{id,
    horizon, generated_at, valid_until, forecast}`.
    """
    if horizon not in VALID_HORIZONS:
        raise ValueError(f"unknown forecast horizon {horizon!r}; expected one of {VALID_HORIZONS}")

    cached = db.fetch_latest_forecast(horizon)
    now = datetime.now(UTC)
    if cached is not None:
        valid_until = cached["valid_until"]
        if valid_until is not None and valid_until > now:
            return cached

    context = _build_context(db, horizon)
    fresh = await synthesize_forecast(horizon, context, client=client)
    if fresh is None:
        if cached is not None:
            logger.info(
                "Forecast synthesis unavailable/rejected for horizon=%s; falling back to cached "
                "forecast id=%s (generated_at=%s)",
                horizon,
                cached["id"],
                cached["generated_at"],
            )
        return cached

    valid_until = now + REFRESH_INTERVALS.get(horizon, timedelta(days=7))
    forecast_id = db.save_forecast(horizon, fresh, valid_until)
    return {
        "id": forecast_id,
        "horizon": horizon,
        "generated_at": now,
        "valid_until": valid_until,
        "forecast": fresh,
    }
