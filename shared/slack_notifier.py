"""
Slack alerting for raw rule-engine triggers (README §9 M2: "Slack alerting of
raw triggers — no LLM yet — validates signal quality early").

This intentionally does *not* attempt the README §2 "Event Report" contract
(hard-data correlates, market theories, LLM synthesis, confidence) — that's
M4's Intelligence Orchestrator. This module posts the raw, structured trigger
as-is so signal quality can be eyeballed before any synthesis layer exists.
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


def _format_summary(trigger: dict) -> str:
    return (
        f"[{trigger.get('trigger_type')}] {trigger.get('market')}/{trigger.get('zone')}/"
        f"{trigger.get('product')} value={trigger.get('value')} baseline={trigger.get('baseline')} "
        f"threshold={trigger.get('threshold')} at {trigger.get('time')} — "
        f"{trigger.get('details', '')}"
    )
