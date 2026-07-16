import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.event_synthesizer import (
    build_event_id,
    infer_direction,
    synthesize_event_report,
)
from shared.rule_engine import Trigger

PRICE_SPIKE_TRIGGER = Trigger(
    trigger_type="price_spike",
    market="mFRR EAM",
    zone="DK1",
    product="up",
    value=4850.0,
    time="2026-07-16 17:15:00+00:00",
    baseline=1200.0,
    threshold=3600.0,
    details="z-score=4.10 over 45 historical point(s)",
    detected_at="2026-07-16T17:20:00+00:00",
)

REVISION_TRIGGER = Trigger(
    trigger_type="revision_alert",
    market="mFRR_capacity",
    zone="DK1",
    product="up",
    value=250.0,
    time="2026-07-16 10:00:00+00:00",
    baseline=100.0,
    threshold=5.0,
    details="revised 100.0 -> 250.0",
    detected_at="2026-07-16T10:30:00+00:00",
)

CONTEXT_WINDOW = [
    {"time": "2026-07-16 11:15:00+00:00", "value": 1150.0, "source": "Energinet"},
    {"time": "2026-07-16 17:15:00+00:00", "value": 4850.0, "source": "Energinet"},
]

RETRIEVED_CLAIMS = [
    {
        "claim": "Analysts point to low wind + Karlshamn unavailability",
        "source": "EnergiWatch, 2026-07-14",
        "claim_type": "theory",
        "retrieved_at": "2026-07-14T20:00:00+00:00",
    }
]


def _mock_client(response_text: str):
    text_block = SimpleNamespace(type="text", text=response_text)
    message = SimpleNamespace(content=[text_block])
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=message)))
    return client


def _valid_report_json() -> str:
    return json.dumps(
        {
            "event_id": "placeholder",
            "market": "placeholder",
            "zone": "placeholder",
            "direction": "placeholder",
            "observation": "Balancing energy price hit 4,850 DKK/MWh vs baseline of 1,200",
            "hard_data_correlates": [
                {"signal": "mFRR EAM price", "value": "4,850 DKK/MWh", "source": "Energinet"}
            ],
            "market_theories": [
                {
                    "claim": "Analysts point to low wind + Karlshamn unavailability",
                    "source": "EnergiWatch, 2026-07-14",
                    "type": "theory",
                }
            ],
            "synthesis": "According to EnergiWatch, low wind output and a Karlshamn outage "
            "coincided with the price move from 1,200 to 4,850 DKK/MWh.",
            "confidence": "medium",
            "data_maturity": "provisional — figures may be revised by Energinet",
        }
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_infer_direction_from_up_down_product():
    assert infer_direction(PRICE_SPIKE_TRIGGER) == "up"


def test_infer_direction_falls_back_to_baseline_comparison():
    t = Trigger(
        trigger_type="negative_or_zero_price",
        market="imbalance",
        zone="DK1",
        product="imbalance_price",
        value=-10.0,
        time="2026-07-16 10:00:00+00:00",
        baseline=0.02,
    )
    assert infer_direction(t) == "down"


def test_build_event_id_is_deterministic():
    id_1 = build_event_id(PRICE_SPIKE_TRIGGER, "up")
    id_2 = build_event_id(PRICE_SPIKE_TRIGGER, "up")
    assert id_1 == id_2
    assert "DK1" in id_1
    assert "mFRR" in id_1


async def test_synthesize_skips_gracefully_without_api_key(caplog):
    with caplog.at_level("WARNING"):
        result = await synthesize_event_report(
            PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS
        )

    assert result is None
    assert "ANTHROPIC_API_KEY not set" in caplog.text


async def test_synthesize_success_produces_readme_section2_shape():
    client = _mock_client(_valid_report_json())

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is not None
    # README §2 exact output contract keys.
    assert set(report.keys()) >= {
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
    assert report["market"] == "mFRR EAM"
    assert report["zone"] == "DK1"
    assert report["direction"] == "up"
    assert report["confidence"] == "medium"
    assert report["hard_data_correlates"][0]["source"] == "Energinet"
    assert report["market_theories"][0]["source"] == "EnergiWatch, 2026-07-14"

    client.messages.create.assert_awaited_once()
    _, kwargs = client.messages.create.call_args
    assert kwargs["model"] == "claude-opus-4-8"


async def test_synthesize_overwrites_llm_echoed_identity_fields():
    """event_id/market/zone/direction are owned by our code, not trusted from the model."""
    tampered = json.loads(_valid_report_json())
    tampered["market"] = "some other market entirely"
    client = _mock_client(json.dumps(tampered))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report["market"] == "mFRR EAM"
    assert report["zone"] == "DK1"


async def test_synthesize_rejects_report_when_theory_missing_source():
    payload = json.loads(_valid_report_json())
    del payload["market_theories"][0]["source"]
    client = _mock_client(json.dumps(payload))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_rejects_report_when_hard_data_correlate_missing_source():
    payload = json.loads(_valid_report_json())
    del payload["hard_data_correlates"][0]["source"]
    client = _mock_client(json.dumps(payload))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_rejects_report_with_untraceable_number():
    payload = json.loads(_valid_report_json())
    payload["hard_data_correlates"][0]["value"] = "99,999 DKK/MWh"
    client = _mock_client(json.dumps(payload))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_rejects_report_missing_required_key():
    payload = json.loads(_valid_report_json())
    del payload["confidence"]
    client = _mock_client(json.dumps(payload))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_rejects_invalid_confidence_value():
    payload = json.loads(_valid_report_json())
    payload["confidence"] = "extremely high"
    client = _mock_client(json.dumps(payload))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_returns_none_on_invalid_json():
    client = _mock_client("not json at all")

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_returns_none_when_api_call_raises():
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("API down")))
    )

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is None


async def test_synthesize_allows_empty_market_theories_and_correlates():
    payload = json.loads(_valid_report_json())
    payload["market_theories"] = []
    payload["hard_data_correlates"] = []
    client = _mock_client(json.dumps(payload))

    report = await synthesize_event_report(
        PRICE_SPIKE_TRIGGER, CONTEXT_WINDOW, RETRIEVED_CLAIMS, client=client
    )

    assert report is not None
    assert report["market_theories"] == []
    assert report["hard_data_correlates"] == []
