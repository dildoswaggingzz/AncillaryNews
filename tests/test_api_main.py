import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

MAIN_PATH = Path(__file__).parent.parent / "services" / "api" / "main.py"

spec = importlib.util.spec_from_file_location("api_main", MAIN_PATH)
api_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_main)


SAMPLE_REPORT = {
    "event_id": "2026-07-16 17:15:00+00:00-DK1-mFRR EAM-up-up",
    "market": "mFRR EAM",
    "zone": "DK1",
    "direction": "up",
    "observation": "Balancing energy price hit 4,850 DKK/MWh vs 30-day P95 of 1,200",
    "hard_data_correlates": [
        {"signal": "imbalance", "value": "-820 MW", "source": "Energinet EDS"}
    ],
    "market_theories": [
        {
            "claim": "Analysts point to low wind + Karlshamn unavailability",
            "source": "EnergiWatch, 2026-07-14",
            "type": "theory",
        }
    ],
    "synthesis": "According to EnergiWatch, low wind output coincided with the price move.",
    "confidence": "medium",
    "data_maturity": "provisional — figures may be revised by Energinet",
}

SAMPLE_ROW = {
    "event_id": SAMPLE_REPORT["event_id"],
    "market": "mFRR EAM",
    "zone": "DK1",
    "product": "up",
    "time": datetime(2026, 7, 16, 17, 15, tzinfo=UTC),
    "published_at": datetime(2026, 7, 16, 17, 20, tzinfo=UTC),
    "report": SAMPLE_REPORT,
    "is_correction": False,
    "corrects_event_id": None,
}

CORRECTION_ROW = {
    "event_id": SAMPLE_REPORT["event_id"] + "-correction-abc",
    "market": "mFRR EAM",
    "zone": "DK1",
    "product": "up",
    "time": datetime(2026, 7, 16, 17, 15, tzinfo=UTC),
    "published_at": datetime(2026, 7, 16, 18, 0, tzinfo=UTC),
    "report": {**SAMPLE_REPORT, "observation": "CORRECTION to previously published report ..."},
    "is_correction": True,
    "corrects_event_id": SAMPLE_ROW["event_id"],
}


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def client(db):
    api_main.app.dependency_overrides[api_main.get_db] = lambda: db
    with TestClient(api_main.app) as test_client:
        yield test_client
    api_main.app.dependency_overrides.clear()


# --- health ------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- metrics (Phase 6 production readiness) ---------------------------------


def test_metrics_endpoint_returns_prometheus_text_format(client):
    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "api_requests_total" in resp.text


def test_metrics_endpoint_is_unauthenticated_even_with_api_key_set(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")

    resp = client.get("/metrics")

    assert resp.status_code == 200


# --- API-key auth (Phase 6 production readiness) -----------------------------


def test_json_api_open_when_api_key_unset(client, db, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    db.fetch_distinct_series.return_value = []

    resp = client.get("/series")

    assert resp.status_code == 200


def test_json_api_rejects_missing_key_when_api_key_set(client, db, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_distinct_series.return_value = []

    resp = client.get("/series")

    assert resp.status_code == 401


def test_json_api_rejects_wrong_key_when_api_key_set(client, db, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_distinct_series.return_value = []

    resp = client.get("/series", headers={"X-API-Key": "wrong"})

    assert resp.status_code == 401


def test_json_api_accepts_correct_key_when_api_key_set(client, db, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_distinct_series.return_value = []

    resp = client.get("/series", headers={"X-API-Key": "s3cret"})

    assert resp.status_code == 200


def test_dashboard_stays_open_regardless_of_api_key(client, db, monkeypatch):
    """Dashboard HTML routes are a deliberate exception -- see services/api/main.py
    module docstring."""
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_event_reports.return_value = []

    resp = client.get("/")

    assert resp.status_code == 200


def test_health_stays_open_regardless_of_api_key(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")

    resp = client.get("/health")

    assert resp.status_code == 200


# --- /series -------------------------------------------------------------


def test_list_series(client, db):
    db.fetch_distinct_series.return_value = [("mFRR EAM", "DK1", "up")]

    resp = client.get("/series")

    assert resp.status_code == 200
    assert resp.json() == {"series": [{"market": "mFRR EAM", "zone": "DK1", "product": "up"}]}


def test_series_data_defaults_to_latest_view(client, db):
    db.fetch_series_values.return_value = [
        {
            "time": "2026-07-16T10:00:00",
            "value": 450.5,
            "source": "Energinet",
            "is_provisional": True,
        }
    ]

    resp = client.get("/series/mFRR_capacity/DK1/up")

    assert resp.status_code == 200
    body = resp.json()
    assert body["market"] == "mFRR_capacity"
    assert body["history"] is False
    assert body["count"] == 1

    kwargs = db.fetch_series_values.call_args.kwargs
    assert kwargs["history"] is False
    assert kwargs["limit"] == 500


def test_series_data_history_flag_passed_through(client, db):
    db.fetch_series_values.return_value = []

    resp = client.get("/series/mFRR_capacity/DK1/up?history=true&limit=10")

    assert resp.status_code == 200
    assert resp.json()["history"] is True
    kwargs = db.fetch_series_values.call_args.kwargs
    assert kwargs["history"] is True
    assert kwargs["limit"] == 10


# --- /event-reports --------------------------------------------------------


def test_list_event_reports_default(client, db):
    db.fetch_event_reports.return_value = [SAMPLE_ROW]

    resp = client.get("/event-reports")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["reports"][0]["event_id"] == SAMPLE_ROW["event_id"]
    assert body["reports"][0]["is_correction"] is False


def test_list_event_reports_filters_and_paginates(client, db):
    db.fetch_event_reports.return_value = []

    resp = client.get(
        "/event-reports?market=mFRR+EAM&zone=DK1&product=up&limit=5&offset=10"
        "&time_from=2026-07-01T00:00:00&time_to=2026-07-31T00:00:00"
    )

    assert resp.status_code == 200
    kwargs = db.fetch_event_reports.call_args.kwargs
    assert kwargs["market"] == "mFRR EAM"
    assert kwargs["zone"] == "DK1"
    assert kwargs["product"] == "up"
    assert kwargs["limit"] == 5
    assert kwargs["offset"] == 10
    assert kwargs["time_from"] is not None
    assert kwargs["time_to"] is not None


def test_get_event_report_found(client, db):
    db.fetch_event_report.return_value = SAMPLE_ROW

    resp = client.get(f"/event-reports/{SAMPLE_ROW['event_id']}")

    assert resp.status_code == 200
    assert resp.json()["event_id"] == SAMPLE_ROW["event_id"]


def test_get_event_report_not_found(client, db):
    db.fetch_event_report.return_value = None

    resp = client.get("/event-reports/does-not-exist")

    assert resp.status_code == 404


# --- /triggers -----------------------------------------------------------


def test_list_triggers(client, db):
    db.fetch_triggers.return_value = [
        {
            "id": 1,
            "trigger_type": "price_spike",
            "market": "mFRR EAM",
            "zone": "DK1",
            "product": "up",
            "value": 5000.0,
            "time": "2026-07-16 17:15:00+00:00",
            "baseline": 1200.0,
            "threshold": 3600.0,
            "details": "z-score=4.10",
            "detected_at": "2026-07-16T17:20:00+00:00",
        }
    ]

    resp = client.get("/triggers?market=mFRR+EAM")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["triggers"][0]["trigger_type"] == "price_spike"
    kwargs = db.fetch_triggers.call_args.kwargs
    assert kwargs["market"] == "mFRR EAM"


# --- dashboard pages -------------------------------------------------------


def test_dashboard_home_renders_recent_reports(client, db):
    db.fetch_event_reports.return_value = [SAMPLE_ROW]

    resp = client.get("/")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Balancing energy price hit 4,850 DKK/MWh" in resp.text
    assert "confidence: medium" in resp.text


def test_dashboard_home_renders_with_no_reports(client, db):
    db.fetch_event_reports.return_value = []

    resp = client.get("/")

    assert resp.status_code == 200
    assert "No Event Reports published yet." in resp.text


def test_dashboard_event_report_detail(client, db):
    db.fetch_event_report.return_value = SAMPLE_ROW

    resp = client.get(f"/dashboard/event-reports/{SAMPLE_ROW['event_id']}")

    assert resp.status_code == 200
    assert "Hard data correlates" in resp.text
    assert "Energinet EDS" in resp.text
    assert "EnergiWatch, 2026-07-14" in resp.text


def test_dashboard_event_report_detail_links_to_corrected_report(client, db):
    db.fetch_event_report.side_effect = lambda event_id: (
        CORRECTION_ROW if event_id == CORRECTION_ROW["event_id"] else SAMPLE_ROW
    )

    resp = client.get(f"/dashboard/event-reports/{CORRECTION_ROW['event_id']}")

    assert resp.status_code == 200
    assert "CORRECTION" in resp.text
    assert f"/dashboard/event-reports/{SAMPLE_ROW['event_id']}" in resp.text


def test_dashboard_event_report_detail_not_found(client, db):
    db.fetch_event_report.return_value = None

    resp = client.get("/dashboard/event-reports/does-not-exist")

    assert resp.status_code == 404


def test_dashboard_series_list(client, db):
    db.fetch_distinct_series.return_value = [("mFRR EAM", "DK1", "up")]

    resp = client.get("/dashboard/series")

    assert resp.status_code == 200
    assert "/dashboard/series/mFRR EAM/DK1/up" in resp.text


def test_dashboard_series_detail(client, db):
    db.fetch_series_values.return_value = [
        {
            "time": "2026-07-16T10:00:00",
            "value": 450.5,
            "source": "Energinet",
            "is_provisional": True,
            "ingested_at": "2026-07-16T10:05:00",
        }
    ]

    resp = client.get("/dashboard/series/mFRR_capacity/DK1/up")

    assert resp.status_code == 200
    assert "mFRR_capacity" in resp.text
    assert "450.5" in resp.text


def test_dashboard_series_detail_with_no_data(client, db):
    db.fetch_series_values.return_value = []

    resp = client.get("/dashboard/series/mFRR_capacity/DK1/up")

    assert resp.status_code == 200
    assert "No data for this series yet." in resp.text
