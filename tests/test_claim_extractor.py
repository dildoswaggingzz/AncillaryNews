import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.claim_extractor import extract_claims
from shared.rss_reader import ArticleRef

TIER1_ARTICLE = ArticleRef(
    url="https://nordicbalancingmodel.net/some-update",
    title="NBM publishes MARI accession update",
    author="Nordic Balancing Model",
    published="Fri, 03 Jul 2026 07:11:50 +0000",
    feed_name="Nordic Balancing Model",
    feed_tier="tier1",
)

TIER2_ARTICLE = ArticleRef(
    url="https://feeds.example.test/analyst-take",
    title="Analyst: wind shortfall drove DK1 mFRR prices up",
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


async def test_extract_claims_skips_gracefully_without_api_key(caplog):
    with caplog.at_level("WARNING"):
        result = await extract_claims("some article text", TIER1_ARTICLE)

    assert result is None
    assert "ANTHROPIC_API_KEY not set" in caplog.text


async def test_extract_claims_parses_fact_theory_forecast_typing():
    response_json = json.dumps(
        {
            "summary": "NBM confirms the Nordic mFRR MARI accession timeline.",
            "claims": [
                {
                    "claim": "The four Nordic TSOs still target Q1 2027 for MARI accession.",
                    "claim_type": "fact",
                },
                {
                    "claim": "Analysts expect the transition to reduce cross-border price spread.",
                    "claim_type": "theory",
                },
                {
                    "claim": "Prices may spike briefly during platform cutover.",
                    "claim_type": "forecast",
                },
            ],
        }
    )
    client = _mock_client(response_json)

    result = await extract_claims("article text", TIER1_ARTICLE, client=client)

    assert result is not None
    assert result.summary.startswith("NBM confirms")
    assert [c.claim_type for c in result.claims] == ["fact", "theory", "forecast"]
    client.messages.create.assert_awaited_once()
    _, kwargs = client.messages.create.call_args
    assert kwargs["model"] == "claude-haiku-4-5"


async def test_extract_claims_downgrades_tier2_fact_to_theory():
    """README §6: Tier 2 (media/analyst) claims are never asserted as bare fact."""
    response_json = json.dumps(
        {
            "summary": "Analyst commentary on the DK1 price spike.",
            "claims": [
                {
                    "claim": "DK1 balancing prices hit 4,850 DKK/MWh on Tuesday.",
                    "claim_type": "fact",
                },
            ],
        }
    )
    client = _mock_client(response_json)

    result = await extract_claims("article text", TIER2_ARTICLE, client=client)

    assert result is not None
    assert result.claims[0].claim_type == "theory"


async def test_extract_claims_defaults_unrecognised_claim_type_to_theory():
    response_json = json.dumps(
        {
            "summary": "Summary.",
            "claims": [{"claim": "Something happened.", "claim_type": "speculation"}],
        }
    )
    client = _mock_client(response_json)

    result = await extract_claims("article text", TIER1_ARTICLE, client=client)

    assert result.claims[0].claim_type == "theory"


async def test_extract_claims_returns_none_on_invalid_json():
    client = _mock_client("not json at all")

    result = await extract_claims("article text", TIER1_ARTICLE, client=client)

    assert result is None


async def test_extract_claims_returns_empty_claims_when_none_found():
    response_json = json.dumps({"summary": "Nothing balancing-market related here.", "claims": []})
    client = _mock_client(response_json)

    result = await extract_claims("article text", TIER1_ARTICLE, client=client)

    assert result is not None
    assert result.claims == []


async def test_extract_claims_returns_none_when_api_call_raises():
    client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("API down")))
    )

    result = await extract_claims("article text", TIER1_ARTICLE, client=client)

    assert result is None
