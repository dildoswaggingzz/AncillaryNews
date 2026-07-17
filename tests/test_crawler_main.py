import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.claim_extractor import ExtractedClaim, ExtractionResult
from shared.rss_reader import ArticleRef

MAIN_PATH = Path(__file__).parent.parent / "services" / "crawler" / "main.py"

spec = importlib.util.spec_from_file_location("crawler_main", MAIN_PATH)
crawler_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crawler_main)

ARTICLE = ArticleRef(
    url="https://nordicbalancingmodel.net/some-update",
    title="NBM publishes MARI accession update",
    author="Nordic Balancing Model",
    published="Fri, 03 Jul 2026 07:11:50 +0000",
    feed_name="Nordic Balancing Model",
    feed_tier="tier1",
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
