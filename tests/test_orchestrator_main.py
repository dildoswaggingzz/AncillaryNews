import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.rule_engine import Trigger

MAIN_PATH = Path(__file__).parent.parent / "services" / "orchestrator" / "main.py"

spec = importlib.util.spec_from_file_location("orchestrator_main", MAIN_PATH)
orchestrator_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orchestrator_main)

PRICE_SPIKE_TRIGGER = Trigger(
    trigger_type="price_spike",
    market="mFRR EAM",
    zone="DK1",
    product="up",
    value=4850.0,
    time="2026-07-16 17:15:00+00:00",
    baseline=1200.0,
    threshold=3600.0,
    details="z-score=4.10 over 45 historical point(s)",
    detected_at="2026-07-16T17:20:00+00:00",
)

REVISION_TRIGGER = Trigger(
    trigger_type="revision_alert",
    market="mFRR_capacity",
    zone="DK1",
    product="up",
    value=250.0,
    time="2026-07-16 10:00:00+00:00",
    baseline=100.0,
    threshold=5.0,
    details="revised 100.0 -> 250.0",
    detected_at="2026-07-16T10:30:00+00:00",
)

VALID_REPORT = {
    "event_id": "2026-07-16 17:15:00+00:00-DK1-mFRR EAM-up-up",
    "market": "mFRR EAM",
    "zone": "DK1",
    "direction": "up",
    "observation": "Balancing energy price hit 4,850 DKK/MWh vs baseline of 1,200",
    "hard_data_correlates": [
        {"signal": "mFRR EAM price", "value": "4,850 DKK/MWh", "source": "Energinet"}
    ],
    "market_theories": [
        {
            "claim": "Analysts point to low wind + Karlshamn unavailability",
            "source": "EnergiWatch, 2026-07-14",
            "type": "theory",
        }
    ],
    "synthesis": "According to EnergiWatch, low wind output coincided with the price move.",
    "confidence": "medium",
    "data_maturity": "provisional — figures may be revised by Energinet",
}


@pytest.fixture(autouse=True)
def database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")


@pytest.fixture
def db():
    db_mock = MagicMock()
    db_mock.fetch_context_window.return_value = [
        {"time": "2026-07-16 11:15:00+00:00", "value": 1150.0, "source": "Energinet"}
    ]
    db_mock.find_published_report.return_value = None
    return db_mock


@pytest.fixture
def store():
    store_mock = MagicMock()
    store_mock.search_claims = AsyncMock(return_value=[])
    return store_mock


# --- process_trigger ---------------------------------------------------------


async def test_process_trigger_fetches_context_window_and_searches_claims(db, store):
    with patch.object(
        orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=None)
    ):
        await orchestrator_main.process_trigger(PRICE_SPIKE_TRIGGER, db, store)

    db.fetch_context_window.assert_called_once()
    args, kwargs = db.fetch_context_window.call_args
    assert args[0] == "mFRR EAM"
    assert args[1] == "DK1"
    assert args[2] == "up"

    store.search_claims.assert_awaited_once()


async def test_process_trigger_no_op_when_synthesis_returns_none(db, store):
    with patch.object(
        orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=None)
    ):
        await orchestrator_main.process_trigger(PRICE_SPIKE_TRIGGER, db, store)

    db.save_event_report.assert_not_called()


async def test_process_trigger_persists_and_posts_to_slack_on_success(db, store):
    report = dict(VALID_REPORT)
    with (
        patch.object(
            orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=report)
        ),
        patch.object(
            orchestrator_main, "send_event_report_alert", new=AsyncMock(return_value=True)
        ) as mock_slack,
    ):
        await orchestrator_main.process_trigger(PRICE_SPIKE_TRIGGER, db, store)

    db.save_event_report.assert_called_once()
    _, kwargs = db.save_event_report.call_args
    assert kwargs["is_correction"] is False
    assert kwargs["corrects_event_id"] is None
    assert kwargs["report"]["event_id"] == report["event_id"]

    mock_slack.assert_awaited_once()
    posted = mock_slack.call_args.args[0]
    assert posted["is_correction"] is False


async def test_process_trigger_swallows_slack_failure(db, store):
    report = dict(VALID_REPORT)
    with (
        patch.object(
            orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=report)
        ),
        patch.object(
            orchestrator_main,
            "send_event_report_alert",
            new=AsyncMock(side_effect=RuntimeError("slack down")),
        ),
    ):
        # Must not raise.
        await orchestrator_main.process_trigger(PRICE_SPIKE_TRIGGER, db, store)

    db.save_event_report.assert_called_once()


async def test_process_trigger_swallows_persist_failure(db, store):
    report = dict(VALID_REPORT)
    db.save_event_report.side_effect = RuntimeError("db down")
    with (
        patch.object(
            orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=report)
        ),
        patch.object(orchestrator_main, "send_event_report_alert", new=AsyncMock()) as mock_slack,
    ):
        await orchestrator_main.process_trigger(PRICE_SPIKE_TRIGGER, db, store)

    mock_slack.assert_not_awaited()


async def test_process_trigger_skips_when_trigger_time_unparseable(db, store):
    bad_trigger = Trigger(
        trigger_type="price_spike",
        market="mFRR EAM",
        zone="DK1",
        product="up",
        value=1.0,
        time="not-a-real-timestamp",
    )

    with patch.object(orchestrator_main, "synthesize_event_report", new=AsyncMock()) as mock_synth:
        await orchestrator_main.process_trigger(bad_trigger, db, store)

    mock_synth.assert_not_awaited()
    db.fetch_context_window.assert_not_called()


# --- correction events (README §5) -------------------------------------------


async def test_process_trigger_emits_correction_when_revision_alert_has_existing_report(db, store):
    existing_report = {
        "event_id": "2026-07-16 10:00:00+00:00-DK1-mFRR_capacity-up-up",
        "market": "mFRR_capacity",
        "zone": "DK1",
    }
    db.find_published_report.return_value = {
        "event_id": existing_report["event_id"],
        "report": existing_report,
    }
    report = {
        "event_id": "2026-07-16 10:00:00+00:00-DK1-mFRR_capacity-up-up",
        "market": "mFRR_capacity",
        "zone": "DK1",
        "direction": "up",
        "observation": "Revised value observed",
        "hard_data_correlates": [],
        "market_theories": [],
        "synthesis": "The figure was revised.",
        "confidence": "medium",
        "data_maturity": "provisional",
    }

    with (
        patch.object(
            orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=report)
        ),
        patch.object(orchestrator_main, "send_event_report_alert", new=AsyncMock()) as mock_slack,
    ):
        await orchestrator_main.process_trigger(REVISION_TRIGGER, db, store)

    db.find_published_report.assert_called_once()
    db.save_event_report.assert_called_once()
    _, kwargs = db.save_event_report.call_args

    assert kwargs["is_correction"] is True
    assert kwargs["corrects_event_id"] == existing_report["event_id"]
    # A new, distinct event_id -- never overwrites the original.
    assert kwargs["event_id"] != existing_report["event_id"]
    assert "CORRECTION" in kwargs["report"]["observation"]

    posted = mock_slack.call_args.args[0]
    assert posted["is_correction"] is True
    assert posted["corrects_event_id"] == existing_report["event_id"]


async def test_process_trigger_revision_alert_without_existing_report_is_not_a_correction(
    db, store
):
    db.find_published_report.return_value = None
    report = dict(VALID_REPORT)

    with (
        patch.object(
            orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=report)
        ),
        patch.object(orchestrator_main, "send_event_report_alert", new=AsyncMock()),
    ):
        await orchestrator_main.process_trigger(REVISION_TRIGGER, db, store)

    _, kwargs = db.save_event_report.call_args
    assert kwargs["is_correction"] is False
    assert kwargs["corrects_event_id"] is None


async def test_non_revision_trigger_never_checks_for_existing_report(db, store):
    report = dict(VALID_REPORT)
    with (
        patch.object(
            orchestrator_main, "synthesize_event_report", new=AsyncMock(return_value=report)
        ),
        patch.object(orchestrator_main, "send_event_report_alert", new=AsyncMock()),
    ):
        await orchestrator_main.process_trigger(PRICE_SPIKE_TRIGGER, db, store)

    db.find_published_report.assert_not_called()


# --- run_synthesis_cycle ------------------------------------------------------


async def test_run_synthesis_cycle_evaluates_rule_engine_and_processes_each_trigger():
    with (
        patch.object(orchestrator_main, "DatabaseManager") as mock_db_cls,
        patch.object(orchestrator_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(orchestrator_main, "QdrantStore") as mock_store_cls,
        patch.object(
            orchestrator_main,
            "run_rule_engine",
            new=AsyncMock(return_value=[PRICE_SPIKE_TRIGGER, REVISION_TRIGGER]),
        ),
        patch.object(orchestrator_main, "process_trigger", new=AsyncMock()) as mock_process,
    ):
        mock_db_instance = MagicMock()
        mock_db_cls.return_value = mock_db_instance

        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await orchestrator_main.run_synthesis_cycle()

    mock_store_instance.ensure_collection.assert_awaited_once()
    assert mock_process.await_count == 2
    mock_qdrant_instance.close.assert_awaited_once()
    mock_db_instance.close.assert_called_once()


async def test_run_synthesis_cycle_survives_rule_engine_failure():
    with (
        patch.object(orchestrator_main, "DatabaseManager") as mock_db_cls,
        patch.object(orchestrator_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(orchestrator_main, "QdrantStore") as mock_store_cls,
        patch.object(
            orchestrator_main, "run_rule_engine", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        patch.object(orchestrator_main, "process_trigger", new=AsyncMock()) as mock_process,
    ):
        mock_db_instance = MagicMock()
        mock_db_cls.return_value = mock_db_instance

        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await orchestrator_main.run_synthesis_cycle()

    mock_process.assert_not_awaited()
    mock_qdrant_instance.close.assert_awaited_once()
    mock_db_instance.close.assert_called_once()


async def test_run_synthesis_cycle_continues_after_one_trigger_synthesis_fails():
    with (
        patch.object(orchestrator_main, "DatabaseManager") as mock_db_cls,
        patch.object(orchestrator_main, "AsyncQdrantClient") as mock_qdrant_cls,
        patch.object(orchestrator_main, "QdrantStore") as mock_store_cls,
        patch.object(
            orchestrator_main,
            "run_rule_engine",
            new=AsyncMock(return_value=[PRICE_SPIKE_TRIGGER, REVISION_TRIGGER]),
        ),
        patch.object(
            orchestrator_main,
            "process_trigger",
            new=AsyncMock(side_effect=[RuntimeError("boom"), None]),
        ) as mock_process,
    ):
        mock_db_instance = MagicMock()
        mock_db_cls.return_value = mock_db_instance

        mock_qdrant_instance = MagicMock()
        mock_qdrant_instance.close = AsyncMock()
        mock_qdrant_cls.return_value = mock_qdrant_instance

        mock_store_instance = MagicMock()
        mock_store_instance.ensure_collection = AsyncMock()
        mock_store_cls.return_value = mock_store_instance

        await orchestrator_main.run_synthesis_cycle()

    assert mock_process.await_count == 2
    mock_qdrant_instance.close.assert_awaited_once()
    mock_db_instance.close.assert_called_once()
