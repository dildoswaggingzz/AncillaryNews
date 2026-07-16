import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.datasets import DATASETS

MAIN_PATH = Path(__file__).parent.parent / "services" / "ingestor" / "main.py"

spec = importlib.util.spec_from_file_location("ingestor_main", MAIN_PATH)
ingestor_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingestor_main)


@pytest.fixture(autouse=True)
def database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")


@pytest.fixture(autouse=True)
def no_rate_limit_sleep(monkeypatch):
    """Keep the multi-dataset cycle test fast; rate-limit pacing is covered separately."""
    monkeypatch.setattr(ingestor_main, "RATE_LIMIT_SECONDS", 0)


@pytest.fixture
def db_manager_cls():
    with patch.object(ingestor_main, "DatabaseManager") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_cls, mock_instance


async def test_cycle_polls_and_saves_every_dataset(db_manager_cls):
    _, db_instance = db_manager_cls
    records_by_dataset = {d.name: [{"fake": d.name}] for d in DATASETS}

    async def fake_fetch(endpoint, params=None):
        dataset_id = endpoint.removeprefix("dataset/")
        dataset = next(d for d in DATASETS if d.dataset_id == dataset_id)
        return {"records": records_by_dataset[dataset.name]}

    with (
        patch.object(
            ingestor_main.BaseIngestor, "fetch_data", new=AsyncMock(side_effect=fake_fetch)
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()) as mock_close,
    ):
        await ingestor_main.run_ingestion_cycle()

    assert db_instance.save_market_data.call_count == len(DATASETS)
    saved_datasets = {call.args[1].name for call in db_instance.save_market_data.call_args_list}
    assert saved_datasets == {d.name for d in DATASETS}
    mock_close.assert_awaited_once()
    db_instance.close.assert_called_once()


async def test_cycle_skips_save_when_no_records(db_manager_cls):
    _, db_instance = db_manager_cls

    with (
        patch.object(
            ingestor_main.BaseIngestor,
            "fetch_data",
            new=AsyncMock(return_value={"records": []}),
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()),
    ):
        await ingestor_main.run_ingestion_cycle()

    db_instance.save_market_data.assert_not_called()


async def test_cycle_continues_after_one_dataset_fetch_fails(db_manager_cls):
    """One dataset failing to fetch shouldn't stop the rest of the cycle (uptime KPI)."""
    _, db_instance = db_manager_cls
    failing_dataset_id = DATASETS[0].dataset_id

    async def flaky_fetch(endpoint, params=None):
        if endpoint == f"dataset/{failing_dataset_id}":
            raise RuntimeError("API down")
        return {"records": [{"fake": "record"}]}

    with (
        patch.object(
            ingestor_main.BaseIngestor, "fetch_data", new=AsyncMock(side_effect=flaky_fetch)
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()) as mock_close,
    ):
        await ingestor_main.run_ingestion_cycle()

    # Every dataset except the failing one still got saved.
    assert db_instance.save_market_data.call_count == len(DATASETS) - 1
    mock_close.assert_awaited_once()
    db_instance.close.assert_called_once()


async def test_cycle_continues_after_one_dataset_save_fails(db_manager_cls):
    _, db_instance = db_manager_cls
    db_instance.save_market_data.side_effect = [RuntimeError("db error")] + [
        None for _ in range(len(DATASETS) - 1)
    ]

    with (
        patch.object(
            ingestor_main.BaseIngestor,
            "fetch_data",
            new=AsyncMock(return_value={"records": [{"fake": "record"}]}),
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()) as mock_close,
    ):
        await ingestor_main.run_ingestion_cycle()

    assert db_instance.save_market_data.call_count == len(DATASETS)
    mock_close.assert_awaited_once()
    db_instance.close.assert_called_once()


async def test_cycle_closes_client_and_db_even_if_all_fetches_fail(db_manager_cls):
    _, db_instance = db_manager_cls

    with (
        patch.object(
            ingestor_main.BaseIngestor,
            "fetch_data",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()) as mock_close,
    ):
        await ingestor_main.run_ingestion_cycle()

    db_instance.save_market_data.assert_not_called()
    mock_close.assert_awaited_once()
    db_instance.close.assert_called_once()


def test_ingestor_no_longer_owns_rule_engine_evaluation():
    """
    M4 relocated trigger evaluation into services/orchestrator/main.py (see
    its module docstring) so it isn't coupled to the ingestion poll cadence
    or evaluated twice. The ingestor module should no longer import or call
    it.
    """
    assert not hasattr(ingestor_main, "run_rule_engine")
