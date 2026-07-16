from unittest.mock import MagicMock, patch

import pytest

from shared.datasets import DATASETS, DatasetConfig, SeriesConfig
from shared.db_manager import DatabaseManager

MFRR_CAPACITY = next(d for d in DATASETS if d.name == "mfrr_capacity")
POWER_SYSTEM = next(d for d in DATASETS if d.name == "power_system_right_now")

RECORDS = [
    {
        "TimeUTC": "2026-07-16T10:00:00",
        "PriceArea": "DK1",
        "UpPriceDKK": 450.5,
        "DownPriceDKK": 100.0,
    },
    {
        "TimeUTC": "2026-07-16T10:00:00",
        "PriceArea": "DK2",
        "UpPriceDKK": 460.0,
        "DownPriceDKK": 110.0,
    },
]


@pytest.fixture
def pooled_conn():
    """Patches the connection pool so DatabaseManager() never dials a real DB."""
    with patch("shared.db_manager.psycopg2_pool.SimpleConnectionPool") as mock_pool_cls:
        conn = MagicMock()
        mock_pool = MagicMock()
        mock_pool.getconn.return_value = conn
        mock_pool_cls.return_value = mock_pool
        yield conn, mock_pool


@pytest.fixture
def db(monkeypatch, pooled_conn):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    return DatabaseManager()


def test_init_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        DatabaseManager()


def test_init_creates_bounded_connection_pool(monkeypatch, pooled_conn):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    _, mock_pool = pooled_conn

    DatabaseManager(minconn=2, maxconn=7)

    from shared.db_manager import psycopg2_pool

    psycopg2_pool.SimpleConnectionPool.assert_called_once_with(
        2, 7, "postgresql://test:test@localhost/test"
    )


@patch("shared.db_manager.execute_values")
def test_save_market_data_maps_records_to_rows(mock_execute, db, pooled_conn):
    conn, mock_pool = pooled_conn

    saved = db.save_market_data(RECORDS, MFRR_CAPACITY)

    _, _, values = mock_execute.call_args.args
    assert saved == 4  # 2 records x 2 series (up/down)
    fetched_ats = {v[7] for v in values}
    assert len(fetched_ats) == 1  # single fetched_at for the whole batch
    rows_without_fetched_at = [v[:7] for v in values]
    assert rows_without_fetched_at == [
        ("2026-07-16T10:00:00", "mFRR_capacity", "DK1", "up", 450.5, "Energinet", True),
        ("2026-07-16T10:00:00", "mFRR_capacity", "DK1", "down", 100.0, "Energinet", True),
        ("2026-07-16T10:00:00", "mFRR_capacity", "DK2", "up", 460.0, "Energinet", True),
        ("2026-07-16T10:00:00", "mFRR_capacity", "DK2", "down", 110.0, "Energinet", True),
    ]
    conn.commit.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


@patch("shared.db_manager.execute_values")
def test_save_market_data_omits_missing_series_field(mock_execute, db):
    """A record missing one product's value field should skip only that series."""
    partial_records = [{"TimeUTC": "2026-07-16T10:00:00", "PriceArea": "DK1", "UpPriceDKK": 450.5}]

    saved = db.save_market_data(partial_records, MFRR_CAPACITY)

    _, _, values = mock_execute.call_args.args
    assert saved == 1
    assert values[0][:7] == (
        "2026-07-16T10:00:00",
        "mFRR_capacity",
        "DK1",
        "up",
        450.5,
        "Energinet",
        True,
    )


def test_save_market_data_falls_back_to_configured_zone_when_no_zone_field(db):
    with patch("shared.db_manager.execute_values") as mock_execute:
        records = [{"Minutes1UTC": "2026-07-16T10:00:00", "OnshoreWindPower": 1200.0}]

        db.save_market_data(records, POWER_SYSTEM)

        _, _, values = mock_execute.call_args.args
        assert values[0][2] == "ALL"  # zone falls back to dataset.zone


@patch("shared.db_manager.execute_values")
def test_save_market_data_closes_connection_on_failure(mock_execute, db, pooled_conn):
    conn, mock_pool = pooled_conn
    mock_execute.side_effect = RuntimeError("insert failed")

    with pytest.raises(RuntimeError):
        db.save_market_data(RECORDS, MFRR_CAPACITY)

    conn.rollback.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_save_market_data_raises_on_malformed_record(db, pooled_conn):
    _, mock_pool = pooled_conn

    with pytest.raises(KeyError):
        db.save_market_data([{"PriceArea": "DK1"}], MFRR_CAPACITY)

    # Mapping fails before a connection is ever checked out.
    mock_pool.getconn.assert_not_called()


def test_save_market_data_returns_zero_when_no_series_map(db):
    """All records lacking every series' value field -> nothing to insert."""
    with patch("shared.db_manager.execute_values") as mock_execute:
        saved = db.save_market_data(
            [{"TimeUTC": "2026-07-16T10:00:00", "PriceArea": "DK1"}], MFRR_CAPACITY
        )

        assert saved == 0
        mock_execute.assert_not_called()


def test_revision_preserving_repeated_fetch_creates_new_history_rows(db):
    """
    A second fetch with a changed value must produce a second INSERT batch
    (a new history row), never an UPDATE overwriting the first.
    """
    with patch("shared.db_manager.execute_values") as mock_execute:
        first_records = [
            {"TimeUTC": "2026-07-16T10:00:00", "PriceArea": "DK1", "UpPriceDKK": 450.5}
        ]
        second_records = [
            {"TimeUTC": "2026-07-16T10:00:00", "PriceArea": "DK1", "UpPriceDKK": 999.9}
        ]

        db.save_market_data(first_records, MFRR_CAPACITY)
        db.save_market_data(second_records, MFRR_CAPACITY)

        assert mock_execute.call_count == 2
        first_values = mock_execute.call_args_list[0].args[2]
        second_values = mock_execute.call_args_list[1].args[2]
        assert first_values[0][4] == 450.5
        assert second_values[0][4] == 999.9
        # Both calls used the "INSERT ... ON CONFLICT DO NOTHING" query, never
        # an UPDATE, so the earlier row is never overwritten in place.
        assert "INSERT INTO market_data_history" in mock_execute.call_args_list[0].args[1]
        assert "DO UPDATE" not in mock_execute.call_args_list[0].args[1]


def test_dataset_config_series_are_declarative():
    """Sanity check: the dataset registry declares series without inline logic."""
    imbalance = next(d for d in DATASETS if d.name == "imbalance_price")
    assert isinstance(imbalance.series[0], SeriesConfig)
    assert {s.product for s in imbalance.series} == {
        "imbalance_price",
        "afrr_vwa_up",
        "afrr_vwa_down",
    }


def test_fetch_distinct_series_queries_and_returns_rows(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [("mFRR_capacity", "DK1", "up")]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_distinct_series()

    assert result == [("mFRR_capacity", "DK1", "up")]
    cursor.execute.assert_called_once()
    assert "DISTINCT market, zone, product" in cursor.execute.call_args.args[0]
    mock_pool.putconn.assert_called_once_with(conn)


def test_fetch_history_maps_rows_to_dicts(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("2026-07-16T10:00:00", 450.5, "2026-07-16T10:05:00"),
        ("2026-07-16T09:00:00", 440.0, "2026-07-16T09:05:00"),
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_history("mFRR_capacity", "DK1", "up", limit=500)

    assert result == [
        {"time": "2026-07-16T10:00:00", "value": 450.5, "fetched_at": "2026-07-16T10:05:00"},
        {"time": "2026-07-16T09:00:00", "value": 440.0, "fetched_at": "2026-07-16T09:05:00"},
    ]
    cursor.execute.assert_called_once()
    query, params = cursor.execute.call_args.args
    assert "market_data_history" in query
    assert params == ("mFRR_capacity", "DK1", "up", 500)
    mock_pool.putconn.assert_called_once_with(conn)


def test_dataset_config_defaults():
    cfg = DatasetConfig(
        name="test",
        dataset_id="TestDataset",
        market="test_market",
        time_field="TimeUTC",
        series=[SeriesConfig(product="value", value_field="Value")],
    )
    assert cfg.zone_field == "PriceArea"
    assert cfg.zone == "ALL"
    assert cfg.source == "Energinet"
    assert cfg.is_provisional is True
