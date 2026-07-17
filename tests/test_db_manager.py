import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from shared.datasets import DATASETS, DatasetConfig, SeriesConfig
from shared.db_manager import DatabaseManager

MFRR_CAPACITY = next(d for d in DATASETS if d.name == "mfrr_capacity")
POWER_SYSTEM = next(d for d in DATASETS if d.name == "power_system_right_now")
MFRR_EAM = next(d for d in DATASETS if d.name == "mfrr_eam")
AFRR_ENERGY_ACTIVATION = next(d for d in DATASETS if d.name == "afrr_energy_activation")
AFRR_PICASSO_CORRECTIONS = next(d for d in DATASETS if d.name == "afrr_picasso_corrections")

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


@patch("shared.db_manager.execute_values")
def test_save_market_data_maps_mfrr_eam_price_and_volume_products(mock_execute, db):
    """MfrrEnergyActivationMarket carries both price (up/down) and volume
    (*_volume) products from a single record, distinct from mFRR_capacity."""
    records = [
        {
            "TimeUTC": "2026-07-17T06:15:00",
            "PriceArea": "DK1",
            "mFRRSAUpReqMW": 38,
            "mFRRSAUpEUR": 165.24,
            "mFRRSADownReqMW": None,
            "mFRRSADownEUR": 132.5,
            "TotalmFRRUpMW": 38,
            "TotalmFRRDownMW": 0,
            "mFRROfferedUpMW": 525,
            "mFRROfferedDownMW": 1011,
        }
    ]

    saved = db.save_market_data(records, MFRR_EAM)

    _, _, values = mock_execute.call_args.args
    # up, down, up_volume (down_volume omitted: None), up_total_volume,
    # down_total_volume (0, not omitted -- 0 is a valid volume, not missing),
    # up_offered_volume, down_offered_volume
    assert saved == 7
    rows_without_fetched_at = {(v[1], v[2], v[3], v[4]) for v in values}
    assert rows_without_fetched_at == {
        ("mFRR_EAM", "DK1", "up", 165.24),
        ("mFRR_EAM", "DK1", "down", 132.5),
        ("mFRR_EAM", "DK1", "up_volume", 38),
        ("mFRR_EAM", "DK1", "up_total_volume", 38),
        ("mFRR_EAM", "DK1", "down_total_volume", 0),
        ("mFRR_EAM", "DK1", "up_offered_volume", 525),
        ("mFRR_EAM", "DK1", "down_offered_volume", 1011),
    }
    assert ("mFRR_EAM", "DK1", "down_volume", None) not in rows_without_fetched_at


@patch("shared.db_manager.execute_values")
def test_save_market_data_maps_afrr_energy_activation_volume(mock_execute, db):
    """aFRR_Activated (MW, signed) maps to a new 'activation_volume' product
    alongside the pre-existing 'activation_price' product."""
    records = [
        {
            "TimeMsUTC": "2026-07-16T13:06:09.901",
            "PriceArea": "DK2",
            "aFRR_Activated": 12.6,
            "aFRR_ActivatedEUR": 194.07,
        }
    ]

    saved = db.save_market_data(records, AFRR_ENERGY_ACTIVATION)

    _, _, values = mock_execute.call_args.args
    assert saved == 2
    rows = {(v[1], v[2], v[3], v[4]) for v in values}
    assert rows == {
        ("aFRR_energy", "DK2", "activation_price", 194.07),
        ("aFRR_energy", "DK2", "activation_volume", 12.6),
    }


@patch("shared.db_manager.execute_values")
def test_save_market_data_maps_afrr_picasso_corrections(mock_execute, db):
    """Correction (MW) and PriceUp/DownEUR map to their own aFRR_correction
    market, distinct from aFRR_energy and mFRR_EAM."""
    records = [
        {
            "TimeMsUTC": "2026-07-16T13:06:03.983",
            "PriceArea": "DK1",
            "Correction": 126.88699,
            "PriceUpEUR": 150.0,
            "PriceDownEUR": None,
        }
    ]

    saved = db.save_market_data(records, AFRR_PICASSO_CORRECTIONS)

    _, _, values = mock_execute.call_args.args
    # PriceDownEUR is None, so only correction_volume + up are saved.
    assert saved == 2
    rows = {(v[1], v[2], v[3], v[4]) for v in values}
    assert rows == {
        ("aFRR_correction", "DK1", "correction_volume", 126.88699),
        ("aFRR_correction", "DK1", "up", 150.0),
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


def test_fetch_context_window_queries_windowed_range(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
            440.0,
            "Energinet",
            True,
            datetime(2026, 7, 16, 9, 5, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            450.5,
            "Energinet",
            True,
            datetime(2026, 7, 16, 10, 5, tzinfo=UTC),
        ),
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    center_time = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    result = db.fetch_context_window(
        "mFRR_capacity", "DK1", "up", center_time, hours_before=6, hours_after=6
    )

    assert result == [
        {
            "time": datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
            "value": 440.0,
            "source": "Energinet",
            "is_provisional": True,
            "fetched_at": datetime(2026, 7, 16, 9, 5, tzinfo=UTC),
        },
        {
            "time": datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            "value": 450.5,
            "source": "Energinet",
            "is_provisional": True,
            "fetched_at": datetime(2026, 7, 16, 10, 5, tzinfo=UTC),
        },
    ]
    cursor.execute.assert_called_once()
    query, params = cursor.execute.call_args.args
    assert "market_data_history" in query
    assert params[:3] == ("mFRR_capacity", "DK1", "up")
    window_start, window_end = params[3], params[4]
    assert window_start == datetime(2026, 7, 16, 4, 0, tzinfo=UTC)
    assert window_end == datetime(2026, 7, 16, 16, 0, tzinfo=UTC)
    mock_pool.putconn.assert_called_once_with(conn)


def test_fetch_context_window_defaults_to_six_hours_either_side(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn.cursor.return_value.__enter__.return_value = cursor

    center_time = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    db.fetch_context_window("mFRR_capacity", "DK1", "up", center_time)

    _, params = cursor.execute.call_args.args
    assert params[3] == datetime(2026, 7, 16, 6, 0, tzinfo=UTC)
    assert params[4] == datetime(2026, 7, 16, 18, 0, tzinfo=UTC)


def test_save_event_report_inserts_report_json(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    report = {"event_id": "evt-1", "market": "mFRR_capacity", "confidence": "medium"}
    db.save_event_report(
        event_id="evt-1",
        market="mFRR_capacity",
        zone="DK1",
        product="up",
        time=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        report=report,
    )

    cursor.execute.assert_called_once()
    query, params = cursor.execute.call_args.args
    assert "INSERT INTO event_reports" in query
    assert params[0] == "evt-1"
    assert params[5].adapted == report  # psycopg2.extras.Json wraps the dict
    assert params[6] is False
    assert params[7] is None
    conn.commit.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_save_event_report_marks_correction_with_corrects_event_id(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    db.save_event_report(
        event_id="evt-1-correction-abc",
        market="mFRR_capacity",
        zone="DK1",
        product="up",
        time=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        report={"event_id": "evt-1-correction-abc"},
        is_correction=True,
        corrects_event_id="evt-1",
    )

    _, params = cursor.execute.call_args.args
    assert params[6] is True
    assert params[7] == "evt-1"


def test_save_event_report_rolls_back_and_reraises_on_failure(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.execute.side_effect = RuntimeError("insert failed")
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(RuntimeError):
        db.save_event_report(
            event_id="evt-1",
            market="mFRR_capacity",
            zone="DK1",
            product="up",
            time=datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            report={},
        )

    conn.rollback.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_find_published_report_returns_none_when_absent(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.find_published_report(
        "mFRR_capacity", "DK1", "up", datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    )

    assert result is None
    mock_pool.putconn.assert_called_once_with(conn)


def test_find_published_report_maps_row_to_dict(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    report_json = {"event_id": "evt-1", "confidence": "medium"}
    cursor.fetchone.return_value = (
        "evt-1",
        "mFRR_capacity",
        "DK1",
        "up",
        datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 10, 20, tzinfo=UTC),
        json.dumps(report_json),
        False,
        None,
    )
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.find_published_report(
        "mFRR_capacity", "DK1", "up", datetime(2026, 7, 16, 10, 0, tzinfo=UTC)
    )

    assert result["event_id"] == "evt-1"
    assert result["report"] == report_json
    assert result["is_correction"] is False
    query, params = cursor.execute.call_args.args
    assert "event_reports" in query
    assert params == ("mFRR_capacity", "DK1", "up", datetime(2026, 7, 16, 10, 0, tzinfo=UTC))


def test_fetch_series_values_defaults_to_latest_view(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            450.5,
            "Energinet",
            True,
            datetime(2026, 7, 16, 10, 5, tzinfo=UTC),
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_series_values("mFRR_capacity", "DK1", "up", limit=100)

    assert result == [
        {
            "time": datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            "value": 450.5,
            "source": "Energinet",
            "is_provisional": True,
            "ingested_at": datetime(2026, 7, 16, 10, 5, tzinfo=UTC),
        }
    ]
    query, params = cursor.execute.call_args.args
    assert "FROM market_data\n" in query
    assert "market_data_history" not in query
    assert params == ["mFRR_capacity", "DK1", "up", 100]
    mock_pool.putconn.assert_called_once_with(conn)


def test_fetch_series_values_history_flag_reads_raw_history(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            450.5,
            "Energinet",
            True,
            datetime(2026, 7, 16, 10, 5, tzinfo=UTC),
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_series_values("mFRR_capacity", "DK1", "up", history=True)

    assert result[0]["fetched_at"] == datetime(2026, 7, 16, 10, 5, tzinfo=UTC)
    query, _ = cursor.execute.call_args.args
    assert "FROM market_data_history" in query


def test_fetch_series_values_applies_time_range_filters(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn.cursor.return_value.__enter__.return_value = cursor

    time_from = datetime(2026, 7, 1, tzinfo=UTC)
    time_to = datetime(2026, 7, 31, tzinfo=UTC)
    db.fetch_series_values("mFRR_capacity", "DK1", "up", time_from=time_from, time_to=time_to)

    query, params = cursor.execute.call_args.args
    assert "time >= %s" in query
    assert "time <= %s" in query
    assert params == ["mFRR_capacity", "DK1", "up", time_from, time_to, 500]


def test_fetch_event_reports_returns_mapped_rows(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            "evt-1",
            "mFRR_capacity",
            "DK1",
            "up",
            datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
            datetime(2026, 7, 16, 10, 20, tzinfo=UTC),
            json.dumps({"event_id": "evt-1"}),
            False,
            None,
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_event_reports(market="mFRR_capacity", limit=10, offset=0)

    assert result[0]["event_id"] == "evt-1"
    assert result[0]["report"] == {"event_id": "evt-1"}
    query, params = cursor.execute.call_args.args
    assert "market = %s" in query
    assert params == ["mFRR_capacity", 10, 0]
    mock_pool.putconn.assert_called_once_with(conn)


def test_fetch_event_reports_no_filters_omits_where_clause(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn.cursor.return_value.__enter__.return_value = cursor

    db.fetch_event_reports()

    query, params = cursor.execute.call_args.args
    assert "WHERE" not in query
    assert params == [50, 0]


def test_fetch_event_report_returns_none_when_absent(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_event_report("does-not-exist")

    assert result is None


def test_fetch_event_report_maps_row_to_dict(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchone.return_value = (
        "evt-1",
        "mFRR_capacity",
        "DK1",
        "up",
        datetime(2026, 7, 16, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 16, 10, 20, tzinfo=UTC),
        {"event_id": "evt-1"},
        True,
        "evt-0",
    )
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_event_report("evt-1")

    assert result["event_id"] == "evt-1"
    assert result["is_correction"] is True
    assert result["corrects_event_id"] == "evt-0"
    query, params = cursor.execute.call_args.args
    assert params == ("evt-1",)


def test_save_trigger_inserts_row(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    trigger = {
        "trigger_type": "price_spike",
        "market": "mFRR_capacity",
        "zone": "DK1",
        "product": "up",
        "value": 5000.0,
        "time": "2026-07-16 17:15:00+00:00",
        "baseline": 1200.0,
        "threshold": 3600.0,
        "details": "z-score=4.10",
        "detected_at": "2026-07-16T17:20:00+00:00",
    }
    db.save_trigger(trigger)

    cursor.execute.assert_called_once()
    query, params = cursor.execute.call_args.args
    assert "INSERT INTO triggers" in query
    assert params[0] == "price_spike"
    assert params[5] == "2026-07-16 17:15:00+00:00"
    conn.commit.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_save_trigger_rolls_back_and_reraises_on_failure(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.execute.side_effect = RuntimeError("insert failed")
    conn.cursor.return_value.__enter__.return_value = cursor

    with pytest.raises(RuntimeError):
        db.save_trigger(
            {
                "trigger_type": "price_spike",
                "market": "m",
                "zone": "z",
                "product": "p",
                "value": 1.0,
                "time": "t",
            }
        )

    conn.rollback.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_fetch_triggers_returns_mapped_rows(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            1,
            "price_spike",
            "mFRR_capacity",
            "DK1",
            "up",
            5000.0,
            "2026-07-16T17:15:00",
            1200.0,
            3600.0,
            "z-score=4.10",
            datetime(2026, 7, 16, 17, 20, tzinfo=UTC),
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_triggers(market="mFRR_capacity", limit=10, offset=0)

    assert result[0]["trigger_type"] == "price_spike"
    assert result[0]["id"] == 1
    query, params = cursor.execute.call_args.args
    assert "market = %s" in query
    assert params == ["mFRR_capacity", 10, 0]


def test_save_bess_run_inserts_header_and_ticks(db, pooled_conn):
    from shared.bess_simulator import BacktestResult, BessConfig, BessTick

    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchone.return_value = (7,)
    conn.cursor.return_value.__enter__.return_value = cursor

    result = BacktestResult(
        zone="DK1",
        start_time=datetime(2026, 7, 16, tzinfo=UTC),
        end_time=datetime(2026, 7, 17, tzinfo=UTC),
        config=BessConfig(),
        ticks=[
            BessTick(
                time=datetime(2026, 7, 16, tzinfo=UTC),
                soc_mwh=1.0,
                soc_fraction=0.5,
                action="idle",
                day_ahead_price=500.0,
                energy_discharged_mwh=0.0,
                arbitrage_revenue_dkk=0.0,
                capacity_reserved_mw=0.3,
                capacity_revenue_dkk=15.0,
                capacity_revenue_by_market={"FCR": 10.0, "aFRR_capacity": 5.0},
                cumulative_arbitrage_revenue_dkk=0.0,
                cumulative_capacity_revenue_dkk=15.0,
                cumulative_total_revenue_dkk=15.0,
            )
        ],
    )

    with patch("shared.db_manager.execute_values") as mock_execute_values:
        run_id = db.save_bess_run(result)

    assert run_id == 7
    insert_run_call = cursor.execute.call_args_list[0]
    assert "INSERT INTO bess_simulation_runs" in insert_run_call.args[0]
    mock_execute_values.assert_called_once()
    tick_query = mock_execute_values.call_args.args[1]
    assert "INSERT INTO bess_simulation_ticks" in tick_query
    conn.commit.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_save_bess_run_skips_tick_insert_when_no_ticks(db, pooled_conn):
    from shared.bess_simulator import BacktestResult, BessConfig

    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchone.return_value = (3,)
    conn.cursor.return_value.__enter__.return_value = cursor

    result = BacktestResult(
        zone="DK1",
        start_time=datetime(2026, 7, 16, tzinfo=UTC),
        end_time=datetime(2026, 7, 17, tzinfo=UTC),
        config=BessConfig(),
        ticks=[],
    )

    with patch("shared.db_manager.execute_values") as mock_execute_values:
        run_id = db.save_bess_run(result)

    assert run_id == 3
    mock_execute_values.assert_not_called()


def test_save_bess_run_rolls_back_and_reraises_on_failure(db, pooled_conn):
    from shared.bess_simulator import BacktestResult, BessConfig

    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.execute.side_effect = RuntimeError("insert failed")
    conn.cursor.return_value.__enter__.return_value = cursor

    result = BacktestResult(
        zone="DK1",
        start_time=datetime(2026, 7, 16, tzinfo=UTC),
        end_time=datetime(2026, 7, 17, tzinfo=UTC),
        config=BessConfig(),
        ticks=[],
    )

    with pytest.raises(RuntimeError):
        db.save_bess_run(result)

    conn.rollback.assert_called_once()
    mock_pool.putconn.assert_called_once_with(conn)


def test_fetch_bess_runs_returns_mapped_rows(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            1,
            "DK1",
            datetime(2026, 7, 16, tzinfo=UTC),
            datetime(2026, 7, 17, tzinfo=UTC),
            {"power_mw": 1.0},
            100.0,
            50.0,
            150.0,
            0.5,
            48,
            datetime(2026, 7, 17, 9, tzinfo=UTC),
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_bess_runs(zone="DK1", limit=10, offset=0)

    assert result[0]["id"] == 1
    assert result[0]["zone"] == "DK1"
    assert result[0]["config"] == {"power_mw": 1.0}
    query, params = cursor.execute.call_args.args
    assert "zone = %s" in query
    assert params == ["DK1", 10, 0]


def test_fetch_bess_runs_decodes_json_string_config(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            1,
            "DK1",
            datetime(2026, 7, 16, tzinfo=UTC),
            datetime(2026, 7, 17, tzinfo=UTC),
            json.dumps({"power_mw": 1.0}),
            100.0,
            50.0,
            150.0,
            0.5,
            48,
            datetime(2026, 7, 17, 9, tzinfo=UTC),
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_bess_runs()

    assert result[0]["config"] == {"power_mw": 1.0}


def test_fetch_bess_run_returns_none_when_absent(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    conn.cursor.return_value.__enter__.return_value = cursor

    assert db.fetch_bess_run(999) is None


def test_fetch_bess_ticks_returns_mapped_rows(db, pooled_conn):
    conn, mock_pool = pooled_conn
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        (
            datetime(2026, 7, 16, tzinfo=UTC),
            1.0,
            0.5,
            "idle",
            500.0,
            0.0,
            0.0,
            0.3,
            15.0,
            {"FCR": 10.0, "aFRR_capacity": 5.0},
            0.0,
            15.0,
            15.0,
        )
    ]
    conn.cursor.return_value.__enter__.return_value = cursor

    result = db.fetch_bess_ticks(1)

    assert result[0]["action"] == "idle"
    assert result[0]["capacity_revenue_by_market"] == {"FCR": 10.0, "aFRR_capacity": 5.0}
    query, params = cursor.execute.call_args.args
    assert "run_id = %s" in query
    assert params == (1,)


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


# --- BESS-eligible datasets (FCR / aFRR capacity) --------------------------


def test_fcr_dk1_and_dk2_datasets_are_registered():
    fcr_dk1 = next(d for d in DATASETS if d.name == "fcr_dk1")
    fcr_dk2 = next(d for d in DATASETS if d.name == "fcr_dk2")
    assert fcr_dk1.market == "FCR"
    assert fcr_dk1.zone == "DK1"
    assert fcr_dk2.market == "FCR"
    assert fcr_dk2.zone_field == "PriceArea"


def test_afrr_reserves_nordic_dataset_is_registered():
    afrr_capacity = next(d for d in DATASETS if d.name == "afrr_reserves_nordic")
    assert afrr_capacity.market == "aFRR_capacity"
    products = {s.product for s in afrr_capacity.series}
    assert products == {"up", "down"}


@patch("shared.db_manager.execute_values")
def test_save_market_data_applies_series_filter_field(mock_execute, db):
    dataset = DatasetConfig(
        name="fcr_dk2_test",
        dataset_id="FcrNdDK2",
        market="FCR",
        time_field="HourUTC",
        zone_field="PriceArea",
        series=[
            SeriesConfig(
                product="price",
                value_field="PriceTotalEUR",
                filter_field="ProductName",
                filter_value="FCR-N",
            )
        ],
    )
    records = [
        {
            "HourUTC": "2026-07-18T21:00:00",
            "PriceArea": "DK2",
            "ProductName": "FCR-D ned",
            "PriceTotalEUR": 1.73,
        },
        {
            "HourUTC": "2026-07-18T21:00:00",
            "PriceArea": "DK2",
            "ProductName": "FCR-N",
            "PriceTotalEUR": 20.0,
        },
    ]

    saved = db.save_market_data(records, dataset)

    assert saved == 1
    values = mock_execute.call_args.args[2]
    assert values[0][4] == 20.0  # only the FCR-N row's price is mapped
