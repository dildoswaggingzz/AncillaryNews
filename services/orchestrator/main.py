"""
M4 Intelligence Orchestrator (README §3C / §9): owns scheduled trigger
evaluation -- relocated here from `services/ingestor/main.py` per the M4
brief, so trigger evaluation isn't coupled to the ingestion poll cadence and
isn't evaluated twice. `shared/rule_engine.run_rule_engine` still posts every
raw trigger straight to Slack exactly as it did under M2 -- that's still
valid, useful signal independent of whether synthesis below succeeds; the
synthesis pipeline here is an enrichment on top of it, not a replacement.

For each fired Trigger, runs the README §3C RAG + LLM synthesis pipeline:
pull the hard-data context window, retrieve semantically related claims from
Qdrant, synthesize + citation-validate an Event Report with Claude Opus, and
on success persist it (init-db/02-event-reports.sql) and post it to Slack.
Revision-alert triggers against an already-published report become
correction events (README §5) instead of new independent reports.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Histogram
from qdrant_client import AsyncQdrantClient

from shared.db_manager import DatabaseManager
from shared.event_synthesizer import infer_direction, synthesize_event_report
from shared.logging_config import configure_logging
from shared.metrics import start_metrics_server
from shared.rule_engine import Trigger, run_rule_engine
from shared.slack_notifier import send_event_report_alert
from shared.vector_store import QdrantStore

configure_logging()
logger = logging.getLogger(__name__)

# Runs at the same cadence as the ingestor's own poll cycle (README §8's
# 15-minute early-warning latency target) -- fast enough that a trigger is
# picked up on the very next tick after the data that caused it lands.
TRIGGER_EVALUATION_INTERVAL_MINUTES = 15

QDRANT_URL = "http://vector-db:6333"

# Port for this service's standalone Prometheus exposition endpoint.
# Trigger-fired-by-type counters live in shared/rule_engine.py (where
# triggers are canonically fired); LLM call/latency and citation-rejection
# counters live in shared/event_synthesizer.py (where the Claude call and
# citation validation actually happen) -- both registered at import time
# below, alongside this service's own cycle-duration histogram.
METRICS_PORT = int(os.getenv("METRICS_PORT", "9102"))

SYNTHESIS_CYCLE_DURATION = Histogram(
    "orchestrator_cycle_duration_seconds", "Duration of one full trigger-evaluation/synthesis cycle"
)

# README §3C step 2: "hard-data context window ... ±N hours".
CONTEXT_WINDOW_HOURS_BEFORE = 6.0
CONTEXT_WINDOW_HOURS_AFTER = 6.0

# README §3C step 3 RAG retrieval tuning. `retrieved_at` (when *we* crawled a
# claim) is only a proxy for when the claim is actually relevant to a given
# trigger time, so the search window is generous, not exact.
RAG_SEARCH_LIMIT = 5
RAG_TIME_WINDOW_HOURS = 48.0


def _rag_query(trigger: Trigger, direction: str) -> str:
    """Builds the semantic search query for RAG retrieval (README §3C step 3:
    "semantic + time-filtered search ... relevant to the market/zone/direction")."""
    return (
        f"{trigger.market} {trigger.zone} {trigger.product} price {direction} -- "
        f"Danish/Nordic balancing market explanation"
    )


def _parse_trigger_time(trigger: Trigger) -> datetime | None:
    """
    `Trigger.time` is `str(...)` of whatever psycopg2 returned for a
    TIMESTAMPTZ column (see shared/rule_engine.py) -- a datetime's default
    str() form (e.g. "2026-07-16 10:00:00+00:00"), which `datetime.fromisoformat`
    parses fine. Returns None (logged) rather than raising if it doesn't.
    """
    try:
        return datetime.fromisoformat(trigger.time)
    except ValueError:
        logger.error("Could not parse trigger time %r; skipping synthesis", trigger.time)
        return None


async def process_trigger(trigger: Trigger, db: DatabaseManager, store: QdrantStore) -> bool:
    """
    Runs the full context-fetch + RAG + LLM-synthesis + citation-validation
    + persist + Slack pipeline for one fired Trigger.

    Any failure here is logged and swallowed -- the raw trigger has already
    reached Slack via `run_rule_engine` regardless of what happens in this
    pipeline, and one trigger's synthesis failing must never stop evaluation
    of the rest of the cycle's triggers.

    Returns `True` only when an Event Report was actually persisted this
    call (used by `run_synthesis_cycle` to count `reports_published` for the
    on-demand run-now summary); every early-return path below returns
    `False`.
    """
    center_time = _parse_trigger_time(trigger)
    if center_time is None:
        return False

    context_window = db.fetch_context_window(
        trigger.market,
        trigger.zone,
        trigger.product,
        center_time,
        hours_before=CONTEXT_WINDOW_HOURS_BEFORE,
        hours_after=CONTEXT_WINDOW_HOURS_AFTER,
    )

    direction = infer_direction(trigger)
    retrieved_claims = await store.search_claims(
        _rag_query(trigger, direction),
        time_from=center_time - timedelta(hours=RAG_TIME_WINDOW_HOURS),
        time_to=center_time + timedelta(hours=RAG_TIME_WINDOW_HOURS),
        limit=RAG_SEARCH_LIMIT,
    )

    report = await synthesize_event_report(trigger, context_window, retrieved_claims)
    if report is None:
        # No ANTHROPIC_API_KEY, the model call failed, or the report was
        # rejected by citation validation -- already logged by
        # shared/event_synthesizer.py. The raw trigger already reached
        # Slack, so nothing is silently lost.
        return False

    is_correction = False
    corrects_event_id = None
    if trigger.trigger_type == "revision_alert":
        existing = db.find_published_report(
            trigger.market, trigger.zone, trigger.product, center_time
        )
        if existing is not None:
            is_correction = True
            corrects_event_id = existing["event_id"]
            report["event_id"] = f"{report['event_id']}-correction-{trigger.detected_at}"
            report["observation"] = (
                f"CORRECTION to previously published report {corrects_event_id}: "
                f"{report['observation']}"
            )
            report["synthesis"] = (
                f"This is a correction to Event Report {corrects_event_id}, triggered by a "
                f"data revision: the figure for {trigger.market}/{trigger.zone}/{trigger.product} "
                f"at {trigger.time} changed from {trigger.baseline} to {trigger.value} "
                f"({trigger.details}). {report['synthesis']}"
            )

    try:
        db.save_event_report(
            event_id=report["event_id"],
            market=trigger.market,
            zone=trigger.zone,
            product=trigger.product,
            time=center_time,
            report=report,
            is_correction=is_correction,
            corrects_event_id=corrects_event_id,
        )
    except Exception:
        logger.exception("Failed to persist Event Report %s", report["event_id"])
        return False

    logger.info("Published Event Report %s (is_correction=%s)", report["event_id"], is_correction)

    try:
        await send_event_report_alert(
            {**report, "is_correction": is_correction, "corrects_event_id": corrects_event_id}
        )
    except Exception:
        logger.exception("Failed to send Slack alert for Event Report %s", report["event_id"])

    return True


async def run_synthesis_cycle() -> dict:
    """
    Evaluates every rule-engine trigger class (relocated here from
    `services/ingestor/main.py` per the M4 brief -- see module docstring)
    and runs the synthesis pipeline for every trigger it fires.

    Returns a small `{"triggers_fired": int, "reports_published": int}`
    summary -- used by the on-demand `POST /orchestrator/run-now` route
    (`services/api/main.py`) and its dashboard button to report back what
    one cycle actually did; the automatic scheduler (`scheduled_synthesis_cycle`
    below) ignores the return value, same as before this was added.
    """
    db = DatabaseManager()
    qdrant_client = AsyncQdrantClient(url=QDRANT_URL)
    store = QdrantStore(qdrant_client)
    cycle_start = time.monotonic()
    triggers: list[Trigger] = []
    reports_published = 0

    try:
        await store.ensure_collection()

        try:
            triggers = await run_rule_engine(db)
            logger.info("Rule engine evaluated cycle: %d trigger(s) fired", len(triggers))
        except Exception:
            logger.exception("Rule engine evaluation failed")
            return {"triggers_fired": 0, "reports_published": 0}

        for trigger in triggers:
            try:
                # `process_trigger` returns `True` only on a successfully
                # persisted Event Report; an `is True` check (rather than
                # truthiness) so a mocked `process_trigger` in tests --
                # which defaults to returning a truthy `MagicMock` -- never
                # gets miscounted as a publish.
                if await process_trigger(trigger, db, store) is True:
                    reports_published += 1
            except Exception:
                logger.exception(
                    "Synthesis pipeline failed for trigger_type=%s market=%s zone=%s product=%s",
                    trigger.trigger_type,
                    trigger.market,
                    trigger.zone,
                    trigger.product,
                )
    finally:
        await qdrant_client.close()
        db.close()
        SYNTHESIS_CYCLE_DURATION.observe(time.monotonic() - cycle_start)

    return {"triggers_fired": len(triggers), "reports_published": reports_published}


def _auto_run_enabled() -> bool:
    """
    Reads `AUTO_RUN_ENABLED` fresh on every call (same "read the env var live,
    don't freeze it at import time" convention as
    `services/api/main.py`'s `require_api_key`). Defaults to `False`:
    automatic scheduled synthesis cycles are opt-in, not opt-out, since every
    fired rule-engine trigger this cycle processes costs one Claude Opus call
    (`shared/event_synthesizer.py`) -- see `.env.example` / `DEPLOYMENT.md`.
    Set `AUTO_RUN_ENABLED=true` to restore the fully-automatic behavior this
    service had before this gate existed.
    """
    return os.getenv("AUTO_RUN_ENABLED", "false").strip().lower() == "true"


async def scheduled_synthesis_cycle() -> None:
    """
    The APScheduler job entrypoint (see `main` below) -- wraps
    `run_synthesis_cycle` behind the `AUTO_RUN_ENABLED` gate so the scheduler
    itself, this service's process, and its `/metrics` exposition endpoint
    all stay up and healthy regardless, while no Claude Opus calls happen
    automatically unless explicitly opted in. Firing one real cycle on
    demand, independent of this gate, is always available via
    `POST /orchestrator/run-now` on the API service
    (`services/api/main.py`), which calls `run_synthesis_cycle` directly.
    """
    if not _auto_run_enabled():
        logger.info(
            "AUTO_RUN_ENABLED is unset/false; skipping this scheduled synthesis cycle "
            "(no Claude Opus calls made). POST /orchestrator/run-now on the API service "
            "to run one cycle on demand, or set AUTO_RUN_ENABLED=true for automatic "
            "scheduled cycles."
        )
        return
    await run_synthesis_cycle()


async def main():
    start_metrics_server(METRICS_PORT)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_synthesis_cycle,
        "interval",
        minutes=TRIGGER_EVALUATION_INTERVAL_MINUTES,
        next_run_time=datetime.now(),
    )
    scheduler.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
