import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.claim_extractor import ExtractedClaim, ExtractionResult
from shared.event_extractor import ExtractedEvent
from shared.rss_reader import ArticleRef

MAIN_PATH = Path(__file__).parent.parent / "services" / "crawler" / "main.py"

spec = importlib.util.spec_from_file_location("crawler_main", MAIN_PATH)
crawler_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crawler_main)

# Cycle tests pin RSS_FEEDS to a single feed so they exercise cycle mechanics
# (fetch_feed_entries is mocked, so the feed's identity is irrelevant)
# independently of how many feeds ship in shared/rss_feeds.py.
_SINGLE_FEED = object()

ARTICLE = ArticleRef(
    url="https://nordicbalancingmodel.net/some-update",
    title="NBM publishes MARI accession update",
    author="Nordic Balancing Model",
    published="Fri, 03 Jul 2026 07:11:50 +0000",
    feed_name="Nordic Balancing Model",
    feed_tier="tier1",
)


# A self_contained-feed article (ENTSO-E news): body arrives inline on the
# ArticleRef; `url` is a synthetic identity anchor, not a fetchable page.
INLINE_ARTICLE = ArticleRef(
    url="https://transparency.entsoe.eu/news#deadbeefdeadbeef",
    title="Publication issue on Wind offshore generation for Belgium",
    author=None,
    published="Mon, 20 Jul 2026 14:17:09 GMT",
    feed_name="ENTSO-E Transparency Platform",
    feed_tier="tier1",
    content="<p>Dear Transparency Platform users, Elia has noticed deltas.</p>",
)


@pytest.fixture
def store():
    s = MagicMock()
    s.is_processed = AsyncMock(return_value=False)
    s.upsert_claims = AsyncMock(return_value=1)
    s.upsert_raw_article = AsyncMock()
    return s


async def test_process_article_skips_when_already_processed(store):
    store.is_processed = AsyncMock(return_value=True)

    with patch.object(crawler_main, "fetch_article_html", new=AsyncMock()) as mock_fetch:
        await crawler_main.process_article(ARTICLE, http_client=MagicMock(), store=store)

    mock_fetch.assert_not_awaited()


async def test_process_article_stores_raw_text_when_no_api_key(store):
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=None)),
    ):
        result = await crawler_main.process_article(ARTICLE, http_client=MagicMock(), store=store)

    store.upsert_raw_article.assert_awaited_once_with(ARTICLE, "clean article text")
    store.upsert_claims.assert_not_awaited()
    assert result == 0


async def test_process_article_stores_claims_when_extraction_succeeds(store):
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
    ):
        result = await crawler_main.process_article(ARTICLE, http_client=MagicMock(), store=store)

    store.upsert_claims.assert_awaited_once_with(ARTICLE, extraction.claims)
    store.upsert_raw_article.assert_not_awaited()
    assert result == 1


async def test_process_article_uses_inline_content_without_fetching(store):
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    with (
        patch.object(crawler_main, "fetch_article_html", new=AsyncMock()) as mock_fetch,
        patch.object(
            crawler_main, "extract_markdown", return_value="clean article text"
        ) as mock_extract,
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
    ):
        result = await crawler_main.process_article(
            INLINE_ARTICLE, http_client=MagicMock(), store=store
        )

    # Self-contained feed: no HTTP fetch, extract straight from inline content.
    mock_fetch.assert_not_awaited()
    mock_extract.assert_called_once_with(INLINE_ARTICLE.content, url=INLINE_ARTICLE.url)
    store.upsert_claims.assert_awaited_once_with(INLINE_ARTICLE, extraction.claims)
    assert result == 1


async def test_process_article_skips_when_html_fetch_fails(store):
    with (
        patch.object(crawler_main, "fetch_article_html", new=AsyncMock(return_value=None)),
        patch.object(crawler_main, "extract_markdown") as mock_extract,
    ):
        await crawler_main.process_article(ARTICLE, http_client=MagicMock(), store=store)

    mock_extract.assert_not_called()
    store.upsert_claims.assert_not_awaited()
    store.upsert_raw_article.assert_not_awaited()


async def test_process_article_skips_when_markdown_extraction_fails(store):
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value=None),
    ):
        await crawler_main.process_article(ARTICLE, http_client=MagicMock(), store=store)

    store.upsert_claims.assert_not_awaited()
    store.upsert_raw_article.assert_not_awaited()


async def test_run_crawl_cycle_processes_every_feed_and_closes_client():
    fake_articles = [ARTICLE]

    with (
        patch.object(crawler_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(crawler_main, "QdrantStore") as mock_store_cls,
        patch.object(crawler_main, "RSS_FEEDS", [_SINGLE_FEED]),
        patch.object(crawler_main, "fetch_feed_entries", new=AsyncMock(return_value=fake_articles)),
        patch.object(crawler_main, "process_article", new=AsyncMock()) as mock_process,
    ):
        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await crawler_main.run_crawl_cycle()

    mock_store_instance.ensure_collection.assert_awaited_once()
    assert mock_process.await_count == len(fake_articles)
    mock_qdrant_instance.close.assert_awaited_once()


async def test_run_crawl_cycle_continues_after_one_article_fails():
    with (
        patch.object(crawler_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(crawler_main, "QdrantStore") as mock_store_cls,
        patch.object(crawler_main, "RSS_FEEDS", [_SINGLE_FEED]),
        patch.object(
            crawler_main, "fetch_feed_entries", new=AsyncMock(return_value=[ARTICLE, ARTICLE])
        ),
        patch.object(
            crawler_main,
            "process_article",
            new=AsyncMock(side_effect=[RuntimeError("boom"), None]),
        ) as mock_process,
    ):
        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await crawler_main.run_crawl_cycle()

    assert mock_process.await_count == 2
    mock_qdrant_instance.close.assert_awaited_once()


# --- AUTO_RUN_ENABLED (cost-control gate) -------------------------------------


async def test_scheduled_crawl_cycle_no_ops_when_auto_run_disabled(monkeypatch):
    monkeypatch.delenv("AUTO_RUN_ENABLED", raising=False)

    with patch.object(crawler_main, "run_crawl_cycle", new=AsyncMock()) as mock_run:
        await crawler_main.scheduled_crawl_cycle()

    mock_run.assert_not_awaited()


async def test_scheduled_crawl_cycle_no_ops_when_auto_run_explicitly_false(monkeypatch):
    monkeypatch.setenv("AUTO_RUN_ENABLED", "false")

    with patch.object(crawler_main, "run_crawl_cycle", new=AsyncMock()) as mock_run:
        await crawler_main.scheduled_crawl_cycle()

    mock_run.assert_not_awaited()


async def test_scheduled_crawl_cycle_runs_when_auto_run_enabled(monkeypatch):
    monkeypatch.setenv("AUTO_RUN_ENABLED", "true")

    with patch.object(crawler_main, "run_crawl_cycle", new=AsyncMock()) as mock_run:
        await crawler_main.scheduled_crawl_cycle()

    mock_run.assert_awaited_once()


def test_auto_run_enabled_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("AUTO_RUN_ENABLED", "TRUE")
    assert crawler_main._auto_run_enabled() is True

    monkeypatch.setenv("AUTO_RUN_ENABLED", "False")
    assert crawler_main._auto_run_enabled() is False


# --- run_crawl_cycle return summary ------------------------------------------


async def test_run_crawl_cycle_returns_articles_processed_and_claims_extracted_summary():
    with (
        patch.object(crawler_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(crawler_main, "QdrantStore") as mock_store_cls,
        patch.object(crawler_main, "RSS_FEEDS", [_SINGLE_FEED]),
        patch.object(
            crawler_main, "fetch_feed_entries", new=AsyncMock(return_value=[ARTICLE, ARTICLE])
        ),
        patch.object(crawler_main, "process_article", new=AsyncMock(side_effect=[3, 0])),
    ):
        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        result = await crawler_main.run_crawl_cycle()

    assert result == {"articles_processed": 2, "claims_extracted": 3}


# --- metrics (Phase 6 production readiness) ---------------------------------


def test_metrics_are_registered_and_exposition_includes_expected_names():
    from prometheus_client import generate_latest

    output = generate_latest().decode()

    assert "crawler_cycle_duration_seconds" in output
    assert "crawler_article_processed_total" in output


async def test_process_article_records_stored_claims_metric(store):
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    before = crawler_main.ARTICLE_PROCESSED_TOTAL.labels(status="stored_claims")._value.get()

    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
    ):
        await crawler_main.process_article(ARTICLE, http_client=MagicMock(), store=store)

    after = crawler_main.ARTICLE_PROCESSED_TOTAL.labels(status="stored_claims")._value.get()
    assert after == before + 1


# --- M6+ supply-event features: additive, non-fatal event storage ----------
# (docs/supply-event-features-design.md §4)


def test_known_at_parses_rfc822_published_string():
    known_at = crawler_main._known_at(ARTICLE)
    assert known_at == datetime(2026, 7, 3, 7, 11, 50, tzinfo=UTC)


def test_known_at_falls_back_to_crawl_time_when_published_is_missing():
    article = ArticleRef(
        url="https://example.test/no-date",
        title="No date",
        author=None,
        published=None,
        feed_name="Test Feed",
        feed_tier="tier1",
    )
    before = datetime.now(UTC)
    known_at = crawler_main._known_at(article)
    after = datetime.now(UTC)
    assert before <= known_at <= after


def test_known_at_falls_back_to_crawl_time_when_published_is_unparseable():
    article = ArticleRef(
        url="https://example.test/bad-date",
        title="Bad date",
        author=None,
        published="not a real date",
        feed_name="Test Feed",
        feed_tier="tier1",
    )
    before = datetime.now(UTC)
    known_at = crawler_main._known_at(article)
    after = datetime.now(UTC)
    assert before <= known_at <= after


def test_event_id_deterministic_and_index_scoped():
    id_a = crawler_main._event_id("https://example.test/article", 0)
    id_b = crawler_main._event_id("https://example.test/article", 0)
    id_c = crawler_main._event_id("https://example.test/article", 1)
    assert id_a == id_b  # deterministic -- re-crawl regenerates the same ID
    assert id_a != id_c  # per-event, index-scoped


@pytest.fixture
def db():
    d = MagicMock()
    d.save_market_event = MagicMock()
    return d


async def test_process_article_stores_events_additively_alongside_claims(store, db):
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    event = ExtractedEvent(
        event_type="prequalification",
        market="FCR",
        zone="DK2",
        direction="up",
        magnitude_mw=20.0,
        effective_from=None,
        confidence=0.9,
        raw_excerpt="A 20 MW battery prequalified.",
    )
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
        patch.object(crawler_main, "extract_events", new=AsyncMock(return_value=[event])),
    ):
        result = await crawler_main.process_article(
            ARTICLE, http_client=MagicMock(), store=store, db=db
        )

    store.upsert_claims.assert_awaited_once_with(ARTICLE, extraction.claims)
    db.save_market_event.assert_called_once()
    _, kwargs = db.save_market_event.call_args
    assert kwargs["event_type"] == "prequalification"
    assert kwargs["magnitude_mw"] == 20.0
    assert kwargs["source_url"] == ARTICLE.url
    assert kwargs["known_at"] == datetime(2026, 7, 3, 7, 11, 50, tzinfo=UTC)
    assert result == 1  # claims count is unaffected by the event path


async def test_process_article_skips_event_path_when_db_is_none(store):
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
        patch.object(crawler_main, "extract_events", new=AsyncMock()) as mock_extract_events,
    ):
        result = await crawler_main.process_article(
            ARTICLE, http_client=MagicMock(), store=store, db=None
        )

    mock_extract_events.assert_not_awaited()
    assert result == 1


async def test_process_article_event_extraction_failure_leaves_claim_storage_intact(store, db):
    """
    Design §4's core requirement, tested directly: an event-path failure
    (here, `extract_events` itself raising) must not affect claim storage or
    prevent `process_article` from returning its normal claims-stored count.
    """
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
        patch.object(
            crawler_main, "extract_events", new=AsyncMock(side_effect=RuntimeError("LLM boom"))
        ),
    ):
        result = await crawler_main.process_article(
            ARTICLE, http_client=MagicMock(), store=store, db=db
        )

    store.upsert_claims.assert_awaited_once_with(ARTICLE, extraction.claims)
    db.save_market_event.assert_not_called()
    assert result == 1  # claims path fully intact despite the event-path exception


async def test_process_article_event_storage_failure_leaves_claim_storage_intact(store, db):
    """
    Same guarantee, but the failure is in `db.save_market_event` (a DB
    error) rather than in extraction itself.
    """
    extraction = ExtractionResult(
        summary="summary",
        claims=[ExtractedClaim(claim="claim text", claim_type="fact")],
    )
    event = ExtractedEvent(
        event_type="outage",
        market=None,
        zone="DK1",
        direction=None,
        magnitude_mw=None,
        effective_from=None,
        confidence=0.5,
        raw_excerpt="An outage occurred.",
    )
    db.save_market_event.side_effect = RuntimeError("DB down")

    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=extraction)),
        patch.object(crawler_main, "extract_events", new=AsyncMock(return_value=[event])),
    ):
        result = await crawler_main.process_article(
            ARTICLE, http_client=MagicMock(), store=store, db=db
        )

    store.upsert_claims.assert_awaited_once_with(ARTICLE, extraction.claims)
    assert result == 1


async def test_process_article_event_path_runs_even_when_no_api_key_stored_claims_raw(store, db):
    """
    Event storage is attempted independently of the claim path's own
    ANTHROPIC_API_KEY outcome (design §4: "after the existing claim path" --
    both extractors hit the same key, so in practice both skip together, but
    the wiring itself does not couple them).
    """
    event = ExtractedEvent(
        event_type="prequalification",
        market="FCR",
        zone="DK2",
        direction=None,
        magnitude_mw=5.0,
        effective_from=None,
        confidence=0.9,
        raw_excerpt="Prequalified.",
    )
    with (
        patch.object(
            crawler_main, "fetch_article_html", new=AsyncMock(return_value="<html>body</html>")
        ),
        patch.object(crawler_main, "extract_markdown", return_value="clean article text"),
        patch.object(crawler_main, "extract_claims", new=AsyncMock(return_value=None)),
        patch.object(crawler_main, "extract_events", new=AsyncMock(return_value=[event])),
    ):
        result = await crawler_main.process_article(
            ARTICLE, http_client=MagicMock(), store=store, db=db
        )

    store.upsert_raw_article.assert_awaited_once()
    db.save_market_event.assert_called_once()
    assert result == 0


def test_get_db_returns_none_when_database_url_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert crawler_main._get_db() is None


async def test_run_crawl_cycle_closes_db_when_configured():
    fake_db = MagicMock()
    with (
        patch.object(crawler_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(crawler_main, "QdrantStore") as mock_store_cls,
        patch.object(crawler_main, "_get_db", return_value=fake_db),
        patch.object(crawler_main, "fetch_feed_entries", new=AsyncMock(return_value=[])),
    ):
        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await crawler_main.run_crawl_cycle()

    fake_db.close.assert_called_once()


async def test_run_crawl_cycle_passes_db_to_process_article():
    fake_db = MagicMock()
    with (
        patch.object(crawler_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(crawler_main, "QdrantStore") as mock_store_cls,
        patch.object(crawler_main, "_get_db", return_value=fake_db),
        patch.object(crawler_main, "RSS_FEEDS", [_SINGLE_FEED]),
        patch.object(crawler_main, "fetch_feed_entries", new=AsyncMock(return_value=[ARTICLE])),
        patch.object(
            crawler_main, "process_article", new=AsyncMock(return_value=0)
        ) as mock_process,
    ):
        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await crawler_main.run_crawl_cycle()

    mock_process.assert_awaited_once()
    call_args = mock_process.await_args.args
    assert call_args[0] == ARTICLE
    assert call_args[2] == mock_store_instance
    assert call_args[3] is fake_db
