#!/usr/bin/env python
"""
Registry validation CLI: confirms every `shared/datasets.py` entry's
configured field names (`time_field`, `zone_field`, every series'
`value_field`/`filter_field`/`extra_filters` keys) actually exist in that
dataset's live, published Energi Data Service schema.

**Run this before merging any change to `shared/datasets.py`** -- a typo'd
`value_field` silently never ingests a row (see `shared/dataset_validation.py`'s
module docstring for the full "typo vs legitimately-null" problem this
closes) and nothing in the normal ingestion path is loud enough to catch it
in review.

Makes real HTTP calls to `api.energidataservice.dk` (one `meta/dataset/{id}`
call per dataset, falling back to a live record sample only if that call
fails -- see `shared/dataset_validation.py:missing_fields`), so this is
**not part of the offline test suite** (`tests/test_dataset_validation.py`'s
equivalent whole-registry sweep is marked `@pytest.mark.live`, excluded from
the default `poetry run pytest` run -- see `pyproject.toml`'s `addopts`).
Run it manually, or in CI on a schedule / pre-merge gate, not on every
commit.

Usage:

    poetry run python scripts/validate_datasets.py

    # Only a subset, e.g. while iterating on a new registry entry:
    poetry run python scripts/validate_datasets.py --datasets ffr_dk2,inertia_nordic

Exit code is nonzero if any dataset has a missing field -- suitable as a
CI gate.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# This repo has no __init__.py / package-mode (see pyproject.toml's
# package-mode = false), so running this script directly (not via `python
# -m`) needs the repo root on sys.path -- same reason
# scripts/backfill_history.py and scripts/migrate.py do this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.base_ingestor import BaseIngestor  # noqa: E402
from shared.dataset_validation import missing_fields  # noqa: E402
from shared.datasets import DATASETS  # noqa: E402
from shared.logging_config import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

ENERGINET_BASE_URL = "https://api.energidataservice.dk"

# Same "rate limit ~1 request/second" observation as every other module that
# talks to api.energidataservice.dk (shared/backfill.py, services/ingestor/
# main.py) -- one meta/dataset (or fallback sample) call per dataset here.
RATE_LIMIT_SECONDS = 1.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--datasets",
        type=str,
        default=None,
        help="Comma-separated subset of shared/datasets.py names to validate "
        "(default: every registered dataset).",
    )
    return parser.parse_args(argv)


async def _run(dataset_names: list[str] | None) -> dict[str, dict[str, list[str]]]:
    datasets = DATASETS
    if dataset_names is not None:
        known = {d.name for d in DATASETS}
        unknown = set(dataset_names) - known
        if unknown:
            raise ValueError(f"unknown dataset name(s): {sorted(unknown)}")
        datasets = [d for d in DATASETS if d.name in dataset_names]

    ingestor = BaseIngestor(ENERGINET_BASE_URL)
    failures: dict[str, dict[str, list[str]]] = {}
    try:
        for i, dataset in enumerate(datasets):
            if i > 0:
                await asyncio.sleep(RATE_LIMIT_SECONDS)
            logger.info("Validating %s (%s)...", dataset.name, dataset.dataset_id)
            missing = await missing_fields(ingestor, dataset)
            if missing:
                failures[dataset.name] = missing
                logger.error("%s has missing field(s): %s", dataset.name, missing)
            else:
                logger.info("%s: OK", dataset.name)
    finally:
        await ingestor.close()

    return failures


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    dataset_names = [n.strip() for n in args.datasets.split(",")] if args.datasets else None

    failures = asyncio.run(_run(dataset_names))

    if failures:
        logger.error(
            "Registry validation FAILED: %d dataset(s) have field(s) absent from their "
            "published schema: %s",
            len(failures),
            sorted(failures),
        )
        sys.exit(1)

    logger.info(
        "Registry validation passed: every configured field exists in its dataset's "
        "published schema."
    )


if __name__ == "__main__":
    main()
