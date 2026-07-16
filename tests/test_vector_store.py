import uuid
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


async def test_raw_article_and_claim_ids_are_derived_from_url_deterministically():
    """Re-crawling produces the same raw-article point ID as before (upsert semantics)."""
    from shared.vector_store import _raw_point_id

    id_1 = _raw_point_id(ARTICLE.url)
    id_2 = _raw_point_id(ARTICLE.url)

    assert id_1 == id_2
    uuid.UUID(id_1)
