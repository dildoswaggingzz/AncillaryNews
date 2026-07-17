from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from shared.bess_estimator import (
    DEFAULT_ZONES,
    ILLUSTRATIVE_CONFIGS,
    MORNING_BRIEF_RUN_LABEL,
    run_illustrative_backtests,
)
from shared.bess_simulator import BacktestResult, BessTick

START = datetime(2026, 6, 17, tzinfo=UTC)
END = datetime(2026, 7, 17, tzinfo=UTC)


def _result_with_ticks(zone: str, config, cap_binding: bool) -> BacktestResult:
    tick = BessTick(
        time=START,
        soc_mwh=1.0,
        soc_fraction=0.5,
        action="discharge",
        day_ahead_price=100.0,
        energy_discharged_mwh=1.0,
        arbitrage_revenue_dkk=100.0,
        capacity_reserved_mw=0.0,
        capacity_revenue_dkk=0.0,
        capacity_revenue_by_market={},
        cumulative_arbitrage_revenue_dkk=100.0,
        cumulative_capacity_revenue_dkk=0.0,
        cumulative_total_revenue_dkk=100.0,
        cycle_cap_binding=cap_binding,
    )
    return BacktestResult(zone=zone, start_time=START, end_time=END, config=config, ticks=[tick])


def test_illustrative_configs_has_two_entries():
    assert len(ILLUSTRATIVE_CONFIGS) == 2
    labels = [label for label, _ in ILLUSTRATIVE_CONFIGS]
    assert "Small commercial (1 MW / 2 MWh)" in labels
    assert "Utility-scale (10 MW / 40 MWh)" in labels


def test_run_illustrative_backtests_runs_every_config_x_zone_combo():
    db = MagicMock()
    db.save_bess_run.side_effect = range(1, 100)

    with patch("shared.bess_estimator.run_backtest") as mock_run_backtest:
        mock_run_backtest.side_effect = lambda db_arg, zone, start, end, config: _result_with_ticks(
            zone, config, cap_binding=False
        )
        summaries = run_illustrative_backtests(db, DEFAULT_ZONES, start_time=START, end_time=END)

    assert len(summaries) == len(ILLUSTRATIVE_CONFIGS) * len(DEFAULT_ZONES)
    zones_seen = {s["zone"] for s in summaries}
    assert zones_seen == set(DEFAULT_ZONES)
    assert mock_run_backtest.call_count == len(ILLUSTRATIVE_CONFIGS) * len(DEFAULT_ZONES)


def test_run_illustrative_backtests_persists_with_morning_brief_label():
    db = MagicMock()
    db.save_bess_run.return_value = 7

    with patch("shared.bess_estimator.run_backtest") as mock_run_backtest:
        mock_run_backtest.side_effect = lambda db_arg, zone, start, end, config: _result_with_ticks(
            zone, config, cap_binding=False
        )
        run_illustrative_backtests(db, ("DK1",), start_time=START, end_time=END)

    for call in db.save_bess_run.call_args_list:
        assert call.kwargs["label"] == MORNING_BRIEF_RUN_LABEL


def test_run_illustrative_backtests_applies_max_cycles_per_day_override():
    db = MagicMock()
    db.save_bess_run.return_value = 1
    captured_configs = []

    with patch("shared.bess_estimator.run_backtest") as mock_run_backtest:

        def fake_run_backtest(db_arg, zone, start, end, config):
            captured_configs.append(config)
            return _result_with_ticks(zone, config, cap_binding=False)

        mock_run_backtest.side_effect = fake_run_backtest
        run_illustrative_backtests(
            db, ("DK1",), start_time=START, end_time=END, max_cycles_per_day=0.75
        )

    assert all(c.max_cycles_per_day == 0.75 for c in captured_configs)


def test_run_illustrative_backtests_surfaces_cycle_cap_was_binding():
    db = MagicMock()
    db.save_bess_run.return_value = 1

    with patch("shared.bess_estimator.run_backtest") as mock_run_backtest:
        mock_run_backtest.side_effect = lambda db_arg, zone, start, end, config: _result_with_ticks(
            zone, config, cap_binding=True
        )
        summaries = run_illustrative_backtests(db, ("DK1",), start_time=START, end_time=END)

    assert all(s["cycle_cap_was_binding"] is True for s in summaries)


def test_run_illustrative_backtests_summary_shape():
    db = MagicMock()
    db.save_bess_run.return_value = 42

    with patch("shared.bess_estimator.run_backtest") as mock_run_backtest:
        mock_run_backtest.side_effect = lambda db_arg, zone, start, end, config: _result_with_ticks(
            zone, config, cap_binding=False
        )
        summaries = run_illustrative_backtests(db, ("DK1",), start_time=START, end_time=END)

    summary = summaries[0]
    assert summary["run_id"] == 42
    assert "config_label" in summary
    assert "total_revenue_dkk" in summary
    assert "total_arbitrage_revenue_dkk" in summary
    assert "total_capacity_revenue_dkk" in summary
    assert "full_cycle_equivalents" in summary
