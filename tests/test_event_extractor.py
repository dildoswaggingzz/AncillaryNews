import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.event_extractor import TIER2_CONFIDENCE_CAP, extract_events
from shared.rss_reader import ArticleRef

TIER1_ARTICLE = ArticleRef(
    url="https://nordicbalancingmodel.net/prequalification-update",
    title="Energinet publishes FCR-D DK2 prequalification update",
    author="Nordic Balancing Model",
    published="Fri, 03 Jul 2026 07:11:50 +0000",
    feed_name="Nordic Balancing Model",
    feed_tier="tier1",
)

TIER2_ARTICLE = ArticleRef(
    url="https://feeds.example.test/battery-prequalifies",
    title="Analyst: new battery prequalifies into FCR-D DK2",
    author="Jane Doe",
    published="Tue, 14 Jul 2026 20:00:00 +0000",
    feed_name="EnergyWatch",
    feed_tier="tier2",
)


def _mock_client(response_text: str):
    text_block = SimpleNamespace(type="text", text=response_text)
    message = SimpleNamespace(content=[text_block])
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=message)))
    return client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


# --- ANTHROPIC_API_KEY-missing precedent (verbatim from claim_extractor.py) --


async def test_extract_events_skips_gracefully_without_api_key(caplog):
    with caplog.at_level("WARNING"):
        result = await extract_events("some article text", TIER1_ARTICLE)

    assert result is None
    assert "ANTHROPIC_API_KEY not set" in caplog.text


async def test_extract_events_returns_none_when_api_call_raises():
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("API down")))
    )

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result is None


async def test_extract_events_returns_none_on_invalid_json():
    client = _mock_client("not json at all")

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result is None


# --- happy path / parsing --------------------------------------------------


async def test_extract_events_parses_a_full_event():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "prequalification",
                    "market": "FCR",
                    "zone": "DK2",
                    "direction": "up",
                    "magnitude_mw": 20.0,
                    "effective_from": "2026-09-01",
                    "confidence": 0.9,
                    "raw_excerpt": "The 20 MW battery was prequalified for FCR-D up in DK2.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result is not None
    assert len(result) == 1
    event = result[0]
    assert event.event_type == "prequalification"
    assert event.market == "FCR"
    assert event.zone == "DK2"
    assert event.direction == "up"
    assert event.magnitude_mw == 20.0
    assert event.effective_from == date(2026, 9, 1)
    assert event.confidence == 0.9
    assert "prequalified" in event.raw_excerpt
    client.messages.create.assert_awaited_once()
    _, kwargs = client.messages.create.call_args
    assert kwargs["model"] == "claude-haiku-4-5"


async def test_extract_events_returns_empty_list_when_none_found():
    client = _mock_client(json.dumps({"events": []}))

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result == []


async def test_extract_events_parses_markdown_fenced_response():
    inner = json.dumps(
        {
            "events": [
                {
                    "event_type": "outage",
                    "market": None,
                    "zone": "DK1",
                    "direction": None,
                    "magnitude_mw": None,
                    "effective_from": None,
                    "confidence": 0.7,
                    "raw_excerpt": "An unplanned outage hit a DK1 generator on Tuesday.",
                }
            ]
        },
        indent=2,
    )
    response_text = f"```json\n{inner}\n```"
    client = _mock_client(response_text)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result is not None
    assert result[0].event_type == "outage"
    assert result[0].magnitude_mw is None


# --- magnitude / effective_from: null rather than a guess (design §3) ------


async def test_magnitude_and_effective_from_null_when_not_stated():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "capacity_commissioning",
                    "market": "aFRR",
                    "zone": "DK1",
                    "direction": None,
                    "magnitude_mw": None,
                    "effective_from": None,
                    "confidence": 0.6,
                    "raw_excerpt": "New capacity is expected to come online later this year.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result[0].magnitude_mw is None
    assert result[0].effective_from is None


async def test_magnitude_non_numeric_never_coerced_defaults_to_none():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "prequalification",
                    "market": "FCR",
                    "zone": "DK2",
                    "direction": "up",
                    "magnitude_mw": "a lot",  # a hallucination-shaped non-numeric value
                    "effective_from": "not a date",
                    "confidence": 0.8,
                    "raw_excerpt": "A large battery prequalified.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result[0].magnitude_mw is None
    assert result[0].effective_from is None


async def test_event_missing_raw_excerpt_is_dropped():
    """Design §2: every event must be traceable to source text -- untraceable events are dropped."""
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "prequalification",
                    "market": "FCR",
                    "zone": "DK2",
                    "direction": "up",
                    "magnitude_mw": 10.0,
                    "effective_from": None,
                    "confidence": 0.8,
                    "raw_excerpt": "",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result == []


async def test_unrecognised_event_type_defaults_to_other():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "something_new",
                    "market": None,
                    "zone": None,
                    "direction": None,
                    "magnitude_mw": None,
                    "effective_from": None,
                    "confidence": 0.5,
                    "raw_excerpt": "Something happened.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result[0].event_type == "other"


# --- Tier-2 confidence cap (design §3's two-tier trust model) --------------


async def test_tier2_source_confidence_capped():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "prequalification",
                    "market": "FCR",
                    "zone": "DK2",
                    "direction": "up",
                    "magnitude_mw": 15.0,
                    "effective_from": None,
                    "confidence": 0.95,  # model's own high confidence
                    "raw_excerpt": "A new battery reportedly prequalified.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER2_ARTICLE, client=client)

    assert result[0].confidence == TIER2_CONFIDENCE_CAP
    assert result[0].confidence < 0.95


async def test_tier2_source_confidence_already_below_cap_is_left_alone():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "prequalification",
                    "market": "FCR",
                    "zone": "DK2",
                    "direction": "up",
                    "magnitude_mw": 15.0,
                    "effective_from": None,
                    "confidence": 0.2,
                    "raw_excerpt": "A new battery reportedly prequalified.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER2_ARTICLE, client=client)

    assert result[0].confidence == 0.2


async def test_tier1_source_confidence_never_capped():
    response_json = json.dumps(
        {
            "events": [
                {
                    "event_type": "prequalification",
                    "market": "FCR",
                    "zone": "DK2",
                    "direction": "up",
                    "magnitude_mw": 15.0,
                    "effective_from": None,
                    "confidence": 0.95,
                    "raw_excerpt": "Energinet confirmed the prequalification.",
                }
            ]
        }
    )
    client = _mock_client(response_json)

    result = await extract_events("article text", TIER1_ARTICLE, client=client)

    assert result[0].confidence == 0.95


# --- known_at is never assigned by this module (design §1/§3) --------------


async def test_extracted_event_carries_no_known_at_or_storage_fields():
    """
    Design §1/§3: the model must never be trusted to date events; `known_at`
    is assigned by the crawler from `ArticleRef.published`, not by this
    module. `ExtractedEvent` structurally cannot carry a `known_at` (or
    `event_id`/`source_*`/`extracted_at`) field at all.
    """
    from shared.event_extractor import ExtractedEvent

    field_names = {f for f in ExtractedEvent.__dataclass_fields__}
    assert "known_at" not in field_names
    assert "event_id" not in field_names
    assert "extracted_at" not in field_names


# --- metrics -----------------------------------------------------------------


def test_metrics_are_registered_and_exposition_includes_expected_names():
    from prometheus_client import generate_latest

    output = generate_latest().decode()

    assert "crawler_event_llm_calls_total" in output
    assert "crawler_event_llm_call_duration_seconds" in output


async def test_extract_events_success_increments_llm_call_success_counter():
    from shared.event_extractor import EVENT_EXTRACTION_LLM_CALL_TOTAL

    before = EVENT_EXTRACTION_LLM_CALL_TOTAL.labels(status="success")._value.get()
    client = _mock_client(json.dumps({"events": []}))

    await extract_events("article text", TIER1_ARTICLE, client=client)

    after = EVENT_EXTRACTION_LLM_CALL_TOTAL.labels(status="success")._value.get()
    assert after == before + 1


async def test_extract_events_api_failure_increments_llm_call_error_counter():
    from shared.event_extractor import EVENT_EXTRACTION_LLM_CALL_TOTAL

    before = EVENT_EXTRACTION_LLM_CALL_TOTAL.labels(status="error")._value.get()
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("API down")))
    )

    await extract_events("article text", TIER1_ARTICLE, client=client)

    after = EVENT_EXTRACTION_LLM_CALL_TOTAL.labels(status="error")._value.get()
    assert after == before + 1
