import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.forecast_synthesizer import (
    REFRESH_INTERVALS,
    get_or_refresh_forecast,
    synthesize_forecast,
)


def _mock_client(response_text: str):
    text_block = SimpleNamespace(type="text", text=response_text)
    message = SimpleNamespace(content=[text_block])
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=message)))


def _valid_forecast_json(horizon: str = "month") -> str:
    return json.dumps(
        {
            "horizon": horizon,
            "narrative": (
                "Prices are likely to stay roughly flat, with modest upside if wind drops."
            ),
            "confidence": "medium",
            "swing_factors": ["wind output", "gas prices"],
        }
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _db_with_no_history():
    db = MagicMock()
    db.fetch_daily_aggregates.return_value = []
    return db


# --- synthesize_forecast: validation ------------------------------------------


async def test_synthesize_forecast_returns_none_without_api_key():
    db_context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}
    result = await synthesize_forecast("month", db_context, client=None)
    assert result is None


async def test_synthesize_forecast_rejects_unknown_horizon():
    with pytest.raises(ValueError, match="unknown forecast horizon"):
        await synthesize_forecast("decade", {}, client=_mock_client(_valid_forecast_json()))


async def test_synthesize_forecast_accepts_valid_response():
    client = _mock_client(_valid_forecast_json("month"))
    context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}

    forecast = await synthesize_forecast("month", context, client=client)

    assert forecast is not None
    assert forecast["horizon"] == "month"
    assert forecast["confidence"] == "medium"
    assert forecast["swing_factors"] == ["wind output", "gas prices"]


async def test_synthesize_forecast_rejects_missing_keys():
    client = _mock_client(json.dumps({"narrative": "x", "confidence": "low"}))
    context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}

    forecast = await synthesize_forecast("month", context, client=client)

    assert forecast is None


async def test_synthesize_forecast_rejects_bare_numeric_price_prediction():
    payload = json.dumps(
        {
            "horizon": "month",
            "narrative": "Prices will be 450 DKK/MWh next month.",
            "confidence": "high",
            "swing_factors": ["wind output"],
        }
    )
    client = _mock_client(payload)
    context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}

    forecast = await synthesize_forecast("month", context, client=client)

    assert forecast is None


async def test_synthesize_forecast_accepts_hedged_numeric_reference():
    payload = json.dumps(
        {
            "horizon": "month",
            "narrative": "Prices could stay around 450 DKK/MWh next month if wind holds up.",
            "confidence": "medium",
            "swing_factors": ["wind output"],
        }
    )
    client = _mock_client(payload)
    context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}

    forecast = await synthesize_forecast("month", context, client=client)

    assert forecast is not None


async def test_synthesize_forecast_rejects_wrong_swing_factor_count():
    payload = json.dumps(
        {
            "horizon": "month",
            "narrative": "Outlook is mixed.",
            "confidence": "low",
            "swing_factors": ["a", "b", "c"],
        }
    )
    client = _mock_client(payload)
    context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}

    forecast = await synthesize_forecast("month", context, client=client)

    assert forecast is None


async def test_synthesize_forecast_returns_none_on_api_failure():
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("API down")))
    )
    context = {"zone": "DK1", "window_days": 90, "as_of": "2026-07-17", "daily_aggregates": []}

    forecast = await synthesize_forecast("month", context, client=client)

    assert forecast is None


# --- get_or_refresh_forecast: cache hit/miss/stale/fallback -------------------


async def test_get_or_refresh_forecast_returns_cache_hit_without_calling_llm():
    db = _db_with_no_history()
    now = datetime.now(UTC)
    db.fetch_latest_forecast.return_value = {
        "id": 1,
        "horizon": "month",
        "generated_at": now - timedelta(days=1),
        "valid_until": now + timedelta(days=6),
        "forecast": {"narrative": "cached", "confidence": "medium", "swing_factors": ["x"]},
    }
    client = _mock_client(_valid_forecast_json("month"))

    result = await get_or_refresh_forecast(db, "month", client=client)

    assert result["forecast"]["narrative"] == "cached"
    client.messages.create.assert_not_awaited()
    db.save_forecast.assert_not_called()


async def test_get_or_refresh_forecast_refreshes_stale_cache():
    db = _db_with_no_history()
    now = datetime.now(UTC)
    db.fetch_latest_forecast.return_value = {
        "id": 1,
        "horizon": "month",
        "generated_at": now - timedelta(days=10),
        "valid_until": now - timedelta(days=3),  # stale
        "forecast": {"narrative": "old", "confidence": "medium", "swing_factors": ["x"]},
    }
    db.save_forecast.return_value = 99
    client = _mock_client(_valid_forecast_json("month"))

    result = await get_or_refresh_forecast(db, "month", client=client)

    client.messages.create.assert_awaited_once()
    db.save_forecast.assert_called_once()
    assert result["id"] == 99
    assert "wind output" in result["forecast"]["swing_factors"]


async def test_get_or_refresh_forecast_synthesizes_when_no_cache_exists():
    db = _db_with_no_history()
    db.fetch_latest_forecast.return_value = None
    db.save_forecast.return_value = 5
    client = _mock_client(_valid_forecast_json("year"))

    result = await get_or_refresh_forecast(db, "year", client=client)

    assert result["id"] == 5
    db.save_forecast.assert_called_once()
    args = db.save_forecast.call_args.args
    assert args[0] == "year"


async def test_get_or_refresh_forecast_falls_back_to_stale_cache_on_missing_api_key():
    db = _db_with_no_history()
    now = datetime.now(UTC)
    db.fetch_latest_forecast.return_value = {
        "id": 1,
        "horizon": "month",
        "generated_at": now - timedelta(days=10),
        "valid_until": now - timedelta(days=3),  # stale
        "forecast": {
            "narrative": "old but good enough",
            "confidence": "medium",
            "swing_factors": ["x"],
        },
    }

    result = await get_or_refresh_forecast(db, "month", client=None)

    assert result["forecast"]["narrative"] == "old but good enough"
    db.save_forecast.assert_not_called()


async def test_get_or_refresh_forecast_returns_none_when_no_cache_and_no_api_key():
    db = _db_with_no_history()
    db.fetch_latest_forecast.return_value = None

    result = await get_or_refresh_forecast(db, "month", client=None)

    assert result is None


def test_refresh_intervals_match_confirmed_product_decision():
    assert REFRESH_INTERVALS["month"] == timedelta(days=7)
    assert REFRESH_INTERVALS["quarter"] == timedelta(days=7)
    assert REFRESH_INTERVALS["year"] == timedelta(days=30)
