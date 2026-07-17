#!/usr/bin/env python
"""
One-time/occasional historical backfill CLI for the BESS backtest
simulator's datasets. See `shared/backfill.py` for the full mechanism,
rationale, and idempotency notes -- this script is a thin argparse wrapper
around `shared.backfill.run_backfill`, nothing more.

Not part of any always-on service loop (unlike `services/ingestor/main.py`'s
scheduled poller) -- run this manually, whenever a wider historical window
is needed for a BESS backtest (`shared/bess_simulator.py`).

Usage (needs `DATABASE_URL` pointed at a reachable Postgres/TimescaleDB
instance -- e.g. against docker-compose's mapped port for a local run):

    DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \\
        poetry run python scripts/backfill_history.py --days 30

    # Explicit window instead of --days:
    poetry run python scripts/backfill_history.py \\
        --start 2025-10-01 --end 2025-11-01

    # Only a subset of the BESS-relevant datasets:
    poetry run python scripts/backfill_history.py \\
        --datasets fcr_dk1,day_ahead_prices --days 90

Safe to re-run against an overlapping or identical window -- see
`shared/backfill.py`'s module docstring on `market_data_history`'s
append-only revision design (a re-run adds new `fetched_at` revisions, it
never raises or silently no-ops on already-backfilled data).
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# This repo has no __init__.py / package-mode (see pyproject.toml's
# package-mode = false), so running this script directly (not via `python
# -m`) needs the repo root on sys.path for `shared.backfill` to import --
# same reason services/*/main.py add nothing extra (they're launched with
# PYTHONPATH=/app inside Docker instead; this script is meant for a local,
# non-Docker `poetry run` invocation, so it does the equivalent itself).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.backfill import (  # noqa: E402
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_CHUNK_DAYS,
    bess_datasets,
    run_backfill,
)
from shared.logging_config import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--start",
        type=_parse_datetime,
        default=None,
        help="Backfill window start (ISO 8601, e.g. 2025-10-01). Defaults to --days before --end.",
    )
    parser.add_argument(
        "--end",
        type=_parse_datetime,
        default=None,
        help="Backfill window end (ISO 8601). Defaults to now.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_BACKFILL_DAYS,
        help=(
            f"Days of history to backfill when --start is omitted "
            f"(default: {DEFAULT_BACKFILL_DAYS})."
        ),
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=DEFAULT_CHUNK_DAYS,
        help=f"Date-range chunk size per Energinet request (default: {DEFAULT_CHUNK_DAYS}).",
    )
    parser.add_argument(
        "--datasets",
        type=str,
        default=None,
        help="Comma-separated subset of BESS dataset names to backfill (default: all of them -- "
        + ", ".join(d.name for d in bess_datasets())
        + ").",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> dict:
    end = args.end or datetime.now(UTC)
    start = args.start or (end - timedelta(days=args.days))
    dataset_names = [n.strip() for n in args.datasets.split(",")] if args.datasets else None

    logger.info("Starting backfill for [%s, %s)...", start, end)
    summary = await run_backfill(
        start, end, dataset_names=dataset_names, chunk_days=args.chunk_days
    )

    for r in summary["datasets"]:
        logger.info(
            "%s: %d row(s) saved (%d chunk(s), %d failed), records span [%s, %s]",
            r["dataset"],
            r["rows_saved"],
            r["chunks_fetched"],
            r["chunks_failed"],
            r["earliest_record_time"],
            r["latest_record_time"],
        )
    logger.info(
        "Backfill complete: %d total row(s) saved across %d dataset(s).",
        summary["total_rows_saved"],
        len(summary["datasets"]),
    )
    return summary


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
