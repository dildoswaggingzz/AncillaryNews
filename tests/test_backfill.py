from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from tenacity import wait_none

from shared.backfill import (
    BESS_DATASET_NAMES,
    _date_chunks,
    _historical_params,
    backfill_dataset,
    bess_datasets,
    run_backfill,
)
from shared.base_ingestor import BaseIngestor
from shared.datasets import DATASETS
from shared.db_manager import SaveResult

FCR_DK1 = next(d for d in DATASETS if d.name == "fcr_dk1")
DAY_AHEAD = next(d for d in DATASETS if d.name == "day_ahead_prices")

FCR_DK1_URL = f"https://api.energidataservice.dk/dataset/{FCR_DK1.dataset_id}"


# --- bess_datasets() -----------------------------------------------------


def test_bess_datasets_matches_bess_dataset_names():
    assert {d.name for d in bess_datasets()} == BESS_DATASET_NAMES


def test_bess_dataset_names_exact_membership():
    """
    Literal exact-membership assertion (not just self-referential against
    BESS_DATASET_NAMES like the test above) -- fails loudly if a future
    registry change adds/removes a name from the allowlist without a
    conscious edit here too. Update this set deliberately, in the same
    change that edits shared/backfill.py:BESS_DATASET_NAMES.
    """
    assert BESS_DATASET_NAMES == {
        "fcr_dk1",
        "fcr_dk2",
        "afrr_reserves_nordic",
        "afrr_energy_activation",
        "day_ahead_prices",
        "imbalance_price",
        "ffr_dk2",
        "ffr_demand_dk2",
        "inertia_nordic",
    }


def test_bess_datasets_excludes_mfrr_markets():
    """mFRR capacity/EAM/capacity-extra are never read by shared/bess_simulator.py (battery
    market-participation constraint) -- must never be backfilled either."""
    names = {d.name for d in bess_datasets()}
    assert "mfrr_capacity" not in names
    assert "mfrr_eam" not in names
    assert "mfrr_capacity_extra" not in names


def test_bess_datasets_excludes_non_bess_ingested_datasets():
    """Ingested for other purposes (system-state context, revision-signal
    investigation) but never read by shared/bess_simulator.py."""
    names = {d.name for d in bess_datasets()}
    assert "power_system_right_now" not in names
    assert "afrr_picasso_corrections" not in names


# --- _date_chunks ----------------------------------------------------------


def test_date_chunks_covers_full_range_without_gaps_or_overlap():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 22, tzinfo=UTC)

    chunks = list(_date_chunks(start, end, chunk_days=7))

    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    for (_s1, e1), (s2, _e2) in zip(chunks, chunks[1:], strict=False):
        assert e1 == s2


def test_date_chunks_clips_final_chunk_rather_than_overshooting():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 10, tzinfo=UTC)

    chunks = list(_date_chunks(start, end, chunk_days=7))

    assert chunks == [
        (start, datetime(2026, 1, 8, tzinfo=UTC)),
        (datetime(2026, 1, 8, tzinfo=UTC), end),
    ]


def test_date_chunks_rejects_non_positive_chunk_days():
    with pytest.raises(ValueError):
        list(_date_chunks(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC), 0))


# --- _historical_params ------------------------------------------------------


def test_historical_params_includes_start_end_limit_and_preserves_dataset_sort():
    start = datetime(2026, 1, 1, 6, 30, tzinfo=UTC)
    end = datetime(2026, 1, 8, tzinfo=UTC)

    params = _historical_params(FCR_DK1, start, end, 12345)

    assert params["start"] == "2026-01-01T06:30"
    assert params["end"] == "2026-01-08T00:00"
    assert params["limit"] == 12345
    assert params["sort"] == FCR_DK1.params["sort"]


# --- backfill_dataset (chunking + idempotency, mocked HTTP via respx) ------


@pytest.fixture
def ingestor():
    # Disable exponential backoff so retry tests run instantly (same pattern
    # as tests/test_base_ingestor.py).
    BaseIngestor.fetch_data.retry.wait = wait_none()
    return BaseIngestor("https://api.energidataservice.dk")


@pytest.fixture
def db():
    d = MagicMock()
    d.save_market_data.side_effect = lambda records, dataset: SaveResult(
        total=len(records), by_series={}
    )
    return d


@respx.mock
async def test_backfill_dataset_pages_through_chunks_and_saves_each(ingestor, db):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 15, tzinfo=UTC)  # chunk_days=7 -> 2 chunks

    route = respx.get(FCR_DK1_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"records": [{"HourUTC": "2026-01-02T00:00:00", "FCRdk_DKK": 10.0}]},
            ),
            httpx.Response(
                200,
                json={"records": [{"HourUTC": "2026-01-09T00:00:00", "FCRdk_DKK": 20.0}]},
            ),
        ]
    )

    result = await backfill_dataset(
        ingestor, db, FCR_DK1, start, end, chunk_days=7, rate_limit_seconds=0
    )

    assert route.call_count == 2
    assert result["dataset"] == "fcr_dk1"
    assert result["chunks_fetched"] == 2
    assert result["chunks_failed"] == 0
    assert result["records_fetched"] == 2
    assert result["rows_saved"] == 2
    assert result["earliest_record_time"] == "2026-01-02T00:00:00"
    assert result["latest_record_time"] == "2026-01-09T00:00:00"
    assert db.save_market_data.call_count == 2
    await ingestor.close()


@respx.mock
async def test_backfill_dataset_skips_failed_fetch_chunk_without_aborting(ingestor, db):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 15, tzinfo=UTC)

    respx.get(FCR_DK1_URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(500),  # exhausts BaseIngestor's 5 retry attempts
            httpx.Response(
                200,
                json={"records": [{"HourUTC": "2026-01-09T00:00:00", "FCRdk_DKK": 20.0}]},
            ),
        ]
    )

    result = await backfill_dataset(
        ingestor, db, FCR_DK1, start, end, chunk_days=7, rate_limit_seconds=0
    )

    assert result["chunks_failed"] == 1
    assert result["chunks_fetched"] == 1
    assert result["rows_saved"] == 1
    assert db.save_market_data.call_count == 1
    await ingestor.close()


@respx.mock
async def test_backfill_dataset_skips_failed_save_chunk_without_aborting(ingestor, db):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 15, tzinfo=UTC)

    respx.get(FCR_DK1_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"records": [{"HourUTC": "2026-01-02T00:00:00", "FCRdk_DKK": 10.0}]},
            ),
            httpx.Response(
                200,
                json={"records": [{"HourUTC": "2026-01-09T00:00:00", "FCRdk_DKK": 20.0}]},
            ),
        ]
    )
    db.save_market_data.side_effect = [Exception("db down"), SaveResult(total=1, by_series={})]

    result = await backfill_dataset(
        ingestor, db, FCR_DK1, start, end, chunk_days=7, rate_limit_seconds=0
    )

    assert result["chunks_failed"] == 1
    assert result["chunks_fetched"] == 2  # both chunks fetched OK; only the save failed
    assert result["rows_saved"] == 1
    await ingestor.close()


@respx.mock
async def test_backfill_dataset_handles_empty_chunk_gracefully(ingestor, db):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 8, tzinfo=UTC)

    respx.get(FCR_DK1_URL).mock(return_value=httpx.Response(200, json={"records": []}))

    result = await backfill_dataset(ingestor, db, FCR_DK1, start, end, rate_limit_seconds=0)

    assert result["chunks_fetched"] == 1
    assert result["records_fetched"] == 0
    assert result["rows_saved"] == 0
    assert result["earliest_record_time"] is None
    db.save_market_data.assert_not_called()
    await ingestor.close()


@respx.mock
async def test_backfill_dataset_is_safe_to_rerun(ingestor, db):
    """
    Re-running backfill_dataset over the exact same window calls
    save_market_data again rather than raising or silently no-oping --
    idempotent in the "safe to re-run" sense, not in the "dedupes against
    prior fetches" sense (see shared/backfill.py's module docstring: each
    save_market_data call is its own fresh fetched_at-tagged INSERT into the
    append-only market_data_history).
    """
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 8, tzinfo=UTC)

    respx.get(FCR_DK1_URL).mock(
        return_value=httpx.Response(
            200, json={"records": [{"HourUTC": "2026-01-02T00:00:00", "FCRdk_DKK": 10.0}]}
        )
    )

    first = await backfill_dataset(ingestor, db, FCR_DK1, start, end, rate_limit_seconds=0)
    second = await backfill_dataset(ingestor, db, FCR_DK1, start, end, rate_limit_seconds=0)

    assert first["rows_saved"] == 1
    assert second["rows_saved"] == 1
    assert db.save_market_data.call_count == 2
    await ingestor.close()


# --- run_backfill (orchestration) -------------------------------------------


async def test_run_backfill_rejects_start_after_end():
    with pytest.raises(ValueError):
        await run_backfill(datetime(2026, 1, 10, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))


async def test_run_backfill_rejects_unknown_dataset_name():
    with pytest.raises(ValueError):
        await run_backfill(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            dataset_names=["not_a_real_dataset"],
        )


async def test_run_backfill_rejects_non_bess_dataset_name():
    """mfrr_capacity is a real shared/datasets.py entry, but not a BESS one --
    must still be rejected, not silently backfilled."""
    with pytest.raises(ValueError):
        await run_backfill(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            dataset_names=["mfrr_capacity"],
        )


def _fake_backfill_dataset_result(dataset):
    return {
        "dataset": dataset.name,
        "dataset_id": dataset.dataset_id,
        "chunks_fetched": 1,
        "chunks_failed": 0,
        "records_fetched": 1,
        "rows_saved": 1,
        "earliest_record_time": "2026-01-01T00:00:00",
        "latest_record_time": "2026-01-01T00:00:00",
    }


@pytest.fixture
def fake_backfill_dataset():
    async def _fake(_ingestor, _db, dataset, _start, _end, chunk_days, rate_limit_seconds):
        return _fake_backfill_dataset_result(dataset)

    return _fake


async def test_run_backfill_filters_to_requested_dataset_names(monkeypatch, fake_backfill_dataset):
    monkeypatch.setattr("shared.backfill.backfill_dataset", fake_backfill_dataset)
    mock_db = MagicMock()

    with patch("shared.backfill.BaseIngestor") as mock_ingestor_cls:
        mock_ingestor = MagicMock()
        mock_ingestor.close = AsyncMock()
        mock_ingestor_cls.return_value = mock_ingestor

        summary = await run_backfill(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            dataset_names=["fcr_dk1", "day_ahead_prices"],
            db=mock_db,
            rate_limit_seconds=0,
        )

    assert {r["dataset"] for r in summary["datasets"]} == {"fcr_dk1", "day_ahead_prices"}
    assert summary["total_rows_saved"] == 2
    mock_ingestor.close.assert_awaited_once()
    mock_db.close.assert_not_called()  # a passed-in db is reused, never closed by run_backfill


async def test_run_backfill_defaults_to_every_bess_dataset(monkeypatch, fake_backfill_dataset):
    monkeypatch.setattr("shared.backfill.backfill_dataset", fake_backfill_dataset)
    mock_db = MagicMock()

    with patch("shared.backfill.BaseIngestor") as mock_ingestor_cls:
        mock_ingestor_cls.return_value.close = AsyncMock()

        summary = await run_backfill(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            db=mock_db,
            rate_limit_seconds=0,
        )

    assert {r["dataset"] for r in summary["datasets"]} == BESS_DATASET_NAMES


async def test_run_backfill_owns_and_closes_its_own_db_when_none_passed(
    monkeypatch, fake_backfill_dataset
):
    monkeypatch.setattr("shared.backfill.backfill_dataset", fake_backfill_dataset)

    with (
        patch("shared.backfill.BaseIngestor") as mock_ingestor_cls,
        patch("shared.backfill.DatabaseManager") as mock_db_cls,
    ):
        mock_ingestor_cls.return_value.close = AsyncMock()
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db

        await run_backfill(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            dataset_names=["fcr_dk1"],
            rate_limit_seconds=0,
        )

    mock_db_cls.assert_called_once()
    mock_db.close.assert_called_once()


async def test_run_backfill_defaults_end_to_now(monkeypatch, fake_backfill_dataset):
    monkeypatch.setattr("shared.backfill.backfill_dataset", fake_backfill_dataset)
    mock_db = MagicMock()

    with patch("shared.backfill.BaseIngestor") as mock_ingestor_cls:
        mock_ingestor_cls.return_value.close = AsyncMock()

        before = datetime.now(UTC)
        summary = await run_backfill(
            datetime(2026, 1, 1, tzinfo=UTC),
            dataset_names=["fcr_dk1"],
            db=mock_db,
            rate_limit_seconds=0,
        )
        after = datetime.now(UTC)

    assert before <= summary["end"] <= after
