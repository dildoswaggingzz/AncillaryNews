import json

import httpx
import pytest
import respx

from shared.slack_notifier import send_slack_alert

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


@respx.mock
async def test_send_slack_alert_returns_false_on_http_error(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK_URL)
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500, text="internal error"))

    sent = await send_slack_alert(TRIGGER)

    assert sent is False
