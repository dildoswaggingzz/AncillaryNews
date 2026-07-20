"""
One-time/occasional historical backfill for the datasets
`shared/bess_simulator.py` reads (see that module's docstring for exactly
which markets it uses and why -- mFRR capacity/EAM are excluded by the BESS
market-participation constraint).

**Why this exists:** `services/ingestor/main.py`'s scheduled cycle only ever
fetches the most recent N records per dataset (`dataset.params`'s
`limit`/`sort` -- see `shared/datasets.py`), so `market_data_history` only
has real depth going back to whenever the ingestor was first brought up. A
BESS backtest (`shared/bess_simulator.py:run_backtest`) needs weeks of price
history to build a meaningful rolling baseline and see varied market
conditions -- this module fills that gap by paging through
`api.energidataservice.dk`'s `start`/`end` date-range query params (confirmed
live against the real API while building this; not documented in
`docs/dataset-catalogue.md`/`docs/dataset-catalogue-addendum.md`, which only
describe the `limit`-based "most recent N" pattern the M0 audit exercised)
instead of the live poller's "give me the most recent N records" pattern.

**Idempotency / safe-to-re-run:** every chunk's records are saved via the
exact same `DatabaseManager.save_market_data` the live ingestor uses --
an INSERT tagged with a fresh `fetched_at`, `ON CONFLICT (time, market, zone,
product, fetched_at) DO NOTHING` (see `shared/db_manager.py`). Re-running a
backfill (or running it over a window the live ingestor has already partly
covered) does NOT dedupe against the live poller's earlier fetches of the
same `time`/`market`/`zone`/`product` -- it adds new rows with their own
`fetched_at`, exactly like any other independent fetch of already-known data.
This is consistent with (not a bug in) `market_data_history`'s deliberately
append-only, revision-preserving design: a backfill run and a live poll are
just two different `fetched_at` observations of the same underlying market
time value. Nothing here mutates or dedupes existing rows.

**Not a scheduled service:** unlike `services/ingestor/main.py`'s always-on
poller, this is meant to be run occasionally/manually -- either via
`scripts/backfill_history.py` or the on-demand `POST /ingestor/backfill`
API route (`services/api/main.py`) -- never via an APScheduler job.

**Rate limiting:** `docs/dataset-catalogue.md` documents ~1 request/second
observed during the original M0 bulk discovery; live testing while building
this module found the real limit noticeably stricter in short bursts
(back-to-back requests with no pacing returned HTTP 429 almost immediately).
`shared/base_ingestor.py:BaseIngestor.fetch_data` already retries
`httpx.HTTPStatusError` (which a 429 raises via `raise_for_status()`, and
which is itself an `httpx.HTTPError` subclass) with exponential backoff (5
attempts, 2-10s), so an occasional 429 here self-heals rather than aborting
the whole run -- but `RATE_LIMIT_SECONDS` below still paces every request at
~1/sec up front to keep 429s rare rather than relying on the retry alone.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from shared.base_ingestor import BaseIngestor
from shared.datasets import DATASETS, DatasetConfig
from shared.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

ENERGINET_BASE_URL = "https://api.energidataservice.dk"

# See module docstring's "Rate limiting" section. Bumped up from the
# ingestor's live-polling 1.0s (services/ingestor/main.py) after a live
# backfill run during this module's own development hit repeated 429s at
# 1.0s spacing (BaseIngestor.fetch_data's retry/backoff recovered most of
# them, but not all within its 5-attempt budget) -- 3.0s empirically kept
# 429s rare across a real 30-day/3-dataset backfill run.
RATE_LIMIT_SECONDS = 3.0

# The shared/datasets.py entries shared/bess_simulator.py reads today, plus
# the ones a near-term BESS-stacking change is expected to read (see its
# module docstring: FCR via fcr_dk1/fcr_dk2, aFRR capacity via
# afrr_reserves_nordic, aFRR energy activation via afrr_energy_activation
# (ingested/eligible but not yet its own revenue stream -- see that
# docstring), day-ahead via day_ahead_prices, imbalance via imbalance_price).
# `ffr_dk2`/`ffr_demand_dk2` (FFR capacity, a genuinely BESS-addressable
# market not yet wired into the simulator's `capacity_markets`) and
# `inertia_nordic` (causal context for FCR-D/FFR demand, read by
# `shared/price_recap_synthesizer.py`) are included here ahead of that
# wiring landing, so historical depth is already backfillable the day it
# does -- a backtest is useless without weeks of prior history, and there's
# no reason to make that a blocking dependency of the wiring change itself.
# mfrr_capacity/mfrr_eam/mfrr_capacity_extra are never read by the BESS
# simulator (battery market-participation constraint --
# `shared/bess_simulator.py:EXCLUDED_MARKETS`) and
# power_system_right_now/afrr_picasso_corrections are ingested for other
# purposes (system-state context, revision-signal investigation) but not
# read by it either. Kept as an explicit name list -- not "every
# non-excluded dataset" -- so this stays a deliberate, reviewable subset of
# the registry rather than silently backfilling whatever it happens to
# contain as shared/datasets.py grows.
BESS_DATASET_NAMES = frozenset(
    {
        "fcr_dk1",
        "fcr_dk2",
        "afrr_reserves_nordic",
        "afrr_energy_activation",
        "day_ahead_prices",
        "imbalance_price",
        "ffr_dk2",
        "ffr_demand_dk2",
        "inertia_nordic",
    }
)

# A real backtest needs "weeks" of history (README brief for this task); 30
# days is the conservative default window for both the CLI script and the
# API route when no explicit start/end is given. Energinet's own retention
# varies a lot per dataset -- some of the datasets above only go back ~3
# months in practice (discovered empirically, not documented anywhere) -- so
# 30 days comfortably fits every BESS-relevant dataset without the caller
# needing to know each one's real depth up front. Pass an explicit --start
# (script) / start_time (API) for a wider window.
DEFAULT_BACKFILL_DAYS = 30

# Energinet's date-range query is chunked into this many days per request
# rather than one single [start, end] call, both to bound each individual
# response's size and to give the rate limiter/retry logic more, smaller
# checkpoints to recover at if one chunk's request fails.
DEFAULT_CHUNK_DAYS = 7

# Generous per-chunk record cap. A DEFAULT_CHUNK_DAYS-wide window of even the
# finest-grained dataset here (afrr_energy_activation, sub-second TimeMsUTC
# resolution) can be large, but Energinet's API itself caps out well below
# this; kept high rather than tuned per-dataset so one constant covers every
# BESS dataset without a second per-dataset "expected volume" table to
# maintain.
CHUNK_LIMIT = 20000


def bess_datasets() -> list[DatasetConfig]:
    """Returns the shared/datasets.py DatasetConfig entries relevant to BESS backtesting
    (BESS_DATASET_NAMES above -- see that constant's docstring for exactly what "relevant"
    covers), in registry order."""
    return [d for d in DATASETS if d.name in BESS_DATASET_NAMES]


def _date_chunks(start: datetime, end: datetime, chunk_days: int):
    """Yields (chunk_start, chunk_end) pairs covering [start, end) in chunk_days-wide slices,
    oldest first. The final chunk is clipped to `end` rather than overshooting it."""
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")
    step = timedelta(days=chunk_days)
    cur = start
    while cur < end:
        chunk_end = min(cur + step, end)
        yield cur, chunk_end
        cur = chunk_end


def _historical_params(dataset: DatasetConfig, start: datetime, end: datetime, limit: int) -> dict:
    """
    Builds Energi Data Service query params for one historical date-range
    chunk of `dataset`: its own declared `sort` (carried over from
    `dataset.params` -- ordering doesn't affect correctness here, every
    record in the chunk gets saved regardless), plus `start`/`end` (the
    date-range params this module adds -- confirmed live against
    `api.energidataservice.dk`, not just `limit`-based like the live
    ingestor's `dataset.params`) and a much higher `limit` than the live
    poller's "most recent N" default.
    """
    params = dict(dataset.params)
    params["start"] = start.strftime("%Y-%m-%dT%H:%M")
    params["end"] = end.strftime("%Y-%m-%dT%H:%M")
    params["limit"] = limit
    return params


async def backfill_dataset(
    ingestor: BaseIngestor,
    db: DatabaseManager,
    dataset: DatasetConfig,
    start: datetime,
    end: datetime,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    rate_limit_seconds: float = RATE_LIMIT_SECONDS,
) -> dict:
    """
    Pages through [start, end) for one dataset in chunk_days-wide date-range
    windows, saving every chunk's records via `DatabaseManager.save_market_data`
    (see module docstring's "Idempotency / safe-to-re-run" section). A
    failed chunk (fetch or save) is logged and skipped, not fatal to the
    rest of the dataset's backfill -- mirrors
    services/ingestor/main.py:run_ingestion_cycle's "one dataset's failure
    doesn't take down the rest" pattern, applied at chunk granularity here.
    Returns a summary dict for this one dataset.
    """
    records_fetched = 0
    rows_saved = 0
    chunks_fetched = 0
    chunks_failed = 0
    earliest_record_time = None
    latest_record_time = None

    chunk_list = list(_date_chunks(start, end, chunk_days))
    for i, (chunk_start, chunk_end) in enumerate(chunk_list):
        if i > 0:
            await asyncio.sleep(rate_limit_seconds)

        params = _historical_params(dataset, chunk_start, chunk_end, CHUNK_LIMIT)
        try:
            data = await ingestor.fetch_data(f"dataset/{dataset.dataset_id}", params=params)
        except Exception:
            logger.exception(
                "Backfill fetch failed for %s [%s, %s)", dataset.name, chunk_start, chunk_end
            )
            chunks_failed += 1
            continue

        records = data.get("records") if data else None
        chunks_fetched += 1
        if not records:
            continue
        records_fetched += len(records)

        try:
            save_result = db.save_market_data(records, dataset)
            rows_saved += save_result.total
        except Exception:
            logger.exception(
                "Backfill save failed for %s [%s, %s)", dataset.name, chunk_start, chunk_end
            )
            chunks_failed += 1
            continue

        for record in records:
            t = record.get(dataset.time_field)
            if t is None:
                continue
            if earliest_record_time is None or t < earliest_record_time:
                earliest_record_time = t
            if latest_record_time is None or t > latest_record_time:
                latest_record_time = t

    return {
        "dataset": dataset.name,
        "dataset_id": dataset.dataset_id,
        "chunks_fetched": chunks_fetched,
        "chunks_failed": chunks_failed,
        "records_fetched": records_fetched,
        "rows_saved": rows_saved,
        "earliest_record_time": earliest_record_time,
        "latest_record_time": latest_record_time,
    }


async def run_backfill(
    start: datetime,
    end: datetime | None = None,
    dataset_names: list[str] | None = None,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    rate_limit_seconds: float = RATE_LIMIT_SECONDS,
    db: DatabaseManager | None = None,
) -> dict:
    """
    Backfills every BESS-relevant dataset (`bess_datasets()`), or the
    `dataset_names` subset of it if given, over `[start, end)` (`end`
    defaults to now). Builds its own `BaseIngestor` always; builds its own
    `DatabaseManager` only if one isn't passed in -- `services/api/main.py`'s
    `POST /ingestor/backfill` route passes its own already-pooled
    `DatabaseManager` (`get_db`) rather than opening a second connection
    pool, while `scripts/backfill_history.py` (a short-lived standalone
    process) lets this open and close its own.
    """
    if end is None:
        end = datetime.now(UTC)
    if start >= end:
        raise ValueError("start must be before end")

    datasets = bess_datasets()
    if dataset_names is not None:
        known_names = {d.name for d in datasets}
        unknown = set(dataset_names) - known_names
        if unknown:
            raise ValueError(
                f"unknown/non-BESS dataset name(s): {sorted(unknown)}; "
                f"must be a subset of {sorted(known_names)}"
            )
        datasets = [d for d in datasets if d.name in dataset_names]

    ingestor = BaseIngestor(ENERGINET_BASE_URL)
    owns_db = db is None
    if owns_db:
        db = DatabaseManager()

    results = []
    try:
        logger.info(
            "Starting historical backfill for %d dataset(s) over [%s, %s)...",
            len(datasets),
            start,
            end,
        )
        for i, dataset in enumerate(datasets):
            if i > 0:
                await asyncio.sleep(rate_limit_seconds)
            result = await backfill_dataset(
                ingestor,
                db,
                dataset,
                start,
                end,
                chunk_days=chunk_days,
                rate_limit_seconds=rate_limit_seconds,
            )
            results.append(result)
            logger.info(
                "Backfilled %s: %d row(s) saved from %d chunk(s) (%d failed), "
                "records span [%s, %s]",
                dataset.name,
                result["rows_saved"],
                result["chunks_fetched"],
                result["chunks_failed"],
                result["earliest_record_time"],
                result["latest_record_time"],
            )
    finally:
        await ingestor.close()
        if owns_db:
            db.close()

    return {
        "start": start,
        "end": end,
        "datasets": results,
        "total_rows_saved": sum(r["rows_saved"] for r in results),
    }
