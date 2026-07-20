import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from shared.bess_simulator import BacktestResult
from shared.claim_extractor import ExtractedClaim, ExtractionResult

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
def vector_store():
    store = MagicMock()
    store.ensure_collection = AsyncMock()
    store.is_processed = AsyncMock(return_value=False)
    store.upsert_claims = AsyncMock(return_value=1)
    store.upsert_raw_article = AsyncMock()
    store.scroll_by_source = AsyncMock(return_value=[])
    return store


@pytest.fixture
def client(db, vector_store):
    api_main.app.dependency_overrides[api_main.get_db] = lambda: db
    api_main.app.dependency_overrides[api_main.get_vector_store] = lambda: vector_store
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


def test_dashboard_triggers_renders(client, db):
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

    resp = client.get("/dashboard/triggers")

    assert resp.status_code == 200
    assert "price_spike" in resp.text
    assert "mFRR EAM" in resp.text
    db.fetch_triggers.assert_called_once()


def test_dashboard_triggers_passes_filters_through(client, db):
    db.fetch_triggers.return_value = []

    resp = client.get("/dashboard/triggers?market=mFRR+EAM&zone=DK1&product=up")

    assert resp.status_code == 200
    kwargs = db.fetch_triggers.call_args.kwargs
    assert kwargs["market"] == "mFRR EAM"
    assert kwargs["zone"] == "DK1"
    assert kwargs["product"] == "up"


def test_dashboard_triggers_renders_empty_state(client, db):
    db.fetch_triggers.return_value = []

    resp = client.get("/dashboard/triggers")

    assert resp.status_code == 200
    assert "No triggers recorded yet" in resp.text


# --- /bess (BESS backtest simulator) ----------------------------------------

BESS_RUN_ROW = {
    "id": 1,
    "zone": "DK1",
    "start_time": "2026-07-16T20:00:00+00:00",
    "end_time": "2026-07-17T08:00:00+00:00",
    "config": {"power_mw": 1.0, "capacity_mwh": 2.0},
    "total_arbitrage_revenue_dkk": 120.5,
    "total_capacity_revenue_dkk": 60.0,
    "total_revenue_dkk": 180.5,
    "full_cycle_equivalents": 0.75,
    "tick_count": 48,
    "created_at": "2026-07-17T09:00:00+00:00",
    "total_afrr_activation_revenue_eur": 25.0,
    "total_capacity_revenue_eur": 8.0,
}

BESS_TICK_ROW = {
    "time": "2026-07-16T20:00:00+00:00",
    "soc_mwh": 1.0,
    "soc_fraction": 0.5,
    "action": "idle",
    "day_ahead_price": 500.0,
    "energy_discharged_mwh": 0.0,
    "arbitrage_revenue_dkk": 0.0,
    "capacity_reserved_mw": 0.3,
    "capacity_revenue_dkk": 15.0,
    "capacity_revenue_by_market": {"FCR:price": 10.0, "aFRR_capacity:up": 5.0},
    "cumulative_arbitrage_revenue_dkk": 0.0,
    "cumulative_capacity_revenue_dkk": 15.0,
    "cumulative_total_revenue_dkk": 15.0,
}


def test_trigger_bess_backtest_runs_and_persists(client, db, monkeypatch):
    monkeypatch.setattr(
        api_main,
        "run_backtest",
        lambda db_arg, zone, start, end, config: BacktestResult(
            zone=zone, start_time=start, end_time=end, config=config, ticks=[]
        ),
    )
    db.save_bess_run.return_value = 42

    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == 42
    assert body["zone"] == "DK1"
    assert body["tick_count"] == 0
    db.save_bess_run.assert_called_once()


def test_trigger_bess_backtest_rejects_invalid_config(client, db):
    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
            "power_mw": -1.0,
        },
    )

    assert resp.status_code == 422


def test_list_bess_runs(client, db):
    db.fetch_bess_runs.return_value = [BESS_RUN_ROW]

    resp = client.get("/bess/runs?zone=DK1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["runs"][0]["id"] == 1
    kwargs = db.fetch_bess_runs.call_args.kwargs
    assert kwargs["zone"] == "DK1"


def test_get_bess_run_found(client, db):
    db.fetch_bess_run.return_value = BESS_RUN_ROW

    resp = client.get("/bess/runs/1")

    assert resp.status_code == 200
    assert resp.json()["id"] == 1
    db.fetch_bess_ticks.assert_not_called()


def test_get_bess_run_with_ticks(client, db):
    db.fetch_bess_run.return_value = BESS_RUN_ROW
    db.fetch_bess_ticks.return_value = [BESS_TICK_ROW]

    resp = client.get("/bess/runs/1?include_ticks=true")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ticks"] == [BESS_TICK_ROW]


def test_get_bess_run_not_found(client, db):
    db.fetch_bess_run.return_value = None

    resp = client.get("/bess/runs/999")

    assert resp.status_code == 404


def test_bess_routes_gated_by_api_key(client, db, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_bess_runs.return_value = []

    resp = client.get("/bess/runs")

    assert resp.status_code == 401


def test_dashboard_bess_list_renders(client, db):
    db.fetch_bess_runs.return_value = [BESS_RUN_ROW]

    resp = client.get("/dashboard/bess")

    assert resp.status_code == 200
    assert "BESS" in resp.text


def test_dashboard_bess_new_form_renders(client):
    resp = client.get("/dashboard/bess/new")

    assert resp.status_code == 200


def test_dashboard_bess_detail_renders(client, db):
    db.fetch_bess_run.return_value = BESS_RUN_ROW
    db.fetch_bess_ticks.return_value = [BESS_TICK_ROW]

    resp = client.get("/dashboard/bess/1")

    assert resp.status_code == 200


def test_dashboard_bess_detail_not_found(client, db):
    db.fetch_bess_run.return_value = None

    resp = client.get("/dashboard/bess/999")

    assert resp.status_code == 404


def test_dashboard_bess_trigger_redirects_to_detail(client, db, monkeypatch):
    monkeypatch.setattr(
        api_main,
        "run_backtest",
        lambda db_arg, zone, start, end, config: BacktestResult(
            zone=zone, start_time=start, end_time=end, config=config, ticks=[]
        ),
    )
    db.save_bess_run.return_value = 7

    resp = client.post(
        "/dashboard/bess/new",
        data={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00",
            "end_time": "2026-07-17T08:00",
            "power_mw": "1.0",
            "capacity_mwh": "2.0",
            "round_trip_efficiency": "0.9",
            "soc_min_fraction": "0.1",
            "soc_max_fraction": "0.9",
            "starting_soc_fraction": "0.5",
            "arbitrage_lookback_periods": "30",
            "arbitrage_z_threshold": "0.5",
            "capacity_commit_mw": "0.3",
            "afrr_activation_participation_rate": "0.3",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/bess/7"


def test_dashboard_bess_trigger_include_ffr_uses_price_ranked_for_dk2(client, db, monkeypatch):
    captured_configs = []

    def fake_run_backtest(db_arg, zone, start, end, config):
        captured_configs.append(config)
        return BacktestResult(zone=zone, start_time=start, end_time=end, config=config, ticks=[])

    monkeypatch.setattr(api_main, "run_backtest", fake_run_backtest)
    db.save_bess_run.return_value = 9

    resp = client.post(
        "/dashboard/bess/new",
        data={
            "zone": "DK2",
            "start_time": "2026-07-16T20:00",
            "end_time": "2026-07-17T08:00",
            "power_mw": "1.0",
            "capacity_mwh": "2.0",
            "round_trip_efficiency": "0.9",
            "soc_min_fraction": "0.1",
            "soc_max_fraction": "0.9",
            "starting_soc_fraction": "0.5",
            "arbitrage_lookback_periods": "30",
            "arbitrage_z_threshold": "0.5",
            "capacity_commit_mw": "0.3",
            "afrr_activation_participation_rate": "0.3",
            "include_ffr": "true",
            "include_afrr_down": "true",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert len(captured_configs) == 1
    assert ("FFR", "price") in captured_configs[0].capacity_markets
    assert ("aFRR_capacity", "down") in captured_configs[0].capacity_markets
    assert captured_configs[0].capacity_allocation == "price_ranked"


def test_dashboard_bess_trigger_without_ffr_keeps_even_allocation(client, db, monkeypatch):
    captured_configs = []

    def fake_run_backtest(db_arg, zone, start, end, config):
        captured_configs.append(config)
        return BacktestResult(zone=zone, start_time=start, end_time=end, config=config, ticks=[])

    monkeypatch.setattr(api_main, "run_backtest", fake_run_backtest)
    db.save_bess_run.return_value = 9

    resp = client.post(
        "/dashboard/bess/new",
        data={
            "zone": "DK2",
            "start_time": "2026-07-16T20:00",
            "end_time": "2026-07-17T08:00",
            "power_mw": "1.0",
            "capacity_mwh": "2.0",
            "round_trip_efficiency": "0.9",
            "soc_min_fraction": "0.1",
            "soc_max_fraction": "0.9",
            "starting_soc_fraction": "0.5",
            "arbitrage_lookback_periods": "30",
            "arbitrage_z_threshold": "0.5",
            "capacity_commit_mw": "0.3",
            "afrr_activation_participation_rate": "0.3",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert captured_configs[0].capacity_allocation == "even"


def test_dashboard_bess_trigger_rejects_ffr_for_dk1(client, db, monkeypatch):
    """FFR is DK2-only (shared/datasets.py's ffr_dk2 entry, fixed
    zone="DK2") -- ticking it for a DK1 run must be rejected server-side,
    not silently earn nothing."""
    mock_run_backtest = MagicMock()
    monkeypatch.setattr(api_main, "run_backtest", mock_run_backtest)

    resp = client.post(
        "/dashboard/bess/new",
        data={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00",
            "end_time": "2026-07-17T08:00",
            "power_mw": "1.0",
            "capacity_mwh": "2.0",
            "round_trip_efficiency": "0.9",
            "soc_min_fraction": "0.1",
            "soc_max_fraction": "0.9",
            "starting_soc_fraction": "0.5",
            "arbitrage_lookback_periods": "30",
            "arbitrage_z_threshold": "0.5",
            "capacity_commit_mw": "0.3",
            "afrr_activation_participation_rate": "0.3",
            "include_ffr": "true",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert "not available in zone" in resp.text
    mock_run_backtest.assert_not_called()


def test_dashboard_bess_trigger_shows_error_on_invalid_config(client, db):
    resp = client.post(
        "/dashboard/bess/new",
        data={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00",
            "end_time": "2026-07-17T08:00",
            "power_mw": "-1.0",
            "capacity_mwh": "2.0",
            "round_trip_efficiency": "0.9",
            "soc_min_fraction": "0.1",
            "soc_max_fraction": "0.9",
            "starting_soc_fraction": "0.5",
            "arbitrage_lookback_periods": "30",
            "arbitrage_z_threshold": "0.5",
            "capacity_commit_mw": "0.3",
            "afrr_activation_participation_rate": "0.3",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 200
    assert "Error" in resp.text
    db.save_bess_run.assert_not_called()


# --- /manual-articles (LinkedIn paste-in tool) ------------------------------

MANUAL_SUBMISSION = {
    "url": "https://www.linkedin.com/posts/jane-doe_dk1-afrr-pricing-activity-123",
    "author": "Jane Doe",
    "title": "aFRR pricing take",
    "text": "DK1 aFRR capacity prices hit a new high this week. I think this is driven by "
    "reduced wind availability.",
}


def test_submit_manual_article_happy_path(client, vector_store, monkeypatch):
    """Claims come back typed, and go through ensure_collection/upsert_claims exactly
    like the RSS crawler pipeline does."""
    monkeypatch.delenv("API_KEY", raising=False)
    extraction = ExtractionResult(
        summary="Analyst comments on DK1 aFRR capacity price trends.",
        claims=[
            ExtractedClaim(claim="DK1 aFRR capacity prices hit a new high.", claim_type="fact"),
            ExtractedClaim(
                claim="Reduced wind availability is driving the increase.", claim_type="theory"
            ),
        ],
    )
    with_extract = AsyncMock(return_value=extraction)
    with monkeypatch.context() as m:
        m.setattr(api_main, "extract_claims", with_extract)
        resp = client.post("/manual-articles", json=MANUAL_SUBMISSION)

    assert resp.status_code == 200
    body = resp.json()
    assert body["feed_tier"] == "tier2"
    assert body["already_processed"] is False
    assert body["stored_raw"] is False
    assert [c["claim_type"] for c in body["claims"]] == ["fact", "theory"]

    vector_store.ensure_collection.assert_awaited_once()
    vector_store.upsert_claims.assert_awaited_once()
    article_arg = vector_store.upsert_claims.call_args.args[0]
    assert article_arg.feed_tier == "tier2"
    assert article_arg.feed_name == "LinkedIn"
    assert article_arg.url == MANUAL_SUBMISSION["url"]


def test_submit_manual_article_downgrades_tier2_fact_to_theory(client, vector_store, monkeypatch):
    """README §6: reuses shared/claim_extractor.py's existing Tier 2 downgrade rather than
    reimplementing it -- a claim Claude marks 'fact' comes back as 'theory' because the
    ArticleRef built here is always feed_tier='tier2'. Exercises the real
    `api_main.extract_claims` (not mocked) against a mocked Anthropic client, so the
    downgrade logic in shared/claim_extractor.py actually runs."""
    monkeypatch.delenv("API_KEY", raising=False)

    from types import SimpleNamespace

    response_text = (
        '{"summary": "s", "claims": [{"claim": "DK1 aFRR hit a new high.", "claim_type": "fact"}]}'
    )
    text_block = SimpleNamespace(type="text", text=response_text)
    message = SimpleNamespace(content=[text_block])
    fake_anthropic_client = SimpleNamespace(
        messages=SimpleNamespace(create=AsyncMock(return_value=message))
    )

    # api_main.extract_claims is the real function object imported from
    # shared.claim_extractor; call it directly with an injected fake client
    # (its own `client=` parameter) instead of stubbing internals.
    real_extract_claims = api_main.extract_claims

    async def call_with_fake_client(text, article, client=None):
        return await real_extract_claims(text, article, client=fake_anthropic_client)

    monkeypatch.setattr(api_main, "extract_claims", call_with_fake_client)

    resp = client.post("/manual-articles", json=MANUAL_SUBMISSION)

    assert resp.status_code == 200
    body = resp.json()
    assert body["claims"][0]["claim_type"] == "theory"


def test_submit_manual_article_dedup_skips_reextraction_on_resubmit(
    client, vector_store, monkeypatch
):
    monkeypatch.delenv("API_KEY", raising=False)
    vector_store.is_processed = AsyncMock(return_value=True)
    fake_extract = AsyncMock()
    monkeypatch.setattr(api_main, "extract_claims", fake_extract)

    resp = client.post("/manual-articles", json=MANUAL_SUBMISSION)

    assert resp.status_code == 200
    body = resp.json()
    assert body["already_processed"] is True
    assert body["claims"] == []
    fake_extract.assert_not_awaited()
    vector_store.upsert_claims.assert_not_awaited()
    vector_store.upsert_raw_article.assert_not_awaited()


def test_submit_manual_article_stores_raw_when_no_api_key(client, vector_store, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    fake_extract = AsyncMock(return_value=None)
    monkeypatch.setattr(api_main, "extract_claims", fake_extract)

    resp = client.post("/manual-articles", json=MANUAL_SUBMISSION)

    assert resp.status_code == 200
    body = resp.json()
    assert body["stored_raw"] is True
    assert body["claims"] == []
    vector_store.upsert_raw_article.assert_awaited_once()
    vector_store.upsert_claims.assert_not_awaited()


def test_submit_manual_article_gated_by_api_key(client, vector_store, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")

    resp = client.post("/manual-articles", json=MANUAL_SUBMISSION)

    assert resp.status_code == 401


def test_submit_manual_article_accepts_correct_api_key(client, vector_store, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    fake_extract = AsyncMock(return_value=ExtractionResult(summary="s", claims=[]))
    monkeypatch.setattr(api_main, "extract_claims", fake_extract)

    resp = client.post("/manual-articles", json=MANUAL_SUBMISSION, headers={"X-API-Key": "s3cret"})

    assert resp.status_code == 200


def test_submit_manual_article_requires_url(client, vector_store, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    resp = client.post("/manual-articles", json={"text": "some text, no url"})

    assert resp.status_code == 422


# --- /manual-articles LinkedIn embed auto-detection (shared/linkedin_embed.py) ---
#
# `text` doubles as an auto-detecting input: iframe HTML/embed URLs/regular
# post URLs are resolved via `resolve_linkedin_content` (mocked here --
# shared/linkedin_embed.py's own tests cover the real HTTP fetch/parse) and,
# when that resolves, override both the submitted text and the submitted
# `url` (with the fetched canonical URL). Plain text -- and any fetch
# failure -- fall back to today's raw-pasted-text behavior untouched.

LINKEDIN_IFRAME_SUBMISSION = {
    "url": "https://example.test/placeholder-not-used",
    "author": None,
    "title": None,
    "text": (
        '<iframe src="https://www.linkedin.com/embed/feed/update/'
        'urn:li:share:7479918035902959616?collapsed=1" height="647" width="504" '
        'frameborder="0" allowfullscreen title="Embedded post"></iframe>'
    ),
}


def test_submit_manual_article_resolves_linkedin_embed_iframe(client, vector_store, monkeypatch):
    """A pasted embed iframe is fetched+parsed, and the *canonical* URL --
    not the submitted placeholder `url` -- is what's used for dedup/storage."""
    monkeypatch.delenv("API_KEY", raising=False)

    from shared.linkedin_embed import LinkedInEmbedContent

    fetched = LinkedInEmbedContent(
        text="The aFRR capacity market (CM) and energy activation market (EAM) prices in DK2 "
        "are set to drop significantly due to a sharp decline in aFRR demand.",
        canonical_url="https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936",
        author="Andreas Barnekov Thingvad",
    )
    monkeypatch.setattr(api_main, "resolve_linkedin_content", AsyncMock(return_value=fetched))

    extraction = ExtractionResult(
        summary="DK2 aFRR prices set to drop due to lower demand.",
        claims=[
            ExtractedClaim(
                claim="DK2 aFRR capacity/energy prices will drop significantly.",
                claim_type="forecast",
            )
        ],
    )
    monkeypatch.setattr(api_main, "extract_claims", AsyncMock(return_value=extraction))

    resp = client.post("/manual-articles", json=LINKEDIN_IFRAME_SUBMISSION)

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"
    assert body["author"] == "Andreas Barnekov Thingvad"
    assert body["claims"][0]["claim_type"] == "forecast"

    article_arg = vector_store.upsert_claims.call_args.args[0]
    assert (
        article_arg.url
        == "https://www.linkedin.com/feed/update/urn:li:activity:7479918037115047936"
    )

    claims_arg = api_main.extract_claims.call_args.args[0]
    assert "DK2" in claims_arg
    assert "iframe" not in claims_arg


def test_submit_manual_article_prefers_submitted_author_over_fetched(
    client, vector_store, monkeypatch
):
    """The human-typed `author` form field wins over whatever the embed page's
    title-tag heuristic produced."""
    monkeypatch.delenv("API_KEY", raising=False)

    from shared.linkedin_embed import LinkedInEmbedContent

    fetched = LinkedInEmbedContent(
        text="Some fetched post text.",
        canonical_url="https://www.linkedin.com/feed/update/urn:li:activity:111",
        author="Fetched Author",
    )
    monkeypatch.setattr(api_main, "resolve_linkedin_content", AsyncMock(return_value=fetched))
    monkeypatch.setattr(
        api_main, "extract_claims", AsyncMock(return_value=ExtractionResult(summary="s", claims=[]))
    )

    submission = dict(LINKEDIN_IFRAME_SUBMISSION, author="Explicit Human Author")
    resp = client.post("/manual-articles", json=submission)

    assert resp.status_code == 200
    assert resp.json()["author"] == "Explicit Human Author"


def test_submit_manual_article_falls_back_to_raw_text_when_no_linkedin_content_detected(
    client, vector_store, monkeypatch
):
    """Plain text (the pre-existing behavior) never touches `resolve_linkedin_content`'s
    HTTP fetch path -- it just returns None and the submitted text/url pass through as-is."""
    monkeypatch.delenv("API_KEY", raising=False)

    fake_extract = AsyncMock(return_value=ExtractionResult(summary="s", claims=[]))
    monkeypatch.setattr(api_main, "extract_claims", fake_extract)

    resp = client.post("/manual-articles", json=MANUAL_SUBMISSION)

    assert resp.status_code == 200
    assert resp.json()["url"] == MANUAL_SUBMISSION["url"]
    fake_extract.assert_awaited_once()
    claims_arg = fake_extract.call_args.args[0]
    assert claims_arg == MANUAL_SUBMISSION["text"]


def test_submit_manual_article_falls_back_to_raw_text_when_linkedin_fetch_fails(
    client, vector_store, monkeypatch
):
    """A recognized LinkedIn URL whose fetch fails (network error, private/deleted post,
    etc) degrades gracefully to the submitted text/url -- never a 500."""
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(api_main, "resolve_linkedin_content", AsyncMock(return_value=None))

    fake_extract = AsyncMock(return_value=ExtractionResult(summary="s", claims=[]))
    monkeypatch.setattr(api_main, "extract_claims", fake_extract)

    resp = client.post("/manual-articles", json=LINKEDIN_IFRAME_SUBMISSION)

    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == LINKEDIN_IFRAME_SUBMISSION["url"]
    claims_arg = fake_extract.call_args.args[0]
    assert claims_arg == LINKEDIN_IFRAME_SUBMISSION["text"]


# --- dashboard manual-article form ------------------------------------------


def test_dashboard_manual_article_form_renders(client):
    resp = client.get("/dashboard/manual-articles")

    assert resp.status_code == 200
    assert "Submit a LinkedIn Post" in resp.text


def test_dashboard_manual_article_form_stays_open_regardless_of_api_key(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")

    resp = client.get("/dashboard/manual-articles")

    assert resp.status_code == 200


def test_dashboard_submit_manual_article_renders_confirmation(client, vector_store, monkeypatch):
    monkeypatch.setenv(
        "API_KEY", "s3cret"
    )  # dashboard form still works even when JSON API is gated
    extraction = ExtractionResult(
        summary="s", claims=[ExtractedClaim(claim="DK1 aFRR spike.", claim_type="theory")]
    )
    fake_extract = AsyncMock(return_value=extraction)
    monkeypatch.setattr(api_main, "extract_claims", fake_extract)

    resp = client.post(
        "/dashboard/manual-articles",
        data={
            "url": MANUAL_SUBMISSION["url"],
            "author": MANUAL_SUBMISSION["author"],
            "title": MANUAL_SUBMISSION["title"],
            "text": MANUAL_SUBMISSION["text"],
        },
    )

    assert resp.status_code == 200
    assert "DK1 aFRR spike." in resp.text
    assert "theory" in resp.text


def test_dashboard_recent_manual_claims_lists_scroll_results(client, vector_store):
    vector_store.scroll_by_source.return_value = [
        {
            "claim": "DK1 aFRR capacity prices hit a new high.",
            "claim_type": "theory",
            "article_url": MANUAL_SUBMISSION["url"],
            "article_title": MANUAL_SUBMISSION["title"],
            "author": MANUAL_SUBMISSION["author"],
            "retrieved_at": "2026-07-16T09:00:00+00:00",
        }
    ]

    resp = client.get("/dashboard/manual-articles/recent")

    assert resp.status_code == 200
    assert "DK1 aFRR capacity prices hit a new high." in resp.text
    vector_store.scroll_by_source.assert_awaited_once_with("LinkedIn", limit=100)


def test_dashboard_recent_manual_claims_empty_state(client, vector_store):
    vector_store.scroll_by_source.return_value = []

    resp = client.get("/dashboard/manual-articles/recent")

    assert resp.status_code == 200
    assert "No manually-submitted posts yet." in resp.text


# --- on-demand orchestrator/crawler triggers (cost-control escape hatch) --


@pytest.fixture
def orchestrator_main_mock():
    module = MagicMock()
    module.run_synthesis_cycle = AsyncMock(
        return_value={"triggers_fired": 3, "reports_published": 1}
    )
    return module


@pytest.fixture
def crawler_main_mock():
    module = MagicMock()
    module.run_crawl_cycle = AsyncMock(
        return_value={"articles_processed": 5, "claims_extracted": 12}
    )
    return module


def test_trigger_orchestrator_run_now_calls_real_cycle_function(
    client, monkeypatch, orchestrator_main_mock
):
    monkeypatch.delenv("API_KEY", raising=False)
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        orchestrator_main_mock
    )
    try:
        resp = client.post("/orchestrator/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 200
    assert resp.json() == {"triggers_fired": 3, "reports_published": 1}
    orchestrator_main_mock.run_synthesis_cycle.assert_awaited_once()


def test_trigger_orchestrator_run_now_gated_by_api_key(client, monkeypatch, orchestrator_main_mock):
    monkeypatch.setenv("API_KEY", "s3cret")
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        orchestrator_main_mock
    )
    try:
        resp = client.post("/orchestrator/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 401
    orchestrator_main_mock.run_synthesis_cycle.assert_not_awaited()


def test_trigger_orchestrator_run_now_accepts_correct_api_key(
    client, monkeypatch, orchestrator_main_mock
):
    monkeypatch.setenv("API_KEY", "s3cret")
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        orchestrator_main_mock
    )
    try:
        resp = client.post("/orchestrator/run-now", headers={"X-API-Key": "s3cret"})
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 200
    orchestrator_main_mock.run_synthesis_cycle.assert_awaited_once()


def test_trigger_crawler_run_now_calls_real_cycle_function(client, monkeypatch, crawler_main_mock):
    monkeypatch.delenv("API_KEY", raising=False)
    api_main.app.dependency_overrides[api_main.get_crawler_main] = lambda: crawler_main_mock
    try:
        resp = client.post("/crawler/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_crawler_main]

    assert resp.status_code == 200
    assert resp.json() == {"articles_processed": 5, "claims_extracted": 12}
    crawler_main_mock.run_crawl_cycle.assert_awaited_once()


def test_trigger_crawler_run_now_gated_by_api_key(client, monkeypatch, crawler_main_mock):
    monkeypatch.setenv("API_KEY", "s3cret")
    api_main.app.dependency_overrides[api_main.get_crawler_main] = lambda: crawler_main_mock
    try:
        resp = client.post("/crawler/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_crawler_main]

    assert resp.status_code == 401
    crawler_main_mock.run_crawl_cycle.assert_not_awaited()


def test_dashboard_trigger_orchestrator_run_now_renders_summary(
    client, db, monkeypatch, orchestrator_main_mock
):
    monkeypatch.delenv("API_KEY", raising=False)
    db.fetch_event_reports.return_value = []
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        orchestrator_main_mock
    )
    try:
        resp = client.post("/dashboard/orchestrator/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 200
    assert "3 trigger(s) fired" in resp.text
    assert "1 Event Report(s) published" in resp.text
    orchestrator_main_mock.run_synthesis_cycle.assert_awaited_once()


def test_dashboard_trigger_orchestrator_run_now_stays_open_regardless_of_api_key(
    client, db, monkeypatch, orchestrator_main_mock
):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_event_reports.return_value = []
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        orchestrator_main_mock
    )
    try:
        resp = client.post("/dashboard/orchestrator/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 200
    orchestrator_main_mock.run_synthesis_cycle.assert_awaited_once()


def test_dashboard_trigger_crawler_run_now_renders_summary(
    client, db, monkeypatch, crawler_main_mock
):
    monkeypatch.delenv("API_KEY", raising=False)
    db.fetch_event_reports.return_value = []
    api_main.app.dependency_overrides[api_main.get_crawler_main] = lambda: crawler_main_mock
    try:
        resp = client.post("/dashboard/crawler/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_crawler_main]

    assert resp.status_code == 200
    assert "5 article(s) processed" in resp.text
    assert "12 claim(s) extracted" in resp.text
    crawler_main_mock.run_crawl_cycle.assert_awaited_once()


def test_dashboard_home_run_now_buttons_present(client, db):
    db.fetch_event_reports.return_value = []

    resp = client.get("/")

    assert resp.status_code == 200
    assert "/dashboard/orchestrator/run-now" in resp.text
    assert "/dashboard/crawler/run-now" in resp.text


# --- on-demand historical backfill (shared/backfill.py) ---------------------


@pytest.fixture
def backfill_mock():
    return AsyncMock(
        return_value={
            "start": datetime(2026, 6, 1, tzinfo=UTC),
            "end": datetime(2026, 7, 1, tzinfo=UTC),
            "datasets": [
                {
                    "dataset": "fcr_dk1",
                    "dataset_id": "FcrDK1",
                    "chunks_fetched": 5,
                    "chunks_failed": 0,
                    "records_fetched": 10,
                    "rows_saved": 10,
                    "earliest_record_time": "2026-06-01T00:00:00",
                    "latest_record_time": "2026-06-30T23:00:00",
                }
            ],
            "total_rows_saved": 10,
        }
    )


def test_trigger_backfill_calls_run_backfill(client, monkeypatch, backfill_mock):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post("/ingestor/backfill", json={})

    assert resp.status_code == 200
    assert resp.json()["total_rows_saved"] == 10
    backfill_mock.assert_awaited_once()


def test_trigger_backfill_gated_by_api_key(client, monkeypatch, backfill_mock):
    monkeypatch.setenv("API_KEY", "s3cret")
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post("/ingestor/backfill", json={})

    assert resp.status_code == 401
    backfill_mock.assert_not_awaited()


def test_trigger_backfill_accepts_correct_api_key(client, monkeypatch, backfill_mock):
    monkeypatch.setenv("API_KEY", "s3cret")
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post("/ingestor/backfill", json={}, headers={"X-API-Key": "s3cret"})

    assert resp.status_code == 200
    backfill_mock.assert_awaited_once()


def test_trigger_backfill_defaults_to_trailing_30_days(client, monkeypatch, backfill_mock):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    client.post("/ingestor/backfill", json={})

    start_time, end_time = backfill_mock.call_args.args[:2]
    assert (end_time - start_time).days == 30


def test_trigger_backfill_passes_through_explicit_window_and_datasets(
    client, monkeypatch, backfill_mock
):
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post(
        "/ingestor/backfill",
        json={
            "start_time": "2025-10-01T00:00:00Z",
            "end_time": "2025-11-01T00:00:00Z",
            "datasets": ["fcr_dk1", "day_ahead_prices"],
        },
    )

    assert resp.status_code == 200
    start_time, end_time = backfill_mock.call_args.args[:2]
    assert start_time == datetime(2025, 10, 1, tzinfo=UTC)
    assert end_time == datetime(2025, 11, 1, tzinfo=UTC)
    assert backfill_mock.call_args.kwargs["dataset_names"] == ["fcr_dk1", "day_ahead_prices"]


def test_trigger_backfill_rejects_invalid_window_with_422(client, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    async def _raise(*_args, **_kwargs):
        raise ValueError("start must be before end")

    monkeypatch.setattr(api_main, "run_backfill", _raise)

    resp = client.post(
        "/ingestor/backfill",
        json={
            "start_time": "2026-07-01T00:00:00Z",
            "end_time": "2026-06-01T00:00:00Z",
        },
    )

    assert resp.status_code == 422


def test_dashboard_trigger_backfill_renders_summary(client, db, monkeypatch, backfill_mock):
    monkeypatch.delenv("API_KEY", raising=False)
    db.fetch_event_reports.return_value = []
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post("/dashboard/ingestor/backfill", data={"days": "30", "datasets": ""})

    assert resp.status_code == 200
    assert "10 row(s) saved" in resp.text
    backfill_mock.assert_awaited_once()
    assert backfill_mock.call_args.kwargs["dataset_names"] is None


def test_dashboard_trigger_backfill_parses_dataset_list(client, db, monkeypatch, backfill_mock):
    monkeypatch.delenv("API_KEY", raising=False)
    db.fetch_event_reports.return_value = []
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post(
        "/dashboard/ingestor/backfill",
        data={"days": "90", "datasets": "fcr_dk1, day_ahead_prices"},
    )

    assert resp.status_code == 200
    assert backfill_mock.call_args.kwargs["dataset_names"] == ["fcr_dk1", "day_ahead_prices"]


def test_dashboard_trigger_backfill_stays_open_regardless_of_api_key(
    client, db, monkeypatch, backfill_mock
):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_event_reports.return_value = []
    monkeypatch.setattr(api_main, "run_backfill", backfill_mock)

    resp = client.post("/dashboard/ingestor/backfill", data={"days": "30", "datasets": ""})

    assert resp.status_code == 200
    backfill_mock.assert_awaited_once()


def test_dashboard_home_backfill_form_present(client, db):
    db.fetch_event_reports.return_value = []

    resp = client.get("/")

    assert resp.status_code == 200
    assert "/dashboard/ingestor/backfill" in resp.text


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


# --- max_cycles_per_day passthrough (shared/bess_simulator.py cycle cap) ----


def test_trigger_bess_backtest_passes_max_cycles_per_day_override(client, db, monkeypatch):
    captured_configs = []

    def fake_run_backtest(db_arg, zone, start, end, config):
        captured_configs.append(config)
        return BacktestResult(zone=zone, start_time=start, end_time=end, config=config, ticks=[])

    monkeypatch.setattr(api_main, "run_backtest", fake_run_backtest)
    db.save_bess_run.return_value = 7

    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
            "max_cycles_per_day": 2.5,
        },
    )

    assert resp.status_code == 200
    assert captured_configs[0].max_cycles_per_day == 2.5


def test_trigger_bess_backtest_max_cycles_per_day_defaults_to_config_default(
    client, db, monkeypatch
):
    captured_configs = []

    def fake_run_backtest(db_arg, zone, start, end, config):
        captured_configs.append(config)
        return BacktestResult(zone=zone, start_time=start, end_time=end, config=config, ticks=[])

    monkeypatch.setattr(api_main, "run_backtest", fake_run_backtest)
    db.save_bess_run.return_value = 7

    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
        },
    )

    assert resp.status_code == 200
    assert captured_configs[0].max_cycles_per_day == 1.5  # BessConfig's own default, untouched


# --- afrr_activation_participation_rate / capacity_markets passthrough -----
# (shared/bess_simulator.py FCR-D + aFRR activation revenue additions)


def test_trigger_bess_backtest_passes_afrr_activation_participation_rate_override(
    client, db, monkeypatch
):
    captured_configs = []

    def fake_run_backtest(db_arg, zone, start, end, config):
        captured_configs.append(config)
        return BacktestResult(zone=zone, start_time=start, end_time=end, config=config, ticks=[])

    monkeypatch.setattr(api_main, "run_backtest", fake_run_backtest)
    db.save_bess_run.return_value = 7

    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK2",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
            "afrr_activation_participation_rate": 0.6,
        },
    )

    assert resp.status_code == 200
    assert captured_configs[0].afrr_activation_participation_rate == 0.6


def test_trigger_bess_backtest_passes_capacity_markets_override(client, db, monkeypatch):
    captured_configs = []

    def fake_run_backtest(db_arg, zone, start, end, config):
        captured_configs.append(config)
        return BacktestResult(zone=zone, start_time=start, end_time=end, config=config, ticks=[])

    monkeypatch.setattr(api_main, "run_backtest", fake_run_backtest)
    db.save_bess_run.return_value = 7

    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK2",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
            "capacity_markets": [["FCR", "price"], ["FCR", "up"], ["FCR", "down"]],
        },
    )

    assert resp.status_code == 200
    assert list(captured_configs[0].capacity_markets) == [
        ("FCR", "price"),
        ("FCR", "up"),
        ("FCR", "down"),
    ]


def test_trigger_bess_backtest_response_includes_total_afrr_activation_revenue_eur(
    client, db, monkeypatch
):
    monkeypatch.setattr(
        api_main,
        "run_backtest",
        lambda db_arg, zone, start, end, config: BacktestResult(
            zone=zone, start_time=start, end_time=end, config=config, ticks=[]
        ),
    )
    db.save_bess_run.return_value = 42

    resp = client.post(
        "/bess/backtest",
        json={
            "zone": "DK1",
            "start_time": "2026-07-16T20:00:00Z",
            "end_time": "2026-07-17T08:00:00Z",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["total_afrr_activation_revenue_eur"] == 0.0


# --- Morning Brief (M5) JSON routes -----------------------------------------

MORNING_BRIEF_ROW = {
    "id": 1,
    "brief_date": "2026-07-17",
    "published_at": datetime(2026, 7, 17, 7, 0, tzinfo=UTC),
    "price_recap": {"headline": "Prices were mild", "zone_summaries": [], "causal_factors": []},
    "forecast_month_id": 10,
    "forecast_quarter_id": 11,
    "forecast_year_id": 12,
    "bess_estimates": [
        {
            "config_label": "Small commercial (1 MW / 2 MWh)",
            "zone": "DK1",
            "run_id": 99,
            "total_revenue_dkk": 1234.5,
            "full_cycle_equivalents": 12.0,
            "cycle_cap_was_binding": True,
        }
    ],
    "brief": {
        "brief_date": "2026-07-17",
        "headline": "Prices were mild",
        "zone_summaries": [],
        "causal_factors": [],
        "forecasts": {"month": None, "quarter": None, "year": None},
        "bess_estimates": [],
        "jargon_glossary": {},
    },
    "slack_sent": True,
    "email_sent": False,
}


def test_list_morning_briefs(client, db):
    db.fetch_morning_briefs.return_value = [MORNING_BRIEF_ROW]

    resp = client.get("/morning-briefs")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["briefs"][0]["id"] == 1


def test_get_morning_brief_found(client, db):
    db.fetch_morning_brief.return_value = MORNING_BRIEF_ROW

    resp = client.get("/morning-briefs/1")

    assert resp.status_code == 200
    assert resp.json()["id"] == 1


def test_get_morning_brief_not_found(client, db):
    db.fetch_morning_brief.return_value = None

    resp = client.get("/morning-briefs/999")

    assert resp.status_code == 404


def test_morning_brief_routes_gated_by_api_key(client, db, monkeypatch):
    monkeypatch.setenv("API_KEY", "s3cret")
    db.fetch_morning_briefs.return_value = []

    resp = client.get("/morning-briefs")

    assert resp.status_code == 401


@pytest.fixture
def morning_brief_orchestrator_mock():
    module = MagicMock()
    module.run_morning_brief = AsyncMock(
        return_value={
            "brief_date": "2026-07-17",
            "brief_id": 1,
            "slack_sent": True,
            "email_sent": False,
            "bess_estimates_count": 4,
        }
    )
    return module


def test_trigger_morning_brief_run_now_calls_real_pipeline(
    client, monkeypatch, morning_brief_orchestrator_mock
):
    monkeypatch.delenv("API_KEY", raising=False)
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        morning_brief_orchestrator_mock
    )
    try:
        resp = client.post("/morning-briefs/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 200
    assert resp.json()["brief_id"] == 1
    morning_brief_orchestrator_mock.run_morning_brief.assert_awaited_once()


def test_trigger_morning_brief_run_now_gated_by_api_key(
    client, monkeypatch, morning_brief_orchestrator_mock
):
    monkeypatch.setenv("API_KEY", "s3cret")
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        morning_brief_orchestrator_mock
    )
    try:
        resp = client.post("/morning-briefs/run-now")
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 401
    morning_brief_orchestrator_mock.run_morning_brief.assert_not_awaited()


# --- Morning Brief dashboard --------------------------------------------------


def test_dashboard_morning_briefs_list(client, db):
    db.fetch_morning_briefs.return_value = [MORNING_BRIEF_ROW]

    resp = client.get("/dashboard/morning-briefs")

    assert resp.status_code == 200
    assert "/dashboard/morning-briefs/1" in resp.text


def test_dashboard_morning_brief_detail_found(client, db):
    db.fetch_morning_brief.return_value = MORNING_BRIEF_ROW

    resp = client.get("/dashboard/morning-briefs/1")

    assert resp.status_code == 200
    assert "Prices were mild" in resp.text


def test_dashboard_morning_brief_detail_shows_zero_price_periods(client, db):
    """Stage 4: 'FFR cleared at 0 for N periods' framing, not a silent zero."""
    row = {
        **MORNING_BRIEF_ROW,
        "bess_estimates": [
            {
                **MORNING_BRIEF_ROW["bess_estimates"][0],
                "zero_price_periods_by_leg": {"FFR:price": 720},
            }
        ],
    }
    db.fetch_morning_brief.return_value = row

    resp = client.get("/dashboard/morning-briefs/1")

    assert resp.status_code == 200
    assert "FFR:price" in resp.text
    assert "720" in resp.text


def test_dashboard_morning_brief_detail_not_found(client, db):
    db.fetch_morning_brief.return_value = None

    resp = client.get("/dashboard/morning-briefs/999")

    assert resp.status_code == 404


def test_dashboard_trigger_morning_brief_run_now_redirects_to_detail(
    client, db, monkeypatch, morning_brief_orchestrator_mock
):
    monkeypatch.delenv("API_KEY", raising=False)
    api_main.app.dependency_overrides[api_main.get_orchestrator_main] = lambda: (
        morning_brief_orchestrator_mock
    )
    try:
        resp = client.post("/dashboard/morning-briefs/run-now", follow_redirects=False)
    finally:
        del api_main.app.dependency_overrides[api_main.get_orchestrator_main]

    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/morning-briefs/1"
    morning_brief_orchestrator_mock.run_morning_brief.assert_awaited_once()


def test_dashboard_home_shows_morning_brief_run_now_button(client, db):
    db.fetch_event_reports.return_value = []

    resp = client.get("/")

    assert resp.status_code == 200
    assert "/dashboard/morning-briefs/run-now" in resp.text
