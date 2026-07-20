"""
Stage 2 guardrail: catches a typo'd field name in `shared/datasets.py`
*before* it reaches production, closing the gap the M0-era registry left
open -- a typo'd `value_field` is indistinguishable, at ingestion time, from
a column that's legitimately null for every record in a sample
(`shared/db_manager.py:save_market_data` simply omits that product for that
record either way), yields one WARNING log line, and still reports
`status="success"` (`services/ingestor/main.py`). Nothing about that failure
mode is loud enough to catch in review or alert on in production -- this
module (and `scripts/validate_datasets.py`, which wires it into a
pre-merge check) is the fix.

**The distinction this module exists to draw:** a field can be
"configured but never populated in a live sample" (fine -- e.g.
`MfrrCapacityMarketExtra`'s price columns, which are null in every record
today because extra auctions are rare -- see `shared/datasets.py`'s
`mfrr_capacity_extra` entry) or "configured but doesn't exist in the
dataset's schema at all" (a bug -- a copy-paste/typo'd column name that will
*never* ingest a row, no matter how long you wait). Sampling live records
and checking which keys are present cannot tell these apart -- a field
that's merely null in a 5-row sample looks identical to one that's absent
entirely. `meta/dataset/{dataset_id}` is Energinet's own authoritative
column list for the dataset (independent of what any particular row
happens to contain), so it's the primary source; a live record sample is
only a fallback for a dataset whose `meta/dataset` response is temporarily
unavailable or unusably shaped.
"""

from __future__ import annotations

import logging

from shared.base_ingestor import BaseIngestor
from shared.datasets import DatasetConfig

logger = logging.getLogger(__name__)

# Small -- this fallback only needs enough rows to union together a
# reasonably representative set of keys actually present on live records; it
# is not a data-quality sample (shared/dataset_validation.py never tries to
# distinguish "null" from "always null" from a sample, only "present" from
# "absent" -- see module docstring for why the primary meta/dataset path
# avoids needing more than this).
_FALLBACK_SAMPLE_LIMIT = 5


async def _schema_field_names(ingestor: BaseIngestor, dataset: DatasetConfig) -> set[str] | None:
    """
    Returns every column name Energinet's `meta/dataset/{id}` endpoint
    declares for `dataset`, or `None` if that call fails or returns a shape
    with no usable `columns` list (the fallback-to-sample trigger -- see
    `missing_fields`).
    """
    try:
        meta = await ingestor.fetch_data(f"meta/dataset/{dataset.dataset_id}")
    except Exception:
        logger.warning(
            "meta/dataset/%s fetch failed; falling back to a live record sample for schema "
            "validation of %s",
            dataset.dataset_id,
            dataset.name,
        )
        return None

    columns = meta.get("columns") if meta else None
    if not columns:
        logger.warning(
            "meta/dataset/%s returned no usable 'columns' list; falling back to a live record "
            "sample for schema validation of %s",
            dataset.dataset_id,
            dataset.name,
        )
        return None

    return {c["dbColumn"] for c in columns if "dbColumn" in c}


async def _sample_field_names(ingestor: BaseIngestor, dataset: DatasetConfig) -> set[str]:
    """
    Fallback source: the union of keys across a small live record sample.
    Cannot distinguish "absent from the schema" from "present but null
    across this particular sample" -- strictly worse than `meta/dataset`,
    only used when that primary source is unavailable (see module
    docstring).
    """
    data = await ingestor.fetch_data(
        f"dataset/{dataset.dataset_id}", params={"limit": _FALLBACK_SAMPLE_LIMIT}
    )
    records = data.get("records") if data else None
    fields: set[str] = set()
    for record in records or []:
        fields.update(record.keys())
    return fields


def _configured_field_names_by_role(dataset: DatasetConfig) -> dict[str, list[str]]:
    """
    Every field name `dataset` is configured to read, grouped by *role*
    (`time_field`, `zone_field`, `value_field`, `filter_field`) rather than
    flattened into one list -- so a validation failure reads as "the
    `value_field` FFR_PriceDKK is missing" rather than an undifferentiated
    field name, which matters when tracking down which `SeriesConfig` (or
    the dataset-level config itself) needs fixing.
    """
    by_role: dict[str, list[str]] = {"time_field": [dataset.time_field]}
    if dataset.zone_field is not None:
        by_role["zone_field"] = [dataset.zone_field]

    value_fields = [s.value_field for s in dataset.series]
    if value_fields:
        by_role["value_field"] = value_fields

    filter_fields = sorted(
        {s.filter_field for s in dataset.series if s.filter_field is not None}
        | {k for s in dataset.series for k in s.extra_filters}
    )
    if filter_fields:
        by_role["filter_field"] = filter_fields

    return by_role


async def missing_fields(ingestor: BaseIngestor, dataset: DatasetConfig) -> dict[str, list[str]]:
    """Configured field names absent from the dataset's published schema.

    Primary source is `meta/dataset/{dataset_id}` — the authoritative column list, which does
    not depend on a sample happening to be non-null. Critical for MfrrCapacityMarketExtra, whose
    price columns are entirely null today. Falls back to the union of keys across a sample.

    Returns `{}` if every configured field name is present in the schema.
    Otherwise, keyed by role (`time_field`/`zone_field`/`value_field`/
    `filter_field`) with the list of that role's missing field names --
    only roles with at least one miss are included.
    """
    schema_fields = await _schema_field_names(ingestor, dataset)
    if schema_fields is None:
        schema_fields = await _sample_field_names(ingestor, dataset)

    result: dict[str, list[str]] = {}
    for role, configured in _configured_field_names_by_role(dataset).items():
        missing = sorted({name for name in configured if name not in schema_fields})
        if missing:
            result[role] = missing
    return result
