import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MAIN_PATH = Path(__file__).parent.parent / "services" / "ingestor" / "main.py"

spec = importlib.util.spec_from_file_location("ingestor_main", MAIN_PATH)
ingestor_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingestor_main)


@pytest.fixture(autouse=True)
def database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")


async def test_cycle_saves_fetched_records():
    records = [{"HourUTC": "2026-07-16T10:00:00", "PriceArea": "DK1", "PriceDKK": 450.5}]
    with (
        patch.object(
            ingestor_main.BaseIngestor,
            "fetch_data",
            new=AsyncMock(return_value={"records": records}),
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()) as mock_close,
        patch.object(ingestor_main.DatabaseManager, "save_market_data", new=MagicMock()) as save,
    ):
        await ingestor_main.run_ingestion_cycle()

    save.assert_called_once_with(records, product="up")
    mock_close.assert_awaited_once()


async def test_cycle_skips_save_when_no_records():
    with (
        patch.object(
            ingestor_main.BaseIngestor,
            "fetch_data",
            new=AsyncMock(return_value={"records": []}),
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()),
        patch.object(ingestor_main.DatabaseManager, "save_market_data", new=MagicMock()) as save,
    ):
        await ingestor_main.run_ingestion_cycle()

    save.assert_not_called()


async def test_cycle_closes_client_and_reraises_on_fetch_error():
    with (
        patch.object(
            ingestor_main.BaseIngestor,
            "fetch_data",
            new=AsyncMock(side_effect=RuntimeError("API down")),
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()) as mock_close,
        patch.object(ingestor_main.DatabaseManager, "save_market_data", new=MagicMock()) as save,
    ):
        with pytest.raises(RuntimeError):
            await ingestor_main.run_ingestion_cycle()

    save.assert_not_called()
    mock_close.assert_awaited_once()
