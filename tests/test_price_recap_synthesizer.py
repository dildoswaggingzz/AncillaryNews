import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.price_recap_synthesizer import (
    TRAILING_WINDOW_DAYS,
    _pull_recap_data,
    synthesize_price_recap,
)

BRIEF_DATE = date(2026, 7, 17)
YESTERDAY = BRIEF_DATE - timedelta(days=1)


def _mock_client(response_text: str):
    text_block = SimpleNamespace(type="text", text=response_text)
    message = SimpleNamespace(content=[text_block])
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=message)))


def _fetch_daily_aggregates(market, zone, product, start_time, end_time):
    """A deterministic fake: yesterday's single-day window (1 day span) gets a
    higher value than the trailing-30-day window (30 day span), so every
    series has a clear "yesterday vs trailing" delta to test against."""
    span_days = (end_time - start_time).days
    if span_days <= 1:
        return [
            {
                "day": start_time,
                "mean_value": 500.0,
                "min_value": 400.0,
                "max_value": 600.0,
                "sample_count": 24,
            }
        ]
    return [
        {
            "day": start_time + timedelta(days=i),
            "mean_value": 400.0,
            "min_value": 300.0,
            "max_value": 500.0,
            "sample_count": 24,
        }
        for i in range(30)
    ]


@pytest.fixture
def db():
    mock = MagicMock()
    mock.fetch_daily_aggregates.side_effect = _fetch_daily_aggregates
    return mock


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_pull_recap_data_covers_both_zones_and_system_state(db):
    recap_data = _pull_recap_data(db, BRIEF_DATE)

    assert recap_data["yesterday"] == YESTERDAY
    assert set(recap_data["zone_stats"].keys()) == {"DK1", "DK2"}
    assert len(recap_data["zone_stats"]["DK1"]) == 3  # day_ahead, FCR, aFRR_capacity
    assert len(recap_data["system_state_stats"]) == 4  # onshore/offshore wind, solar, co2


def test_pull_recap_data_computes_delta_vs_trailing_baseline(db):
    recap_data = _pull_recap_data(db, BRIEF_DATE)

    day_ahead_stat = next(s for s in recap_data["zone_stats"]["DK1"] if s["market"] == "day_ahead")
    assert day_ahead_stat["yesterday_mean"] == 500.0
    assert day_ahead_stat["trailing_30d_mean"] == 400.0
    assert day_ahead_stat["delta_pct_vs_trailing_30d"] == pytest.approx(25.0)


async def test_synthesize_price_recap_without_api_key_still_returns_zone_summaries(db):
    recap = await synthesize_price_recap(db, BRIEF_DATE, client=None)

    assert recap["causal_factors"] == []
    assert len(recap["zone_summaries"]) == 2
    assert "DK1" in recap["zone_summaries"][0]
    assert recap["jargon_glossary"]  # non-empty


async def test_synthesize_price_recap_accepts_valid_causal_factors(db):
    payload = json.dumps(
        {
            "headline": "Prices rose yesterday.",
            "causal_factors": [
                "The day-ahead price averaged 500.0 DKK/MWh, up from a trailing average of "
                "400.0 DKK/MWh."
            ],
        }
    )
    client = _mock_client(payload)

    recap = await synthesize_price_recap(db, BRIEF_DATE, client=client)

    assert recap["headline"] == "Prices rose yesterday."
    assert len(recap["causal_factors"]) == 1


async def test_synthesize_price_recap_accepts_window_length_reference(db):
    # TRAILING_WINDOW_DAYS here refers to the trailing-window length (a known
    # structural constant the model was told about), not a fabricated data
    # figure -- this must NOT be rejected as an untraceable citation.
    payload = json.dumps(
        {
            "headline": f"Prices rose yesterday versus the trailing {TRAILING_WINDOW_DAYS}-day "
            "average.",
            "causal_factors": [
                f"The day-ahead price averaged 500.0 DKK/MWh, up from its trailing "
                f"{TRAILING_WINDOW_DAYS}-day average of 400.0 DKK/MWh."
            ],
        }
    )
    client = _mock_client(payload)

    recap = await synthesize_price_recap(db, BRIEF_DATE, client=client)

    assert recap["headline"] == (
        f"Prices rose yesterday versus the trailing {TRAILING_WINDOW_DAYS}-day average."
    )
    assert len(recap["causal_factors"]) == 1


async def test_synthesize_price_recap_rejects_fabricated_number(db):
    payload = json.dumps(
        {
            "headline": "Prices spiked yesterday.",
            "causal_factors": [
                "DK1 day-ahead price hit a record 9,999 DKK/MWh, the highest ever recorded."
            ],
        }
    )
    client = _mock_client(payload)

    recap = await synthesize_price_recap(db, BRIEF_DATE, client=client)

    # The fabricated 9,999 figure isn't traceable to any pulled stat -- the
    # whole causal-factor synthesis is rejected, falling back to an empty list.
    assert recap["causal_factors"] == []


async def test_synthesize_price_recap_rejects_fabricated_number_in_headline(db):
    payload = json.dumps(
        {
            "headline": "Prices hit a jaw-dropping 999999 DKK/MWh yesterday, an all-time record.",
            "causal_factors": [
                "The day-ahead price averaged 500.0 DKK/MWh, up from a trailing average of "
                "400.0 DKK/MWh."
            ],
        }
    )
    client = _mock_client(payload)

    recap = await synthesize_price_recap(db, BRIEF_DATE, client=client)

    # A fabricated number in the headline must reject the whole synthesized
    # payload (headline included), not just causal_factors -- the headline
    # is rendered as-is into the Slack/email brief, so it needs the same
    # citation-traceability guarantee.
    assert "999999" not in recap["headline"]
    assert recap["causal_factors"] == []


async def test_synthesize_price_recap_returns_none_causal_factors_on_api_failure(db):
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("API down")))
    )

    recap = await synthesize_price_recap(db, BRIEF_DATE, client=client)

    assert recap["causal_factors"] == []
    assert recap["zone_summaries"]  # zone summaries still built from pure stats
