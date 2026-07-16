from unittest.mock import MagicMock, patch

import pytest

from shared.db_manager import DatabaseManager

RECORDS = [
    {"HourUTC": "2026-07-16T10:00:00", "PriceArea": "DK1", "PriceDKK": 450.5},
    {"HourUTC": "2026-07-16T10:00:00", "PriceArea": "DK2", "PriceDKK": 460.0},
]


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    return DatabaseManager()


def test_init_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        DatabaseManager()


@patch("shared.db_manager.execute_values")
@patch("shared.db_manager.psycopg2.connect")
def test_save_market_data_maps_records_to_rows(mock_connect, mock_execute, db):
    conn = MagicMock()
    mock_connect.return_value = conn

    db.save_market_data(RECORDS, product="up")

    _, _, values = mock_execute.call_args.args
    assert values == [
        ("2026-07-16T10:00:00", "mFRR_EAM", "DK1", "up", 450.5, "Energinet", True),
        ("2026-07-16T10:00:00", "mFRR_EAM", "DK2", "up", 460.0, "Energinet", True),
    ]
    conn.commit.assert_called_once()
    conn.close.assert_called_once()


@patch("shared.db_manager.execute_values")
@patch("shared.db_manager.psycopg2.connect")
def test_save_market_data_closes_connection_on_failure(mock_connect, mock_execute, db):
    conn = MagicMock()
    mock_connect.return_value = conn
    mock_execute.side_effect = RuntimeError("insert failed")

    with pytest.raises(RuntimeError):
        db.save_market_data(RECORDS, product="up")

    conn.close.assert_called_once()


@patch("shared.db_manager.psycopg2.connect")
def test_save_market_data_raises_on_malformed_record(mock_connect, db):
    with pytest.raises(KeyError):
        db.save_market_data([{"PriceArea": "DK1"}], product="up")

    # Mapping fails before a connection is ever opened.
    mock_connect.assert_not_called()
