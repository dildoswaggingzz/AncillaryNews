from unittest.mock import MagicMock, patch

import pytest

from shared.email_notifier import send_morning_brief_email

SUBJECT = "AncillaryNews Morning Brief - 2026-07-17"
HTML_BODY = "<html><body><h1>Morning Brief</h1></body></html>"
PLAINTEXT_BODY = "Morning Brief\n\nPrices were mild."


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Ensure tests never leak real SMTP config from the ambient environment.
    smtp_vars = (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "MORNING_BRIEF_EMAIL_TO",
    )
    for var in smtp_vars:
        monkeypatch.delenv(var, raising=False)


async def test_send_morning_brief_email_skips_when_smtp_host_not_configured(caplog):
    with caplog.at_level("WARNING"):
        sent = await send_morning_brief_email(SUBJECT, HTML_BODY, PLAINTEXT_BODY)

    assert sent is False
    assert "SMTP_HOST" in caplog.text


async def test_send_morning_brief_email_skips_when_recipient_not_configured(monkeypatch, caplog):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    # MORNING_BRIEF_EMAIL_TO deliberately left unset.

    with caplog.at_level("WARNING"):
        sent = await send_morning_brief_email(SUBJECT, HTML_BODY, PLAINTEXT_BODY)

    assert sent is False


async def test_send_morning_brief_email_sends_via_smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "s3cret")
    monkeypatch.setenv("SMTP_FROM", "brief@example.com")
    monkeypatch.setenv("MORNING_BRIEF_EMAIL_TO", "operator@example.com")

    mock_server = MagicMock()
    mock_server.has_extn.return_value = True
    mock_smtp_cls = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    with patch("shared.email_notifier.smtplib.SMTP", mock_smtp_cls):
        sent = await send_morning_brief_email(SUBJECT, HTML_BODY, PLAINTEXT_BODY)

    assert sent is True
    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@example.com", "s3cret")
    mock_server.sendmail.assert_called_once()
    args = mock_server.sendmail.call_args.args
    assert args[0] == "brief@example.com"
    assert args[1] == ["operator@example.com"]
    assert SUBJECT in args[2]


async def test_send_morning_brief_email_returns_false_on_smtp_failure(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MORNING_BRIEF_EMAIL_TO", "operator@example.com")

    mock_smtp_cls = MagicMock(side_effect=OSError("connection refused"))

    with patch("shared.email_notifier.smtplib.SMTP", mock_smtp_cls):
        sent = await send_morning_brief_email(SUBJECT, HTML_BODY, PLAINTEXT_BODY)

    assert sent is False


async def test_send_morning_brief_email_works_without_auth_credentials(monkeypatch):
    # No SMTP_USER/SMTP_PASSWORD set -- an open relay / local dev SMTP server.
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MORNING_BRIEF_EMAIL_TO", "operator@example.com")

    mock_server = MagicMock()
    mock_server.has_extn.return_value = False
    mock_smtp_cls = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    with patch("shared.email_notifier.smtplib.SMTP", mock_smtp_cls):
        sent = await send_morning_brief_email(SUBJECT, HTML_BODY, PLAINTEXT_BODY)

    assert sent is True
    mock_server.starttls.assert_not_called()
    mock_server.login.assert_not_called()
