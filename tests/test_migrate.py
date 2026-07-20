"""
Tests for scripts/migrate.py's pure/mockable logic (discovery + the
per-file apply/commit/rollback flow). The actual idempotency claim in that
script's module docstring was verified by hand against a real
TimescaleDB container while building it (run the full init-db/*.sql
sequence twice in a row -- second pass returns only "already exists,
skipping"/no-op NOTICEs, zero errors) -- not something a mocked-psycopg2
unit test can meaningfully re-assert, so this file sticks to what these
tests are good at: file discovery/ordering and the commit-per-file,
stop-on-first-failure control flow, mirroring shared/db_manager.py's
mocked-connection test style (no real DB).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import scripts.migrate as migrate


def _write_sql_files(tmp_path: Path, names: list[str]) -> Path:
    for name in names:
        (tmp_path / name).write_text(f"-- {name}\nSELECT 1;\n")
    return tmp_path


def test_discover_migrations_sorts_by_filename(tmp_path):
    _write_sql_files(tmp_path, ["03-c.sql", "01-a.sql", "02-b.sql"])

    files = migrate.discover_migrations(tmp_path)

    assert [f.name for f in files] == ["01-a.sql", "02-b.sql", "03-c.sql"]


def test_discover_migrations_ignores_non_sql_files(tmp_path):
    _write_sql_files(tmp_path, ["01-a.sql"])
    (tmp_path / "README.md").write_text("not a migration")

    files = migrate.discover_migrations(tmp_path)

    assert [f.name for f in files] == ["01-a.sql"]


def _mock_connect(mock_connect):
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    mock_connect.return_value = conn
    return conn, cursor


def test_run_migrations_applies_every_file_in_order_and_commits_each(tmp_path):
    _write_sql_files(tmp_path, ["02-second.sql", "01-first.sql"])

    with patch("scripts.migrate.psycopg2.connect") as mock_connect:
        conn, cursor = _mock_connect(mock_connect)

        applied = migrate.run_migrations("postgresql://test", tmp_path)

    assert applied == ["01-first.sql", "02-second.sql"]
    assert cursor.execute.call_count == 2
    assert conn.commit.call_count == 2
    conn.close.assert_called_once()


def test_run_migrations_stops_and_rolls_back_on_first_failure(tmp_path):
    _write_sql_files(tmp_path, ["01-first.sql", "02-second.sql"])

    with patch("scripts.migrate.psycopg2.connect") as mock_connect:
        conn, cursor = _mock_connect(mock_connect)
        cursor.execute.side_effect = RuntimeError("syntax error")

        with pytest.raises(RuntimeError):
            migrate.run_migrations("postgresql://test", tmp_path)

    # Only the first file was attempted -- the second never got a chance to
    # apply after the first one's failure.
    assert cursor.execute.call_count == 1
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    conn.close.assert_called_once()


def test_run_migrations_returns_empty_list_when_no_sql_files(tmp_path):
    applied = migrate.run_migrations("postgresql://test", tmp_path)
    assert applied == []


def test_main_exits_when_database_url_unset(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(SystemExit):
        migrate.main([])
