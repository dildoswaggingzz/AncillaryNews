from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.rule_engine import (
    MIN_HISTORY_POINTS,
    check_negative_or_zero,
    check_price_spike,
    check_revisions,
    check_zone_divergence,
    run_rule_engine,
)

BASE_TIME = datetime(2026, 6, 1, tzinfo=UTC)


def _rows(values: list[float], fetched_at_offset_hours: int = 0) -> list[dict]:
    """
    Builds raw market_data_history-shaped rows, one per value, one hour apart,
    each with a single fetched_at revision. Returned ordered time DESC (as
    DatabaseManager.fetch_history would return them).
    """
    rows = [
        {
            "time": BASE_TIME + timedelta(hours=i),
            "value": v,
            "fetched_at": BASE_TIME + timedelta(hours=i + fetched_at_offset_hours),
        }
        for i, v in enumerate(values)
    ]
    return list(reversed(rows))


# --- price spike -----------------------------------------------------------


def test_price_spike_fires_on_extreme_outlier():
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS)]
    rows = _rows([*normal, 5000.0])

    trigger = check_price_spike("mFRR_capacity", "DK1", "up", rows)

    assert trigger is not None
    assert trigger.trigger_type == "price_spike"
    assert trigger.value == 5000.0
    assert trigger.market == "mFRR_capacity"
    assert trigger.zone == "DK1"
    assert trigger.product == "up"


def test_price_spike_does_not_fire_on_normal_data():
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS + 1)]
    rows = _rows(normal)

    trigger = check_price_spike("mFRR_capacity", "DK1", "up", rows)

    assert trigger is None


def test_price_spike_skips_gracefully_on_insufficient_history(caplog):
    rows = _rows([100.0, 105.0, 110.0])

    with caplog.at_level("INFO"):
        trigger = check_price_spike("mFRR_capacity", "DK1", "up", rows)

    assert trigger is None
    assert "insufficient history" in caplog.text


def test_price_spike_handles_zero_variance_history_without_crashing():
    rows = _rows([100.0] * (MIN_HISTORY_POINTS + 1))

    trigger = check_price_spike("mFRR_capacity", "DK1", "up", rows)

    assert trigger is None


# --- negative/zero price -----------------------------------------------------


def test_negative_or_zero_fires_when_rare_historically():
    normal = [50.0 + (i % 3) for i in range(MIN_HISTORY_POINTS)]
    rows = _rows([*normal, -10.0])

    trigger = check_negative_or_zero("imbalance", "DK1", "imbalance_price", rows)

    assert trigger is not None
    assert trigger.trigger_type == "negative_or_zero_price"
    assert trigger.value == -10.0


def test_negative_or_zero_does_not_fire_when_common_historically():
    # Half the history is already non-positive -> not rare, don't flag.
    values = [(-5.0 if i % 2 == 0 else 20.0) for i in range(MIN_HISTORY_POINTS)]
    rows = _rows([*values, 0.0])

    trigger = check_negative_or_zero("imbalance", "DK1", "imbalance_price", rows)

    assert trigger is None


def test_negative_or_zero_does_not_fire_on_positive_latest_value():
    normal = [50.0 + (i % 3) for i in range(MIN_HISTORY_POINTS + 1)]
    rows = _rows(normal)

    trigger = check_negative_or_zero("imbalance", "DK1", "imbalance_price", rows)

    assert trigger is None


def test_negative_or_zero_skips_gracefully_on_insufficient_history(caplog):
    rows = _rows([1.0, 2.0, -1.0])

    with caplog.at_level("INFO"):
        trigger = check_negative_or_zero("imbalance", "DK1", "imbalance_price", rows)

    assert trigger is None
    assert "insufficient history" in caplog.text


# --- zone divergence ---------------------------------------------------------


def test_zone_divergence_fires_on_extreme_split():
    dk1_normal = [100.0 + (i % 5) * 0.5 for i in range(MIN_HISTORY_POINTS)]
    dk2_normal = [100.0 - (i % 5) * 0.3 for i in range(MIN_HISTORY_POINTS)]
    dk1 = _rows([*dk1_normal, 2000.0])
    dk2 = _rows([*dk2_normal, 100.0])

    trigger = check_zone_divergence("mFRR_capacity", "up", dk1, dk2)

    assert trigger is not None
    assert trigger.trigger_type == "zone_divergence"
    assert trigger.zone == "DK1_vs_DK2"


def test_zone_divergence_does_not_fire_on_normal_data():
    dk1 = _rows([100.0 + (i % 5) * 0.5 for i in range(MIN_HISTORY_POINTS + 1)])
    dk2 = _rows([98.0 - (i % 5) * 0.3 for i in range(MIN_HISTORY_POINTS + 1)])

    trigger = check_zone_divergence("mFRR_capacity", "up", dk1, dk2)

    assert trigger is None


def test_zone_divergence_skips_gracefully_on_insufficient_paired_history(caplog):
    dk1 = _rows([100.0, 105.0])
    dk2 = _rows([98.0, 102.0])

    with caplog.at_level("INFO"):
        trigger = check_zone_divergence("mFRR_capacity", "up", dk1, dk2)

    assert trigger is None
    assert "insufficient paired history" in caplog.text


# --- revision alert -----------------------------------------------------------


def test_revision_alert_fires_when_later_fetch_differs_beyond_tolerance():
    rows = [
        {"time": BASE_TIME, "value": 100.0, "fetched_at": BASE_TIME},
        {"time": BASE_TIME, "value": 250.0, "fetched_at": BASE_TIME + timedelta(hours=1)},
    ]

    triggers = check_revisions("mFRR_capacity", "DK1", "up", rows)

    assert len(triggers) == 1
    assert triggers[0].trigger_type == "revision_alert"
    assert triggers[0].baseline == 100.0
    assert triggers[0].value == 250.0


def test_revision_alert_does_not_fire_within_tolerance():
    rows = [
        {"time": BASE_TIME, "value": 100.0, "fetched_at": BASE_TIME},
        {"time": BASE_TIME, "value": 100.5, "fetched_at": BASE_TIME + timedelta(hours=1)},
    ]

    triggers = check_revisions("mFRR_capacity", "DK1", "up", rows)

    assert triggers == []


def test_revision_alert_does_not_fire_with_single_fetch():
    rows = [{"time": BASE_TIME, "value": 100.0, "fetched_at": BASE_TIME}]

    triggers = check_revisions("mFRR_capacity", "DK1", "up", rows)

    assert triggers == []


def test_revision_alert_evaluates_each_time_independently():
    t2 = BASE_TIME + timedelta(hours=1)
    rows = [
        {"time": BASE_TIME, "value": 100.0, "fetched_at": BASE_TIME},
        {"time": BASE_TIME, "value": 500.0, "fetched_at": BASE_TIME + timedelta(minutes=30)},
        {"time": t2, "value": 200.0, "fetched_at": t2},
    ]

    triggers = check_revisions("mFRR_capacity", "DK1", "up", rows)

    assert len(triggers) == 1
    assert triggers[0].time == str(BASE_TIME)


# --- run_rule_engine orchestration -------------------------------------------


async def test_run_rule_engine_returns_empty_when_no_series():
    db = MagicMock()
    db.fetch_distinct_series.return_value = []

    triggers = await run_rule_engine(db)

    assert triggers == []


async def test_run_rule_engine_fires_and_alerts_slack():
    db = MagicMock()
    db.fetch_distinct_series.return_value = [("mFRR_capacity", "DK1", "up")]
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS)]
    db.fetch_history.return_value = _rows([*normal, 5000.0])

    with patch("shared.rule_engine.send_slack_alert", new=AsyncMock()) as mock_send:
        triggers = await run_rule_engine(db)

    assert len(triggers) == 1
    assert triggers[0].trigger_type == "price_spike"
    mock_send.assert_awaited_once()


async def test_run_rule_engine_checks_zone_divergence_pairs_once():
    db = MagicMock()
    db.fetch_distinct_series.return_value = [
        ("mFRR_capacity", "DK1", "up"),
        ("mFRR_capacity", "DK2", "up"),
    ]
    dk1_normal = [100.0 + (i % 5) * 0.5 for i in range(MIN_HISTORY_POINTS)]
    dk2_normal = [100.0 - (i % 5) * 0.3 for i in range(MIN_HISTORY_POINTS)]

    def fake_history(market, zone, product, limit=1000):
        if zone == "DK1":
            return _rows([*dk1_normal, 2000.0])
        return _rows([*dk2_normal, 100.0])

    db.fetch_history.side_effect = fake_history

    with patch("shared.rule_engine.send_slack_alert", new=AsyncMock()):
        triggers = await run_rule_engine(db)

    divergence_triggers = [t for t in triggers if t.trigger_type == "zone_divergence"]
    assert len(divergence_triggers) == 1


async def test_run_rule_engine_continues_when_slack_send_fails():
    db = MagicMock()
    db.fetch_distinct_series.return_value = [("mFRR_capacity", "DK1", "up")]
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS)]
    db.fetch_history.return_value = _rows([*normal, 5000.0])

    with patch(
        "shared.rule_engine.send_slack_alert", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        triggers = await run_rule_engine(db)

    assert len(triggers) == 1


async def test_run_rule_engine_persists_each_fired_trigger():
    db = MagicMock()
    db.fetch_distinct_series.return_value = [("mFRR_capacity", "DK1", "up")]
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS)]
    db.fetch_history.return_value = _rows([*normal, 5000.0])

    with patch("shared.rule_engine.send_slack_alert", new=AsyncMock()):
        triggers = await run_rule_engine(db)

    assert len(triggers) == 1
    db.save_trigger.assert_called_once_with(triggers[0].to_dict())


async def test_run_rule_engine_continues_when_trigger_persistence_fails():
    db = MagicMock()
    db.fetch_distinct_series.return_value = [("mFRR_capacity", "DK1", "up")]
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS)]
    db.fetch_history.return_value = _rows([*normal, 5000.0])
    db.save_trigger.side_effect = RuntimeError("db down")

    with patch("shared.rule_engine.send_slack_alert", new=AsyncMock()) as mock_send:
        triggers = await run_rule_engine(db)

    assert len(triggers) == 1
    mock_send.assert_awaited_once()


@pytest.fixture(autouse=True)
def _no_real_slack_webhook(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)


# --- metrics (Phase 6 production readiness) ---------------------------------


async def test_run_rule_engine_increments_trigger_fired_counter():
    from shared.rule_engine import TRIGGER_FIRED_TOTAL

    before = TRIGGER_FIRED_TOTAL.labels(trigger_type="price_spike")._value.get()

    db = MagicMock()
    db.fetch_distinct_series.return_value = [("mFRR_capacity", "DK1", "up")]
    normal = [100.0 + (i % 5) for i in range(MIN_HISTORY_POINTS)]
    db.fetch_history.return_value = _rows([*normal, 5000.0])

    with patch("shared.rule_engine.send_slack_alert", new=AsyncMock()):
        await run_rule_engine(db)

    after = TRIGGER_FIRED_TOTAL.labels(trigger_type="price_spike")._value.get()
    assert after == before + 1
