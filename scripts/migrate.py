#!/usr/bin/env python
"""
Schema migration runner: applies every `init-db/*.sql` file, in filename
order, against `DATABASE_URL`.

**Why this exists:** `docker-compose.yml` mounts `./init-db` at
`/docker-entrypoint-initdb.d`, which is Postgres's own convention for
"run these files once, only against a brand-new, empty data directory". On
any *pre-existing* `pgdata` volume, a file added to `init-db/` after that
volume was first created never runs on its own -- discovered live: `init-db/
06-bess-afrr-activation.sql`'s `ALTER TABLE ... ADD COLUMN` never applied to
any deployment whose volume predated it, while `shared/db_manager.py:
save_bess_run` already wrote to those columns unconditionally, i.e. every
"it works" observation of that code path was against a fresh volume only.
This script is the missing piece: a manual, safe-to-re-run way to bring an
*existing* deployment's schema up to date with the latest `init-db/*.sql`
files, without recreating the volume (and therefore without losing data).

**Idempotency, verified file-by-file (not assumed):** every statement in
`init-db/*.sql` as of this writing is already idempotent -- `CREATE TABLE IF
NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `CREATE EXTENSION IF NOT EXISTS`,
`CREATE OR REPLACE VIEW`, `create_hypertable(..., if_not_exists => TRUE)`,
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and the one non-`IF NOT EXISTS`
statement (`01-init.sql`'s `ALTER TABLE ... SET (timescaledb.compress, ...)`)
re-setting the same compression reloptions is itself a no-op on a second
run, confirmed against a live TimescaleDB container by running the full
`init-db/*.sql` sequence twice in a row (first pass: all `CREATE`; second
pass: every statement returns its "already exists, skipping"/no-op NOTICE,
zero errors). So this script always re-applies every file, not just the
ones added since a given volume's creation -- it does not track "already
applied" state itself (no `schema_migrations` table), since idempotent
re-application makes that bookkeeping unnecessary and avoids a second source
of truth about what the live schema actually contains.

If a future `init-db/*.sql` file adds a genuinely non-idempotent statement,
running this script a second time against a volume that already has it
applied will fail loudly on that statement -- the correct, safe failure mode
(surface it, don't silently skip or guess), not something this script tries
to detect ahead of time.

**Not an auto-apply-at-startup mechanism.** Every service instead calls
`DatabaseManager.check_expected_columns()` once at startup and only *logs a
warning* if columns are missing -- deliberately never mutating schema
itself, since auto-applying `ALTER TABLE` from inside an application
process, unattended, against a database a whole fleet of services shares,
is exactly the kind of surprise a production deployment should not have to
reason about. Running this script is a required, explicit deploy step (see
DEPLOYMENT.md) -- something a human (or a deploy pipeline step) does on
purpose, once, right after `docker-compose up`/pulling a new image, not
something that happens implicitly as a side effect of a service starting.

Structured as a thin argparse-CLI wrapper around an importable function
(`run_migrations`), same shape as `scripts/backfill_history.py`.

Usage (needs `DATABASE_URL` pointed at a reachable Postgres/TimescaleDB
instance -- e.g. against docker-compose's mapped port for a local run):

    DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \\
        poetry run python scripts/migrate.py

Safe to run on every deploy, including a completely fresh volume (where
Postgres's own `docker-entrypoint-initdb.d` mechanism has already applied
every file) -- a second application of an already-idempotent file is a
no-op, so "always run this after `docker-compose up`" is a simpler
operational rule than "only run it if the volume predates the newest file".
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2

# This repo has no __init__.py / package-mode (see pyproject.toml's
# package-mode = false), so running this script directly (not via `python
# -m`) needs the repo root on sys.path for `shared.logging_config` to
# import -- same reason scripts/backfill_history.py does this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.logging_config import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

INIT_DB_DIR = Path(__file__).resolve().parent.parent / "init-db"


def discover_migrations(init_db_dir: Path = INIT_DB_DIR) -> list[Path]:
    """
    Returns every `*.sql` file in `init_db_dir`, sorted by filename -- the
    same ordering Postgres's own `docker-entrypoint-initdb.d` mechanism uses
    for a fresh volume (numeric filename prefixes, e.g. `01-`, `02-`, ...),
    so a manual run here applies files in the identical order a fresh
    volume would have seen them in.
    """
    return sorted(init_db_dir.glob("*.sql"))


def run_migrations(database_url: str, init_db_dir: Path = INIT_DB_DIR) -> list[str]:
    """
    Applies every `init_db_dir/*.sql` file, in filename order, against
    `database_url`. Each file's full contents are executed as one
    `cursor.execute()` call (psycopg2/libpq happily run multiple
    `;`-separated statements from a single non-parameterized `execute` call)
    and committed as its own transaction, so one file's failure doesn't roll
    back files already applied earlier in the same run.

    Returns the list of filenames applied, in order, on success. Raises on
    the first file that fails to apply -- a migration failure should stop
    the run and surface loudly, never be silently skipped (see module
    docstring's note on what a non-idempotent future statement would do).
    """
    migration_files = discover_migrations(init_db_dir)
    if not migration_files:
        logger.warning("No *.sql files found in %s", init_db_dir)
        return []

    applied: list[str] = []
    conn = psycopg2.connect(database_url)
    try:
        for path in migration_files:
            sql = path.read_text()
            logger.info("Applying %s...", path.name)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception(
                    "Migration failed on %s -- stopping (file(s) applied before this one: %s)",
                    path.name,
                    applied,
                )
                raise
            applied.append(path.name)
            logger.info("Applied %s", path.name)
    finally:
        conn.close()

    return applied


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--init-db-dir",
        type=Path,
        default=INIT_DB_DIR,
        help=(
            f"Directory of *.sql migration files to apply, in filename order "
            f"(default: {INIT_DB_DIR})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logger.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    applied = run_migrations(database_url, args.init_db_dir)
    logger.info("Migration run complete: %d file(s) applied: %s", len(applied), applied)


if __name__ == "__main__":
    main()
