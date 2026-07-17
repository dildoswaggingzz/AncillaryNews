import json

import httpx
import pytest
import respx

from shared.slack_notifier import send_event_report_alert, send_slack_alert

TRIGGER = {
    "trigger_type": "price_spike",
    "market": "mFRR_capacity",
    "zone": "DK1",
    "product": "up",
    "value": 4850.0,
    "baseline": 1200.0,
    "threshold": 3600.0,
    "time": "2026-07-16T17:15:00",
    "details": "z-score=4.10 over 45 historical point(s)",
}

WEBHOOK_URL = "https://hooks.slack.example/T00/B00/xxx"

EVENT_REPORT = {
    "event_id": "2026-07-16T17:15:00+00:00-DK1-mFRR-EAM-up",
    "market": "mFRR EAM",
    "zone": "DK1",
    "direction": "up",
    "observation": "Balancing energy price hit 4,850 DKK/MWh vs 30-day P95 of 1,200",
    "hard_data_correlates": [
        {"signal": "mFRR EAM price", "value": "4,850 DKK/MWh", "source": "Energinet"}
    ],
    "market_theories": [
        {
            "claim": "Analysts point to low wind + Karlshamn unavailability",
            "source": "EnergiWatch, 2026-07-14",
            "type": "theory",
        }
    ],
    "synthesis": "According to EnergiWatch, low wind output combined with...",
    "confidence": "medium",
    "data_maturity": "provisional — figures may be revised by Energinet",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Ensure tests never leak a real webhook URL from the ambient environment.
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)


async def test_send_slack_alert_skips_when_webhook_not_configured(caplog):
    with caplog.at_level("WARNING"):
        sent = await send_slack_alert(TRIGGER)

    assert sent is False
    assert "SLACK_WEBHOOK_URL not set" in caplog.text


@respx.mock
async def test_send_slack_alert_posts_structured_payload(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    sent = await send_slack_alert(TRIGGER)

    assert sent is True
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["trigger"] == TRIGGER
    assert "price_spike" in body["text"]
    assert "mFRR_capacity" in body["text"]
    # Numbers are rounded and phrased in plain words, not a raw key=value dump.
    assert "value=" not in body["text"]
    assert "4850.00" in body["text"]
    assert "1200.00" in body["text"]
    assert "3600.00" in body["text"]


@respx.mock
async def test_send_slack_alert_returns_false_on_http_error(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500, text="internal error"))

    sent = await send_slack_alert(TRIGGER)

    assert sent is False


async def test_send_event_report_alert_skips_when_webhook_not_configured(caplog):
    with caplog.at_level("WARNING"):
        sent = await send_event_report_alert(EVENT_REPORT)

    assert sent is False
    assert "SLACK_WEBHOOK_URL not set" in caplog.text


@respx.mock
async def test_send_event_report_alert_posts_structured_payload(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    sent = await send_event_report_alert(EVENT_REPORT)

    assert sent is True
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["message_type"] == "event_report"
    assert body["report"] == EVENT_REPORT
    assert "mFRR EAM" in body["text"]
    assert "DK1" in body["text"]
    # The synthesis paragraph is the actual plain-English explanation the
    # whole pipeline exists to produce — it must appear in the visible text,
    # not just be buried in the unrendered `report` dict.
    assert EVENT_REPORT["synthesis"] in body["text"]
    # Hard data correlates are listed as verifiable, cited data points.
    assert "Energinet" in body["text"]
    assert "4,850 DKK/MWh" in body["text"]
    # Market theories must always read as attributed claims, never bare fact.
    assert "according to EnergiWatch" in body["text"]
    assert "low wind + Karlshamn unavailability" in body["text"]
    # Confidence and data maturity are shown plainly.
    assert "medium" in body["text"]
    assert "provisional" in body["text"]


@respx.mock
async def test_send_event_report_alert_marks_correction_in_summary(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    correction = {**EVENT_REPORT, "is_correction": True, "corrects_event_id": "some-original-id"}

    sent = await send_event_report_alert(correction)

    assert sent is True
    body = json.loads(route.calls[0].request.content)
    assert "CORRECTION to an earlier report" in body["text"]
    assert "some-original-id" in body["text"]


@respx.mock
async def test_send_event_report_alert_returns_false_on_http_error(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500, text="internal error"))

    sent = await send_event_report_alert(EVENT_REPORT)

    assert sent is False
