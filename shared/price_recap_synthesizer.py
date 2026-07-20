"""
LLM synthesis of the Morning Brief's (M5) "yesterday's prices, and why they
moved" price recap section.

Pulls yesterday's `day_ahead`/`FCR`/`aFRR_capacity` for DK1+DK2 plus grid
context (`system_state` wind/solar/CO2 at zone="ALL", plus DK2 grid inertia
via `inertia`/`DK2`/`dk2` -- see `SYSTEM_STATE_KEYS` below), computes
trailing-30-day comparison stats via a new pure helper
(`_recap_stats`/`_pull_recap_data` below -- `shared/rule_engine.py`'s checks
are trigger-shaped, firing only above a 3.0 std threshold, not recap-shaped,
so this recomputes its own simple mean/min/max comparison rather than
reusing that module), then makes one Claude Opus call producing 2-3
causal-factor sentences that may cite ONLY the pre-computed stats handed to
it.

Citation validation reuses `shared/event_synthesizer.py`'s promoted, public
`extract_numbers`/`number_is_traceable` helpers rather than duplicating that
logic -- every numeric claim in the synthesized recap must trace back to a
real pulled/computed number, same "treat LLM output as untrusted input"
posture as event synthesis.

Follows the exact `ANTHROPIC_API_KEY`-missing precedent as
`shared/event_synthesizer.py`/`shared/forecast_synthesizer.py`: a missing
key logs a warning and returns a recap built entirely from the pre-computed
stats (no LLM causal-factor sentences), rather than failing the whole
Morning Brief pipeline.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time

from anthropic import AsyncAnthropic
from prometheus_client import Counter, Histogram

from shared.db_manager import DatabaseManager
from shared.event_synthesizer import extract_numbers, number_is_traceable
from shared.llm_json import extract_json_object
from shared.units import unit_for

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024

ZONES = ("DK1", "DK2")
PRICE_SERIES = (("day_ahead", "price"), ("FCR", "price"), ("aFRR_capacity", "up"))
SYSTEM_STATE_ZONE = "ALL"
SYSTEM_STATE_MARKET = "system_state"
SYSTEM_STATE_PRODUCTS = ("onshore_wind", "offshore_wind", "solar", "co2_emission")

# System-state/grid-context keys pulled and prompted alongside each other
# (see _pull_recap_data / _build_user_prompt below) -- (market, zone,
# product) rather than (market, product) since, unlike the wind/solar/CO2
# block above, grid inertia (shared/datasets.py's inertia_nordic entry) is
# NOT zone="ALL": it's DK2's own inertia figure specifically, not a
# Nordic-wide one. Low Nordic inertia is a genuine causal driver of
# FCR-D/FFR demand (less rotating mass -> faster frequency swings after a
# disturbance -> more disturbance reserve procured), which is exactly the
# "explain why prices moved" job this module exists for -- see
# shared/datasets.py's inertia_nordic entry for the full rationale.
SYSTEM_STATE_KEYS: tuple[tuple[str, str, str], ...] = (
    *((SYSTEM_STATE_MARKET, SYSTEM_STATE_ZONE, product) for product in SYSTEM_STATE_PRODUCTS),
    ("inertia", "DK2", "dk2"),
)

TRAILING_WINDOW_DAYS = 30

LLM_CALL_TOTAL = Counter(
    "price_recap_llm_calls_total", "Claude Opus price-recap synthesis calls, by outcome", ["status"]
)
LLM_CALL_DURATION = Histogram(
    "price_recap_llm_call_duration_seconds", "Claude Opus price-recap synthesis call latency"
)
RECAP_REJECTED_TOTAL = Counter(
    "price_recap_rejected_total",
    "Synthesized price-recap causal factors rejected by programmatic citation validation",
)

JARGON_GLOSSARY = {
    "day-ahead price": "The price for electricity delivered the next day, set by the Nord Pool "
    "auction -- the main reference price for the market.",
    "FCR": "Frequency Containment Reserve -- a capacity a battery can be paid to hold in reserve "
    "to help keep the grid's frequency stable.",
    "aFRR capacity": "Automatic Frequency Restoration Reserve capacity -- another paid "
    "reserve-holding market a battery can participate in.",
}


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, dt_time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _recap_stats(
    db: DatabaseManager, market: str, zone: str, product: str, yesterday: date
) -> dict:
    """
    Pure helper: yesterday's single-day mean/min/max for one series, plus
    the trailing-30-day (the 30 days *before* yesterday, excluding it) mean
    for comparison -- the closest recap-shaped analogue to
    `shared/rule_engine.py`'s trigger-shaped baseline, but computed fresh
    here since a recap needs "how does yesterday compare to the recent
    past", not "did this exceed a firing threshold".
    """
    yesterday_start, yesterday_end = _day_bounds(yesterday)
    trailing_start = yesterday_start - timedelta(days=TRAILING_WINDOW_DAYS)

    yesterday_rows = db.fetch_daily_aggregates(
        market, zone, product, yesterday_start, yesterday_end
    )
    trailing_rows = db.fetch_daily_aggregates(
        market, zone, product, trailing_start, yesterday_start
    )

    yesterday_mean = yesterday_rows[0]["mean_value"] if yesterday_rows else None
    trailing_means = [r["mean_value"] for r in trailing_rows if r.get("mean_value") is not None]
    trailing_mean = sum(trailing_means) / len(trailing_means) if trailing_means else None

    delta_pct = None
    if yesterday_mean is not None and trailing_mean:
        delta_pct = ((yesterday_mean - trailing_mean) / trailing_mean) * 100

    return {
        "market": market,
        "zone": zone,
        "product": product,
        # Registry-derived (shared/units.py), never guessed -- lets every
        # consumer of this stats dict (the zone-summary line, the LLM
        # prompt) label its own figures instead of assuming DKK/MWh, which
        # is wrong for e.g. DK2's FCR price (EUR/MW/h). "unknown" if the
        # registry genuinely has no unit declared for this key (shouldn't
        # happen for anything PRICE_SERIES/SYSTEM_STATE_PRODUCTS actually
        # reads -- tests/test_units.py guards against a shipped "unknown").
        "unit": unit_for(market, zone, product) or "unknown",
        "yesterday_mean": yesterday_mean,
        "yesterday_min": yesterday_rows[0]["min_value"] if yesterday_rows else None,
        "yesterday_max": yesterday_rows[0]["max_value"] if yesterday_rows else None,
        "trailing_30d_mean": trailing_mean,
        "delta_pct_vs_trailing_30d": delta_pct,
    }


def _pull_recap_data(db: DatabaseManager, brief_date: date) -> dict:
    """
    Pulls every stat the recap needs: `PRICE_SERIES` for both `ZONES`, plus
    `SYSTEM_STATE_KEYS` (wind/solar/CO2 at zone="ALL", plus DK2 grid
    inertia). `brief_date` is the date the brief is published *for* -- the
    recap always covers the day before it.
    """
    yesterday = brief_date - timedelta(days=1)
    zone_stats: dict[str, list[dict]] = {zone: [] for zone in ZONES}
    for zone in ZONES:
        for market, product in PRICE_SERIES:
            zone_stats[zone].append(_recap_stats(db, market, zone, product, yesterday))

    system_state_stats = [
        _recap_stats(db, market, zone, product, yesterday)
        for market, zone, product in SYSTEM_STATE_KEYS
    ]

    return {
        "yesterday": yesterday,
        "zone_stats": zone_stats,
        "system_state_stats": system_state_stats,
    }


def _known_numbers(recap_data: dict) -> list[float]:
    # TRAILING_WINDOW_DAYS itself is a known structural constant the prompt
    # explicitly tells the model about ("trailing 30-day average") -- without
    # it, any narrative that mentions the window length (not a data figure)
    # gets falsely rejected as an untraceable citation.
    known: list[float] = [float(TRAILING_WINDOW_DAYS)]
    all_stats = [s for stats in recap_data["zone_stats"].values() for s in stats]
    all_stats += recap_data["system_state_stats"]
    for stat in all_stats:
        for key in ("yesterday_mean", "yesterday_min", "yesterday_max", "trailing_30d_mean"):
            if stat.get(key) is not None:
                known.append(float(stat[key]))
        if stat.get("delta_pct_vs_trailing_30d") is not None:
            known.append(float(stat["delta_pct_vs_trailing_30d"]))
    return known


def _client() -> AsyncAnthropic | None:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning(
            "ANTHROPIC_API_KEY not set; skipping price-recap causal-factor synthesis (the recap "
            "will still be built from the pre-computed stats alone)"
        )
        return None
    return AsyncAnthropic(api_key=api_key)


def _zone_summary_line(zone: str, stats: list[dict]) -> str:
    day_ahead = next((s for s in stats if s["market"] == "day_ahead"), None)
    if day_ahead is None or day_ahead["yesterday_mean"] is None:
        return f"{zone}: no day-ahead price data available for yesterday."
    delta = day_ahead.get("delta_pct_vs_trailing_30d")
    delta_phrase = (
        f"{delta:+.1f}% vs the trailing 30-day average"
        if delta is not None
        else "no 30-day baseline yet"
    )
    return (
        f"{zone}: day-ahead price averaged {day_ahead['yesterday_mean']:.1f} {day_ahead['unit']} "
        f"(range {day_ahead['yesterday_min']:.1f}-{day_ahead['yesterday_max']:.1f}), "
        f"{delta_phrase}."
    )


SYSTEM_PROMPT = """\
You are the causal-explanation engine for a plain-language "morning brief" \
aimed at a non-technical BESS (battery energy storage) operator in the \
Danish ancillary services / day-ahead power markets. You are given a set of \
PRE-COMPUTED statistics (yesterday's price/wind/solar/CO2 figures vs a \
trailing 30-day baseline) for DK1 and DK2. Your job is to explain, in plain \
words, why prices likely moved the way they did.

Produce ONE JSON object -- and nothing else, no markdown fences, no prose \
before or after -- with EXACTLY this shape:

{
  "headline": "<one sentence summarizing yesterday's overall price picture>",
  "causal_factors": [
    "<2-3 sentences, each citing ONLY numbers from the pre-computed stats you \
were given -- e.g. wind output vs its baseline, price vs its baseline -- \
explaining a plausible causal link between them. NEVER invent a number not \
present in the stats you were given.>"
  ]
}

Hard rules:
- EVERY number you write MUST come from the pre-computed stats you were \
given -- never invent, estimate, or infer a number that isn't present there.
- "causal_factors" must have 2 or 3 items.
- If the stats don't support a confident causal story, still return an \
honest, more tentative explanation rather than inventing one.
- Each series below is labelled with its unit in square brackets, e.g. \
"[DKK/MW/h]" or "[EUR/MW/h]". NEVER compare or subtract two numbers with \
different units -- if a DK1 figure and a DK2 figure for the same market/\
product carry different units (this happens: DK1's FCR price is DKK/MW/h, \
DK2's is EUR/MW/h), say so explicitly (e.g. "DK1 and DK2 FCR prices aren't \
directly comparable -- different currencies") rather than explaining the \
apparent gap as a market phenomenon.
"""


def _build_user_prompt(recap_data: dict) -> str:
    lines = [f"Yesterday's date: {recap_data['yesterday']}"]
    for zone, stats in recap_data["zone_stats"].items():
        lines.append(f"\n{zone}:")
        for s in stats:
            lines.append(
                f"- {s['market']}/{s['product']} [{s['unit']}]: "
                f"yesterday mean={s['yesterday_mean']}, "
                f"min={s['yesterday_min']}, max={s['yesterday_max']}, "
                f"trailing_30d_mean={s['trailing_30d_mean']}, "
                f"delta_pct_vs_trailing_30d={s['delta_pct_vs_trailing_30d']}"
            )
    # Not all "system state" entries are zone="ALL" (grid inertia's DK2
    # figure isn't -- see SYSTEM_STATE_KEYS), so each line spells out its own
    # zone rather than a blanket "(zone=ALL)" header that would misdescribe it.
    lines.append("\nSystem state / grid context:")
    for s in recap_data["system_state_stats"]:
        lines.append(
            f"- {s['market']}/{s['zone']}/{s['product']} [{s['unit']}]: "
            f"yesterday mean={s['yesterday_mean']}, "
            f"trailing_30d_mean={s['trailing_30d_mean']}, "
            f"delta_pct_vs_trailing_30d={s['delta_pct_vs_trailing_30d']}"
        )
    lines.append("\nRespond with only the JSON object described in the system prompt.")
    return "\n".join(lines)


def _validate_causal_factors(payload: dict, known_numbers: list[float]) -> str | None:
    if not isinstance(payload, dict):
        return "parsed output is not a JSON object"
    headline = payload.get("headline")
    if not headline:
        return "missing headline"
    if not isinstance(headline, str):
        return "headline is not a string"
    for number in extract_numbers(headline):
        if not number_is_traceable(number, known_numbers):
            return f"headline cites number {number} not traceable to the pre-computed recap stats"
    causal_factors = payload.get("causal_factors")
    if not isinstance(causal_factors, list) or not (1 <= len(causal_factors) <= 3):
        return f"causal_factors must be a list of 1-3 items, got {causal_factors!r}"
    for i, factor in enumerate(causal_factors):
        if not isinstance(factor, str) or not factor:
            return f"causal_factors[{i}] is empty/not a string"
        for number in extract_numbers(factor):
            if not number_is_traceable(number, known_numbers):
                return (
                    f"causal_factors[{i}] cites number {number} not traceable to the pre-computed "
                    "recap stats"
                )
    return None


async def _synthesize_causal_factors(
    recap_data: dict, client: AsyncAnthropic | None = None
) -> dict | None:
    anthropic_client = client if client is not None else _client()
    if anthropic_client is None:
        return None

    user_prompt = _build_user_prompt(recap_data)
    call_start = time.monotonic()
    try:
        response = await anthropic_client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception:
        logger.exception("Claude price-recap causal-factor synthesis call failed")
        LLM_CALL_TOTAL.labels(status="error").inc()
        LLM_CALL_DURATION.observe(time.monotonic() - call_start)
        return None

    LLM_CALL_TOTAL.labels(status="success").inc()
    LLM_CALL_DURATION.observe(time.monotonic() - call_start)

    raw_text = "".join(block.text for block in response.content if block.type == "text")
    payload = extract_json_object(raw_text)
    if payload is None:
        logger.error("Claude returned non-JSON price-recap output")
        return None

    known_numbers = _known_numbers(recap_data)
    rejection_reason = _validate_causal_factors(payload, known_numbers)
    if rejection_reason is not None:
        logger.warning("Rejecting synthesized price recap: %s", rejection_reason)
        RECAP_REJECTED_TOTAL.inc()
        return None

    return payload


async def synthesize_price_recap(
    db: DatabaseManager, brief_date: date, client: AsyncAnthropic | None = None
) -> dict:
    """
    Builds the full price-recap section for the Morning Brief published on
    `brief_date` (covering the day before it). Always returns a dict --
    never raises, never returns None -- since a recap built purely from the
    pre-computed stats (zone summaries) is always possible even if the LLM
    causal-factor call is unavailable/rejected; only the `causal_factors`
    list degrades to an honest empty list in that case.

    Returns `{headline, zone_summaries: [...], causal_factors: [...],
    jargon_glossary: {...}}`.
    """
    recap_data = _pull_recap_data(db, brief_date)
    zone_summaries = [
        _zone_summary_line(zone, stats) for zone, stats in recap_data["zone_stats"].items()
    ]

    synthesized = await _synthesize_causal_factors(recap_data, client=client)
    if synthesized is not None:
        headline = synthesized.get("headline", "")
        causal_factors = synthesized.get("causal_factors", [])
    else:
        headline = (
            f"Price recap for {recap_data['yesterday']} (causal-factor synthesis unavailable -- "
            "see zone summaries for the raw figures)."
        )
        causal_factors = []

    return {
        "headline": headline,
        "zone_summaries": zone_summaries,
        "causal_factors": causal_factors,
        "jargon_glossary": dict(JARGON_GLOSSARY),
    }
