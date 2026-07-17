"""
Slack alerting. `send_slack_alert` (M2-era) renders a raw rule-engine trigger
into a single scannable `mrkdwn` line and is still used for the
`/dashboard/triggers` manual-pull page's underlying formatting and by tests,
but `shared/rule_engine.py:run_rule_engine` no longer calls it automatically
— posting one raw Slack message per fired trigger (most of which never
survive citation validation into a synthesized Event Report) read as spam in
practice. `send_event_report_alert` is now the only automatic Slack push in
the pipeline: it fires once the Intelligence Orchestrator has produced and
citation-validated a full synthesized Event Report (README §2), and is the
"interpretation" the user wants prioritized over raw signal.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Cap on how many `hard_data_correlates` entries get their own bullet line in
# the Slack message. The `synthesis` paragraph (the plain-English
# interpretation) is the whole point of the message and stays uncut; raw
# data references are a supporting citation trail, not the headline, so a
# long `hard_data_correlates` list (some reports carry a dozen+ correlate
# rows) gets capped to the most salient few plus a "+N more" tail instead of
# growing the message indefinitely.
MAX_HARD_DATA_BULLETS = 3


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


async def send_morning_brief_alert(brief_text: str) -> bool:
    """
    Posts one already-rendered Morning Brief (M5,
    `shared.morning_brief_editor.render_for_slack`) to the same Slack
    webhook as the other two senders. Doesn't reuse `send_event_report_alert`'s
    dict-shaped payload -- a brief's `text` is already the fully rendered
    `mrkdwn` a Slack reader wants, so the payload here is simpler: just the
    text plus a `message_type` discriminator, no separate structured
    sub-object the way an Event Report's `report` dict is attached.

    Same missing-webhook precedent as the other two senders: logs a warning
    and returns `False` rather than raising.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set; skipping Slack alert for the Morning Brief")
        return False

    payload = {"text": brief_text, "message_type": "morning_brief"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError:
            logger.exception("Failed to send Slack alert for the Morning Brief")
            return False

    return True


def _format_event_report_summary(report: dict) -> str:
    """
    Renders a synthesized Event Report (README §2) as a readable Slack
    `mrkdwn` news brief rather than a technical data dump.

    The headline is built from market/zone/direction/observation, but the
    real payoff is the `synthesis` paragraph — the LLM-written, citation-
    backed plain-English explanation the whole pipeline exists to produce —
    which previously never made it into the visible Slack `text` (it was
    only present in the unrendered `report` dict attached to the payload).
    Hard data correlates are listed as a verifiable bullet list, and market
    theories are always attributed to their source per the README's
    two-tier trust model (numbers = fact, commentary = "according to …").

    A single well-formatted `mrkdwn` text block is used rather than Block
    Kit `blocks`: the content here is fundamentally one flowing brief
    (headline + paragraph + two short lists + a footer line), which mrkdwn
    renders perfectly well, and a plain `text` field keeps this function
    simple to test and keeps the payload consistent with `send_slack_alert`.
    """
    market = report.get("market")
    zone = report.get("zone")
    direction = report.get("direction")
    observation = report.get("observation")
    synthesis = report.get("synthesis")
    confidence = report.get("confidence")
    data_maturity = report.get("data_maturity")

    lines = []

    if report.get("is_correction"):
        corrects_id = report.get("corrects_event_id", "an earlier report")
        lines.append(f"⚠️ *CORRECTION to an earlier report* (`{corrects_id}`):")
        lines.append("")

    lines.append(f"*{market} · {zone} · {direction}* — {observation}")

    if synthesis:
        lines.append("")
        lines.append(synthesis)

    hard_data = report.get("hard_data_correlates") or []
    if hard_data:
        lines.append("")
        lines.append("*Hard data:*")
        for item in hard_data[:MAX_HARD_DATA_BULLETS]:
            signal = item.get("signal", "signal")
            value = item.get("value")
            source = item.get("source", "unknown source")
            value_part = f": {value}" if value else ""
            lines.append(f"• {signal}{value_part} ({source})")
        remaining = len(hard_data) - MAX_HARD_DATA_BULLETS
        if remaining > 0:
            lines.append(f"• _+{remaining} more data point(s) — see full report_")

    theories = report.get("market_theories") or []
    if theories:
        lines.append("")
        lines.append("*Market commentary:*")
        for theory in theories:
            claim = theory.get("claim", "")
            source = theory.get("source", "unknown source")
            lines.append(f"• according to {source}: {claim}")

    lines.append("")
    lines.append(f"_Confidence: {confidence} · {data_maturity}_")

    return "\n".join(lines)


def _format_summary(trigger: dict) -> str:
    """
    Renders a raw M2 trigger as a single scannable line. Deliberately kept
    lean — this is the fast, pre-LLM signal posted before/independent of
    synthesis (see module docstring), so it stays a quick technical glance
    rather than a news brief, just with numbers rounded and phrased in
    plain words instead of a raw key=value dump.
    """
    value = trigger.get("value")
    baseline = trigger.get("baseline")
    threshold = trigger.get("threshold")
    value_s = f"{value:.2f}" if isinstance(value, int | float) else value
    baseline_s = f"{baseline:.2f}" if isinstance(baseline, int | float) else baseline
    threshold_s = f"{threshold:.2f}" if isinstance(threshold, int | float) else threshold

    return (
        f"*{trigger.get('trigger_type')}* on {trigger.get('market')}/{trigger.get('zone')}/"
        f"{trigger.get('product')}: hit {value_s} vs baseline {baseline_s} "
        f"(threshold {threshold_s}) at {trigger.get('time')} — {trigger.get('details', '')}"
    )
