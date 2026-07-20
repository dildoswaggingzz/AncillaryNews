import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.datasets import DATASETS
from shared.db_manager import SaveResult

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
        # Sensible default so tests that don't care about the exact
        # SaveResult shape (most of them) don't have to configure it --
        # real `save_market_data` always returns a SaveResult now (Stage 2),
        # never a bare int/None.
        mock_instance.save_market_data.return_value = SaveResult(
            total=1, by_series={"fake_market:fake_product": 1}
        )
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
        SaveResult(total=1, by_series={"fake_market:fake_product": 1})
        for _ in range(len(DATASETS) - 1)
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


# --- metrics (Phase 6 production readiness) ---------------------------------


def test_metrics_are_registered_and_exposition_includes_expected_names():
    from prometheus_client import generate_latest

    output = generate_latest().decode()

    assert "ingestor_cycle_duration_seconds" in output
    assert "ingestor_dataset_poll_total" in output


# --- schema-completeness startup check (Stage 0) ----------------------------


def test_warn_on_missing_schema_columns_logs_warning(db_manager_cls, caplog):
    _, db_instance = db_manager_cls
    db_instance.check_expected_columns.return_value = [
        ("bess_simulation_runs", "total_capacity_revenue_eur")
    ]

    with caplog.at_level("WARNING"):
        ingestor_main._warn_on_missing_schema_columns()

    assert "missing 1 expected column" in caplog.text
    assert "scripts/migrate.py" in caplog.text
    db_instance.close.assert_called_once()


def test_warn_on_missing_schema_columns_silent_when_complete(db_manager_cls, caplog):
    _, db_instance = db_manager_cls
    db_instance.check_expected_columns.return_value = []

    with caplog.at_level("WARNING"):
        ingestor_main._warn_on_missing_schema_columns()

    assert caplog.text == ""
    db_instance.close.assert_called_once()


async def test_cycle_records_dataset_poll_outcomes(db_manager_cls):
    """A successful fetch+save increments the success counter for that dataset."""
    _, db_instance = db_manager_cls
    dataset = DATASETS[0]

    before = ingestor_main.DATASET_POLL_TOTAL.labels(
        dataset=dataset.name, status="success"
    )._value.get()

    async def fake_fetch(endpoint, params=None):
        return {"records": [{"fake": "record"}]}

    with (
        patch.object(
            ingestor_main.BaseIngestor, "fetch_data", new=AsyncMock(side_effect=fake_fetch)
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()),
    ):
        await ingestor_main.run_ingestion_cycle()

    after = ingestor_main.DATASET_POLL_TOTAL.labels(
        dataset=dataset.name, status="success"
    )._value.get()
    assert after == before + 1


# --- SERIES_ROWS_TOTAL / zero_rows status (Stage 2 guardrail) ----------------


async def test_cycle_increments_series_rows_total_per_by_series_entry(db_manager_cls):
    _, db_instance = db_manager_cls
    dataset = DATASETS[0]
    db_instance.save_market_data.return_value = SaveResult(
        total=3, by_series={"real_market:up": 2, "real_market:down": 1}
    )

    before_up = ingestor_main.SERIES_ROWS_TOTAL.labels(
        dataset=dataset.name, market="real_market", product="up"
    )._value.get()
    before_down = ingestor_main.SERIES_ROWS_TOTAL.labels(
        dataset=dataset.name, market="real_market", product="down"
    )._value.get()

    async def fake_fetch(endpoint, params=None):
        return {"records": [{"fake": "record"}]}

    with (
        patch.object(
            ingestor_main.BaseIngestor, "fetch_data", new=AsyncMock(side_effect=fake_fetch)
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()),
    ):
        await ingestor_main.run_ingestion_cycle()

    after_up = ingestor_main.SERIES_ROWS_TOTAL.labels(
        dataset=dataset.name, market="real_market", product="up"
    )._value.get()
    after_down = ingestor_main.SERIES_ROWS_TOTAL.labels(
        dataset=dataset.name, market="real_market", product="down"
    )._value.get()
    assert after_up == before_up + 2
    assert after_down == before_down + 1


async def test_cycle_increments_series_rows_total_even_for_a_zero_entry(db_manager_cls):
    """
    A configured-but-unpopulated series (SaveResult.by_series' 0 entries)
    must still touch the counter (`.inc(0)`), not be skipped -- that's what
    makes it a real, queryable/alertable Prometheus time series instead of
    one that simply never exists (see services/ingestor/main.py's
    SERIES_ROWS_TOTAL comment).
    """
    _, db_instance = db_manager_cls
    dataset = DATASETS[0]
    db_instance.save_market_data.return_value = SaveResult(
        total=0, by_series={"real_market:typo_product": 0}
    )

    async def fake_fetch(endpoint, params=None):
        return {"records": [{"fake": "record"}]}

    with (
        patch.object(
            ingestor_main.BaseIngestor, "fetch_data", new=AsyncMock(side_effect=fake_fetch)
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()),
    ):
        await ingestor_main.run_ingestion_cycle()

    # Merely fetching the labelled child registers it in the exposition
    # output at 0 -- a series that was never touched at all wouldn't appear
    # here (this is exactly the "flat 0 is visible, absent is not" distinction).
    from prometheus_client import generate_latest

    output = generate_latest().decode()
    assert (
        f'ingestor_series_rows_total{{dataset="{dataset.name}",market="real_market",'
        'product="typo_product"} 0.0' in output
    )


async def test_cycle_reports_zero_rows_status_when_save_result_total_is_zero(db_manager_cls):
    _, db_instance = db_manager_cls
    dataset = DATASETS[0]
    db_instance.save_market_data.return_value = SaveResult(
        total=0, by_series={"real_market:typo_product": 0}
    )

    before = ingestor_main.DATASET_POLL_TOTAL.labels(
        dataset=dataset.name, status="zero_rows"
    )._value.get()

    async def fake_fetch(endpoint, params=None):
        return {"records": [{"fake": "record"}]}

    with (
        patch.object(
            ingestor_main.BaseIngestor, "fetch_data", new=AsyncMock(side_effect=fake_fetch)
        ),
        patch.object(ingestor_main.BaseIngestor, "close", new=AsyncMock()),
    ):
        await ingestor_main.run_ingestion_cycle()

    after = ingestor_main.DATASET_POLL_TOTAL.labels(
        dataset=dataset.name, status="zero_rows"
    )._value.get()
    assert after == before + 1
