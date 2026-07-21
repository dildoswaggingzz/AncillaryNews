"""
Tests for scripts/backfill_history.py's CLI-facing summary/exit-code
behavior, in particular truncation reporting (the gap this file was added
to close -- see the PR description for the real incident: a 4.6-year
`fcr_dk2` backfill silently dropped 88 days across 4 truncated chunks
because `shared.backfill.run_backfill`'s `chunks_truncated`/`any_truncated`
signal was already computed and already surfaced by the JSON API route and
the dashboard template, but never by this script's own operator-facing
summary or exit code).

Mocks `shared.backfill.run_backfill` entirely -- no real HTTP/DB, mirroring
tests/test_backfill.py's own `run_backfill`-level tests (`fake_backfill_dataset`
et al.), just one layer up.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import scripts.backfill_history as backfill_history


def _dataset_result(name: str, *, chunks_truncated=0, chunks_failed=0, truncated_windows=None):
    return {
        "dataset": name,
        "dataset_id": f"{name}Id",
        "chunks_fetched": 2,
        "chunks_failed": chunks_failed,
        "chunks_truncated": chunks_truncated,
        "truncated_windows": truncated_windows or [],
        "records_fetched": 100,
        "rows_saved": 100,
        "earliest_record_time": "2026-01-01T00:00:00",
        "latest_record_time": "2026-01-02T00:00:00",
    }


def _summary(datasets):
    total_truncated = sum(d["chunks_truncated"] for d in datasets)
    return {
        "start": datetime(2026, 1, 1, tzinfo=UTC),
        "end": datetime(2026, 1, 2, tzinfo=UTC),
        "datasets": datasets,
        "total_rows_saved": sum(d["rows_saved"] for d in datasets),
        "total_chunks_truncated": total_truncated,
        "any_truncated": total_truncated > 0,
    }


# --- _run: per-dataset and aggregate summary logging ------------------------


async def test_run_reports_truncation_in_per_dataset_and_aggregate_summary(monkeypatch, caplog):
    windows = [{"start": datetime(2026, 1, 1, tzinfo=UTC), "end": datetime(2026, 1, 8, tzinfo=UTC)}]
    summary = _summary(
        [
            _dataset_result("fcr_dk2", chunks_truncated=4, truncated_windows=windows * 4),
            _dataset_result("day_ahead_prices"),
        ]
    )
    monkeypatch.setattr(backfill_history, "run_backfill", AsyncMock(return_value=summary))
    args = backfill_history.parse_args(["--days", "30"])

    with caplog.at_level("INFO"):
        result = await backfill_history._run(args)

    assert result == summary
    assert "fcr_dk2" in caplog.text
    assert "4 truncated" in caplog.text
    # The affected window(s) must be named, not just a count -- an operator
    # needs to know *which* dates to re-run.
    assert "2026-01-01" in caplog.text
    assert "--chunk-days" in caplog.text
    # The aggregate line must also carry the truncation count, not just
    # total_rows_saved -- this is the exact gap the incident exposed.
    assert "1 chunk(s) truncated" in caplog.text or "4 chunk(s) truncated" in caplog.text


async def test_run_reports_no_truncation_mention_on_clean_run(monkeypatch, caplog):
    """Every per-dataset line always states its (possibly zero) truncated-chunk count -- but a
    clean run must not additionally emit the truncation warning line (affected windows, re-run
    suggestion) since there is nothing to re-run."""
    summary = _summary([_dataset_result("fcr_dk1"), _dataset_result("day_ahead_prices")])
    monkeypatch.setattr(backfill_history, "run_backfill", AsyncMock(return_value=summary))
    args = backfill_history.parse_args(["--days", "30"])

    with caplog.at_level("INFO"):
        await backfill_history._run(args)

    assert "0 truncated" in caplog.text
    assert "(0 chunk(s) truncated)" in caplog.text
    assert "truncated at CHUNK_LIMIT" not in caplog.text
    assert "affected window" not in caplog.text.lower()


# --- main(): exit code -------------------------------------------------------


def test_main_exits_nonzero_and_names_dataset_when_truncated(monkeypatch, caplog):
    windows = [{"start": datetime(2026, 1, 1, tzinfo=UTC), "end": datetime(2026, 1, 8, tzinfo=UTC)}]
    summary = _summary(
        [_dataset_result("fcr_dk2", chunks_truncated=4, truncated_windows=windows * 4)]
    )
    monkeypatch.setattr(backfill_history, "run_backfill", AsyncMock(return_value=summary))

    with caplog.at_level("INFO"), pytest.raises(SystemExit) as exc_info:
        backfill_history.main(["--days", "30"])

    assert exc_info.value.code != 0
    assert "fcr_dk2" in caplog.text
    assert "--chunk-days" in caplog.text


def test_main_exits_nonzero_when_chunks_failed(monkeypatch, caplog):
    summary = _summary([_dataset_result("fcr_dk1", chunks_failed=1)])
    monkeypatch.setattr(backfill_history, "run_backfill", AsyncMock(return_value=summary))

    with caplog.at_level("INFO"), pytest.raises(SystemExit) as exc_info:
        backfill_history.main(["--days", "30"])

    assert exc_info.value.code != 0
    assert "fcr_dk1" in caplog.text


def test_main_exits_zero_and_reports_clean_run(monkeypatch, caplog):
    summary = _summary([_dataset_result("fcr_dk1"), _dataset_result("day_ahead_prices")])
    monkeypatch.setattr(backfill_history, "run_backfill", AsyncMock(return_value=summary))

    with caplog.at_level("INFO"):
        result = backfill_history.main(["--days", "30"])

    assert result is None
    assert "clean" in caplog.text.lower()
