"""
Qdrant storage for the M3 Insight Crawler (README §3B / §6: "metadata on
everything").

Embeddings: `fastembed` (BAAI/bge-small-en-v1.5, 384-dim, ONNX-based) rather
than `sentence-transformers`. Both are "self-hosted, run entirely inside the
crawler container" options consistent with README §3B's own reasoning for
choosing Qdrant over a managed vector DB ("keep the whole stack reproducible
locally"); fastembed was chosen over sentence-transformers specifically
because it has no torch dependency (onnxruntime only), which keeps the
crawler image meaningfully smaller and its startup faster — and it's the
embedding library Qdrant itself ships/recommends for exactly this pairing.
The chosen model is small (~130MB) and downloads once from HuggingFace on
first use, then caches on disk; see services/crawler/Dockerfile / M3 report
for the tradeoffs of not baking the model into the image.

Every point ID is deterministic, derived from the article URL via `uuid5`
(a stable hash — UUIDv5 over the fixed `NAMESPACE_URL` namespace — rather
than a raw truncated hash, because Qdrant point IDs must be an unsigned int
or a UUID). One article can yield multiple claims, so per-claim IDs are
`uuid5(NAMESPACE_URL, f"{url}#claim:{i}")`; re-crawling the same article
regenerates the same IDs and therefore *upserts* (overwrites) rather than
duplicating. The "no API key" raw-storage fallback uses a single point per
article at `uuid5(NAMESPACE_URL, f"{url}#raw")`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastembed import TextEmbedding
from qdrant_client import AsyncQdrantClient, models

from shared.claim_extractor import ExtractedClaim
from shared.rss_reader import ArticleRef

logger = logging.getLogger(__name__)

COLLECTION_NAME = "crawler_claims"
EMBEDDING_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


@dataclass(frozen=True)
class ClaimPayload:
    """The exact payload shape stored on every Qdrant point (README §6)."""

    source: str
    author: str | None
    retrieved_at: str
    claim_type: str  # "fact" | "theory" | "forecast" | "raw"
    article_url: str
    article_title: str
    claim: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "author": self.author,
            "retrieved_at": self.retrieved_at,
            "claim_type": self.claim_type,
            "article_url": self.article_url,
            "article_title": self.article_title,
            "claim": self.claim,
        }


def _claim_point_id(url: str, index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#claim:{index}"))


def _raw_point_id(url: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{url}#raw"))


class QdrantStore:
    """Thin wrapper around AsyncQdrantClient + fastembed for the crawler's needs."""

    def __init__(self, client: AsyncQdrantClient, embedder: TextEmbedding | None = None):
        self._client = client
        self._embedder = embedder

    @property
    def embedder(self) -> TextEmbedding:
        # Lazy: constructing TextEmbedding downloads/loads the ONNX model,
        # which we don't want to pay for at import time or in tests that
        # never need a real embedding.
        if self._embedder is None:
            self._embedder = TextEmbedding(model_name=EMBEDDING_MODEL_NAME)
        return self._embedder

    def _embed(self, text: str) -> list[float]:
        return next(iter(self.embedder.embed([text]))).tolist()

    async def ensure_collection(self) -> None:
        """Creates the claims collection if it doesn't already exist. Idempotent."""
        if await self._client.collection_exists(COLLECTION_NAME):
            return
        await self._client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(size=EMBEDDING_DIM, distance=models.Distance.COSINE),
        )
        logger.info("Created Qdrant collection %s", COLLECTION_NAME)

    async def is_processed(self, url: str) -> bool:
        """
        True if `url` already has at least one point in the collection —
        the crawl-cycle dedup check (README M3 brief point 1): re-runs skip
        articles already crawled instead of re-fetching/re-extracting/
        re-calling Claude for them.
        """
        points, _ = await self._client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="article_url", match=models.MatchValue(value=url))]
            ),
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(points) > 0

    async def upsert_claims(self, article: ArticleRef, claims: list[ExtractedClaim]) -> int:
        """Embeds and upserts one point per extracted claim. Returns the number stored."""
        if not claims:
            return 0

        retrieved_at = datetime.now(UTC).isoformat()
        points = []
        for i, claim in enumerate(claims):
            payload = ClaimPayload(
                source=article.feed_name,
                author=article.author,
                retrieved_at=retrieved_at,
                claim_type=claim.claim_type,
                article_url=article.url,
                article_title=article.title,
                claim=claim.claim,
            )
            points.append(
                models.PointStruct(
                    id=_claim_point_id(article.url, i),
                    vector=self._embed(claim.claim),
                    payload=payload.to_dict(),
                )
            )

        await self._client.upsert(collection_name=COLLECTION_NAME, points=points)
        return len(points)

    async def search_claims(
        self,
        query: str,
        time_from: datetime | None = None,
        time_to: datetime | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """
        RAG retrieval (README §3C step 3): semantic search over the claims
        collection for `query`, embedded with the same fastembed model used
        for storage, optionally restricted to points whose `retrieved_at`
        falls within `[time_from, time_to]`.

        Claim payloads carry no explicit `market`/`zone` field (see
        `ClaimPayload`) — relevance to a market/zone is therefore driven
        entirely by the semantic content of `query` (e.g. "DK1 mFRR EAM
        balancing price"), not a payload filter; only the time window is
        filtered structurally. `retrieved_at` is when *we* stored the claim,
        not when the underlying article claim was published, so this window
        should be generous around the trigger's time, not exact.

        Returns a list of `ClaimPayload.to_dict()`-shaped dicts, most
        semantically similar first.
        """
        query_filter = None
        if time_from is not None or time_to is not None:
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="retrieved_at",
                        range=models.DatetimeRange(gte=time_from, lte=time_to),
                    )
                ]
            )

        response = await self._client.query_points(
            collection_name=COLLECTION_NAME,
            query=self._embed(query),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        return [point.payload for point in response.points]

    async def scroll_by_source(self, source: str, limit: int = 100) -> list[dict]:
        """
        Returns up to `limit` points whose payload `source` field exactly
        matches `source`, most-recently-retrieved first.

        Used by the dashboard's "recent manually-submitted claims" view
        (services/api/main.py) -- the only browsing surface over Qdrant
        content the dashboard has at all, since `search_claims` requires a
        semantic query rather than a plain listing. Manual submissions
        (services/api/main.py's `/manual-articles`) always store
        `feed_name="LinkedIn"`, which becomes this `source` value, making a
        simple exact-match filter sufficient without needing a dedicated
        "is manual" flag on the payload.
        """
        points, _ = await self._client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=models.Filter(
                must=[models.FieldCondition(key="source", match=models.MatchValue(value=source))]
            ),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        payloads = [point.payload for point in points]
        payloads.sort(key=lambda p: p.get("retrieved_at", ""), reverse=True)
        return payloads

    async def upsert_raw_article(self, article: ArticleRef, article_text: str) -> None:
        """
        Stores the raw article text as a single point with `claim_type="raw"`
        — the fallback path when `ANTHROPIC_API_KEY` isn't configured (see
        shared/claim_extractor.py).
        """
        payload = ClaimPayload(
            source=article.feed_name,
            author=article.author,
            retrieved_at=datetime.now(UTC).isoformat(),
            claim_type="raw",
            article_url=article.url,
            article_title=article.title,
            claim=article_text,
        )
        point = models.PointStruct(
            id=_raw_point_id(article.url),
            vector=self._embed(article_text),
            payload=payload.to_dict(),
        )
        await self._client.upsert(collection_name=COLLECTION_NAME, points=[point])
