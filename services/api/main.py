"""
Phase 5 read API + dashboard (follow-on to README §9 M4, per
init-db/02-event-reports.sql's "Phase 5 API/dashboard" comment): a FastAPI
service serving the Event Reports, market data, and rule-engine triggers
built up by M0-M4 as both JSON endpoints and simple server-rendered HTML
pages (README §7: "dashboard later" -- this is that later).

Read-only: this service never writes to `market_data_history`,
`event_reports`, or `triggers` -- those tables remain owned by the
ingestor/orchestrator (see shared/db_manager.py, shared/rule_engine.py).

No authentication (matches the rest of the stack's current internal-tool
posture) -- see README-adjacent Phase 6 (production readiness) note in the
project's follow-up list; auth is explicitly out of scope here.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from shared.db_manager import DatabaseManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# A single pooled DatabaseManager, built lazily on first use and reused for
# the lifetime of the process (unlike the orchestrator's per-cycle instance
# -- this process is long-lived, so there's no reason to reconnect per
# request). Tests override `get_db` via `app.dependency_overrides` instead of
# touching this module-level state directly.
_db: DatabaseManager | None = None


def get_db() -> DatabaseManager:
    global _db
    if _db is None:
        _db = DatabaseManager()
    return _db


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    global _db
    if _db is not None:
        _db.close()
        _db = None


app = FastAPI(
    title="AncillaryNews API",
    description="Read surface over Event Reports, market data, and rule-engine triggers.",
    lifespan=lifespan,
)


# --- JSON API ------------------------------------------------------------


@app.get("/health")
def health():
    """Trivial liveness check -- deliberately does not touch the database,
    so it stays green even if `db` is briefly unreachable."""
    return {"status": "ok"}


@app.get("/series")
def list_series(db: DatabaseManager = Depends(get_db)):
    """Distinct (market, zone, product) series available for charting."""
    rows = db.fetch_distinct_series()
    return {"series": [{"market": m, "zone": z, "product": p} for m, z, p in rows]}


@app.get("/series/{market}/{zone}/{product}")
def series_data(
    market: str,
    zone: str,
    product: str,
    limit: int = Query(500, ge=1, le=5000),
    history: bool = False,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    db: DatabaseManager = Depends(get_db),
):
    """
    Time-series data for one series, for charting. Reads the latest value
    per `time` from the `market_data` view by default; pass `?history=true`
    to see every revision from `market_data_history` instead.
    """
    rows = db.fetch_series_values(
        market,
        zone,
        product,
        limit=limit,
        time_from=time_from,
        time_to=time_to,
        history=history,
    )
    return {
        "market": market,
        "zone": zone,
        "product": product,
        "history": history,
        "count": len(rows),
        "data": rows,
    }


@app.get("/event-reports")
def list_event_reports(
    market: str | None = None,
    zone: str | None = None,
    product: str | None = None,
    time_from: datetime | None = None,
    time_to: datetime | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DatabaseManager = Depends(get_db),
):
    """Published Event Reports, most-recent-first, filterable and paginated."""
    rows = db.fetch_event_reports(
        market=market,
        zone=zone,
        product=product,
        time_from=time_from,
        time_to=time_to,
        limit=limit,
        offset=offset,
    )
    return {"count": len(rows), "limit": limit, "offset": offset, "reports": rows}


@app.get("/event-reports/{event_id}")
def get_event_report(event_id: str, db: DatabaseManager = Depends(get_db)):
    """Single Event Report by ID; 404 if unknown."""
    row = db.fetch_event_report(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Event report {event_id!r} not found")
    return row


@app.get("/triggers")
def list_triggers(
    market: str | None = None,
    zone: str | None = None,
    product: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: DatabaseManager = Depends(get_db),
):
    """Persisted rule-engine triggers (shared/rule_engine.py), most-recent-first."""
    rows = db.fetch_triggers(market=market, zone=zone, product=product, limit=limit, offset=offset)
    return {"count": len(rows), "limit": limit, "offset": offset, "triggers": rows}


# --- dashboard (server-rendered Jinja2 HTML, no JS build toolchain) ------


@app.get("/", response_class=HTMLResponse)
def dashboard_home(request: Request, db: DatabaseManager = Depends(get_db)):
    """Recent Event Reports list -- the dashboard's landing page."""
    reports = db.fetch_event_reports(limit=25, offset=0)
    return templates.TemplateResponse(request, "index.html", {"reports": reports})


@app.get("/dashboard/event-reports/{event_id}", response_class=HTMLResponse)
def dashboard_event_report(request: Request, event_id: str, db: DatabaseManager = Depends(get_db)):
    """Full README §2 shape for one Event Report, with a link to the report
    it corrects if this one is a correction."""
    row = db.fetch_event_report(event_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Event report {event_id!r} not found")

    corrects = None
    if row.get("corrects_event_id"):
        corrects = db.fetch_event_report(row["corrects_event_id"])

    return templates.TemplateResponse(
        request, "event_report.html", {"row": row, "corrects": corrects}
    )


@app.get("/dashboard/series", response_class=HTMLResponse)
def dashboard_series_picker(request: Request, db: DatabaseManager = Depends(get_db)):
    """Lists every available series, linking to each one's detail page."""
    series = db.fetch_distinct_series()
    return templates.TemplateResponse(request, "series_list.html", {"series": series})


@app.get("/dashboard/series/{market}/{zone}/{product}", response_class=HTMLResponse)
def dashboard_series_detail(
    request: Request,
    market: str,
    zone: str,
    product: str,
    limit: int = 200,
    db: DatabaseManager = Depends(get_db),
):
    """Recent values for one series as a table, plus a Chart.js line chart
    (CDN-loaded, no local JS build step) when there's data to plot."""
    rows = db.fetch_series_values(market, zone, product, limit=limit)
    return templates.TemplateResponse(
        request,
        "series_detail.html",
        {"market": market, "zone": zone, "product": product, "rows": rows},
    )
