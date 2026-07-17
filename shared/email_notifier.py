"""
Email delivery for the Morning Brief (M5) -- mirrors
`shared/slack_notifier.py`'s resilience contract exactly: `.env.example`
placeholder-only by default (unconfigured in most dev/CI environments), logs
a warning and returns `False` rather than raising when unconfigured, and
never raises on a send failure either (catches and logs). This keeps
`services/orchestrator/main.py:run_morning_brief`'s "one channel failing
never blocks the other" contract trivial -- both `send_morning_brief_email`
and `shared.slack_notifier.send_morning_brief_alert` share the exact same
`bool` return-value contract.

Uses `smtplib` (stdlib, synchronous) via `asyncio.to_thread` to keep this
module's public function `async def` like every other notifier in this repo,
without blocking the event loop on the actual SMTP conversation.
"""

import asyncio
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _send_sync(
    subject: str,
    html_body: str,
    plaintext_body: str | None,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str | None,
    smtp_password: str | None,
    smtp_from: str,
    smtp_to: str,
) -> None:
    """
    The blocking SMTP conversation, run off the event loop via
    `asyncio.to_thread` by `send_morning_brief_email` below. Raises on
    failure -- the caller is responsible for catching and logging (same
    split as `httpx`-based notifiers catching `httpx.HTTPError` at the call
    site rather than inside a sync helper).
    """
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = smtp_from
    message["To"] = smtp_to
    if plaintext_body:
        message.attach(MIMEText(plaintext_body, "plain"))
    message.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
        server.ehlo()
        if server.has_extn("STARTTLS"):
            server.starttls()
            server.ehlo()
        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)
        server.sendmail(smtp_from, [smtp_to], message.as_string())


async def send_morning_brief_email(
    subject: str, html_body: str, plaintext_body: str | None = None
) -> bool:
    """
    Sends one Morning Brief email via the SMTP settings configured through
    `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`SMTP_FROM`/
    `MORNING_BRIEF_EMAIL_TO` (see `.env.example`).

    If `SMTP_HOST` or `MORNING_BRIEF_EMAIL_TO` isn't set, logs a warning and
    returns `False` rather than raising -- same missing-config precedent as
    `shared/slack_notifier.py`'s missing-webhook handling, so an
    unconfigured email channel never breaks the Morning Brief pipeline (the
    Slack channel, if configured, still gets its own independent delivery
    attempt).
    """
    smtp_host = os.getenv("SMTP_HOST")
    smtp_to = os.getenv("MORNING_BRIEF_EMAIL_TO")
    if not smtp_host or not smtp_to:
        logger.warning(
            "SMTP_HOST/MORNING_BRIEF_EMAIL_TO not set; skipping Morning Brief email for subject=%r",
            subject,
        )
        return False

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    smtp_from = os.getenv("SMTP_FROM") or smtp_user or "noreply@ancillarynews.local"

    try:
        await asyncio.to_thread(
            _send_sync,
            subject,
            html_body,
            plaintext_body,
            smtp_host,
            smtp_port,
            smtp_user,
            smtp_password,
            smtp_from,
            smtp_to,
        )
    except Exception:
        logger.exception("Failed to send Morning Brief email for subject=%r", subject)
        return False

    return True
