"""
Slack alerting for raw rule-engine triggers (README §9 M2: "Slack alerting of
raw triggers — no LLM yet — validates signal quality early") and, since M4,
for the full synthesized Event Report (README §2) once the Intelligence
Orchestrator has produced and validated one.

`send_slack_alert` posts the raw, structured trigger as-is (unchanged since
M2) so signal quality can still be eyeballed independently of whether
synthesis succeeds — the orchestrator's synthesis is an enrichment on top of
this, not a replacement for it (see `shared/rule_engine.py`).
`send_event_report_alert` posts the richer, synthesized Event Report as a
second, distinct message type once it exists.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def send_slack_alert(trigger: dict) -> bool:
    """
    Posts one trigger to the Slack webhook configured via `SLACK_WEBHOOK_URL`.

    If the env var isn't set (true for most dev/CI environments today — it's
    still a placeholder in .env.example), logs a warning and returns False
    rather than raising, so a missing webhook never breaks the ingestion
    cycle that calls this.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning(
            "SLACK_WEBHOOK_URL not set; skipping Slack alert for trigger_type=%s "
            "market=%s zone=%s product=%s",
            trigger.get("trigger_type"),
            trigger.get("market"),
            trigger.get("zone"),
            trigger.get("product"),
        )
        return False

    payload = {"text": _format_summary(trigger), "trigger": trigger}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception(
                "Failed to send Slack alert for trigger_type=%s", trigger.get("trigger_type")
            )
            return False

    return True


async def send_event_report_alert(report: dict) -> bool:
    """
    Posts one synthesized, citation-validated Event Report (README §2) to
    the same Slack webhook as `send_slack_alert`, distinguished by a
    `"message_type": "event_report"` field on the payload so downstream
    consumers (or a human skimming Slack) can tell it apart from a raw
    trigger.

    Same missing-webhook precedent as `send_slack_alert`: logs a warning and
    returns False rather than raising.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning(
            "SLACK_WEBHOOK_URL not set; skipping Slack alert for event_id=%s",
            report.get("event_id"),
        )
        return False

    payload = {
        "text": _format_event_report_summary(report),
        "message_type": "event_report",
        "report": report,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Failed to send Slack alert for event_id=%s", report.get("event_id"))
            return False

    return True


def _format_event_report_summary(report: dict) -> str:
    is_correction = report.get("is_correction")
    prefix = ":warning: CORRECTION" if is_correction else ":rotating_light: Event Report"
    return (
        f"{prefix} [{report.get('market')}/{report.get('zone')}/{report.get('direction')}] "
        f"{report.get('observation')} (confidence={report.get('confidence')}, "
        f"data_maturity={report.get('data_maturity')})"
    )


def _format_summary(trigger: dict) -> str:
    return (
        f"[{trigger.get('trigger_type')}] {trigger.get('market')}/{trigger.get('zone')}/"
        f"{trigger.get('product')} value={trigger.get('value')} baseline={trigger.get('baseline')} "
        f"threshold={trigger.get('threshold')} at {trigger.get('time')} — "
        f"{trigger.get('details', '')}"
    )
