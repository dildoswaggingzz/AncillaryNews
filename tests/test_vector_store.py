import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from shared.claim_extractor import ExtractedClaim
from shared.rss_reader import ArticleRef
from shared.vector_store import COLLECTION_NAME, EMBEDDING_DIM, QdrantStore

ARTICLE = ArticleRef(
    url="https://nordicbalancingmodel.net/some-update",
    title="NBM publishes MARI accession update",
    author="Nordic Balancing Model",
    published="Fri, 03 Jul 2026 07:11:50 +0000",
    feed_name="Nordic Balancing Model",
    feed_tier="tier1",
)


def _fake_embedder():
    embedder = MagicMock()
    embedder.embed.side_effect = lambda texts: iter([np.zeros(EMBEDDING_DIM) for _ in texts])
    return embedder


@pytest.fixture
def qdrant_client():
    client = MagicMock()
    client.collection_exists = AsyncMock(return_value=False)
    client.create_collection = AsyncMock()
    client.upsert = AsyncMock()
    client.scroll = AsyncMock(return_value=([], None))
    client.query_points = AsyncMock()
    return client


@pytest.fixture
def store(qdrant_client):
    return QdrantStore(qdrant_client, embedder=_fake_embedder())


async def test_ensure_collection_creates_when_missing(store, qdrant_client):
    await store.ensure_collection()

    qdrant_client.create_collection.assert_awaited_once()
    _, kwargs = qdrant_client.create_collection.call_args
    assert kwargs["collection_name"] == COLLECTION_NAME
    assert kwargs["vectors_config"].size == EMBEDDING_DIM


async def test_ensure_collection_skips_when_present(store, qdrant_client):
    qdrant_client.collection_exists = AsyncMock(return_value=True)

    await store.ensure_collection()

    qdrant_client.create_collection.assert_not_awaited()


async def test_is_processed_true_when_points_found(store, qdrant_client):
    qdrant_client.scroll = AsyncMock(return_value=([MagicMock()], None))

    assert await store.is_processed(ARTICLE.url) is True


async def test_is_processed_false_when_no_points(store, qdrant_client):
    assert await store.is_processed(ARTICLE.url) is False


async def test_is_processed_filters_on_article_url(store, qdrant_client):
    await store.is_processed(ARTICLE.url)

    _, kwargs = qdrant_client.scroll.call_args
    assert kwargs["collection_name"] == COLLECTION_NAME
    condition = kwargs["scroll_filter"].must[0]
    assert condition.key == "article_url"
    assert condition.match.value == ARTICLE.url


async def test_upsert_claims_payload_shape(store, qdrant_client):
    claims = [
        ExtractedClaim(claim="MARI accession still targets Q1 2027.", claim_type="fact"),
        ExtractedClaim(claim="Analysts expect smoother price convergence.", claim_type="theory"),
    ]

    saved = await store.upsert_claims(ARTICLE, claims)

    assert saved == 2
    qdrant_client.upsert.assert_awaited_once()
    _, kwargs = qdrant_client.upsert.call_args
    assert kwargs["collection_name"] == COLLECTION_NAME
    points = kwargs["points"]
    assert len(points) == 2

    first_payload = points[0].payload
    assert first_payload["source"] == "Nordic Balancing Model"
    assert first_payload["author"] == "Nordic Balancing Model"
    assert first_payload["claim_type"] == "fact"
    assert first_payload["article_url"] == ARTICLE.url
    assert first_payload["article_title"] == ARTICLE.title
    assert first_payload["claim"] == claims[0].claim
    assert "retrieved_at" in first_payload

    # IDs must be deterministic, derived from the article URL, and valid UUIDs.
    uuid.UUID(points[0].id)
    assert points[0].id != points[1].id


async def test_upsert_claims_is_deterministic_across_calls(store, qdrant_client):
    """Re-crawling the same article regenerates identical point IDs -> upsert, not duplicate."""
    claims = [ExtractedClaim(claim="Same claim text.", claim_type="fact")]

    await store.upsert_claims(ARTICLE, claims)
    first_id = qdrant_client.upsert.call_args.kwargs["points"][0].id

    await store.upsert_claims(ARTICLE, claims)
    second_id = qdrant_client.upsert.call_args.kwargs["points"][0].id

    assert first_id == second_id


async def test_upsert_claims_no_op_for_empty_claims(store, qdrant_client):
    saved = await store.upsert_claims(ARTICLE, [])

    assert saved == 0
    qdrant_client.upsert.assert_not_awaited()


async def test_upsert_raw_article_payload_shape(store, qdrant_client):
    await store.upsert_raw_article(ARTICLE, "full raw article text")

    qdrant_client.upsert.assert_awaited_once()
    _, kwargs = qdrant_client.upsert.call_args
    point = kwargs["points"][0]
    assert point.payload["claim_type"] == "raw"
    assert point.payload["claim"] == "full raw article text"
    assert point.payload["article_url"] == ARTICLE.url
    uuid.UUID(point.id)


def _scored_point(payload: dict):
    point = MagicMock()
    point.payload = payload
    return point


async def test_search_claims_embeds_query_and_returns_payloads(store, qdrant_client):
    payload = {
        "source": "EnergyWatch",
        "claim": "Wind shortfall drove DK1 prices up.",
        "claim_type": "theory",
        "retrieved_at": "2026-07-16T09:00:00+00:00",
    }
    qdrant_client.query_points.return_value = MagicMock(points=[_scored_point(payload)])

    results = await store.search_claims("DK1 mFRR price up")

    assert results == [payload]
    qdrant_client.query_points.assert_awaited_once()
    _, kwargs = qdrant_client.query_points.call_args
    assert kwargs["collection_name"] == COLLECTION_NAME
    assert kwargs["limit"] == 5
    assert kwargs["query_filter"] is None


async def test_search_claims_applies_time_filter_when_window_given(store, qdrant_client):
    qdrant_client.query_points.return_value = MagicMock(points=[])
    time_from = datetime(2026, 7, 16, 0, 0, tzinfo=UTC)
    time_to = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)

    await store.search_claims("DK1 mFRR price up", time_from=time_from, time_to=time_to, limit=3)

    _, kwargs = qdrant_client.query_points.call_args
    assert kwargs["limit"] == 3
    query_filter = kwargs["query_filter"]
    assert query_filter is not None
    condition = query_filter.must[0]
    assert condition.key == "retrieved_at"
    assert condition.range.gte == time_from
    assert condition.range.lte == time_to


async def test_search_claims_returns_empty_list_when_no_matches(store, qdrant_client):
    qdrant_client.query_points.return_value = MagicMock(points=[])

    results = await store.search_claims("no relevant claims exist for this query")

    assert results == []


async def test_scroll_by_source_filters_and_sorts_most_recent_first(store, qdrant_client):
    older = MagicMock(
        payload={
            "source": "LinkedIn",
            "claim": "older",
            "retrieved_at": "2026-07-01T00:00:00+00:00",
        }
    )
    newer = MagicMock(
        payload={
            "source": "LinkedIn",
            "claim": "newer",
            "retrieved_at": "2026-07-15T00:00:00+00:00",
        }
    )
    qdrant_client.scroll = AsyncMock(return_value=([older, newer], None))

    results = await store.scroll_by_source("LinkedIn", limit=100)

    assert [r["claim"] for r in results] == ["newer", "older"]
    _, kwargs = qdrant_client.scroll.call_args
    assert kwargs["collection_name"] == COLLECTION_NAME
    assert kwargs["limit"] == 100
    condition = kwargs["scroll_filter"].must[0]
    assert condition.key == "source"
    assert condition.match.value == "LinkedIn"


async def test_scroll_by_source_returns_empty_list_when_no_matches(store, qdrant_client):
    results = await store.scroll_by_source("LinkedIn")

    assert results == []


async def test_raw_article_and_claim_ids_are_derived_from_url_deterministically():
    """Re-crawling produces the same raw-article point ID as before (upsert semantics)."""
    from shared.vector_store import _raw_point_id

    id_1 = _raw_point_id(ARTICLE.url)
    id_2 = _raw_point_id(ARTICLE.url)

    assert id_1 == id_2
    uuid.UUID(id_1)
