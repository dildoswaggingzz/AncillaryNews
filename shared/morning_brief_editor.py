"""
Pure composition/rendering layer for the Morning Brief (M5) -- no LLM call
here (every section handed to `compose_brief` has already been synthesized
and validated upstream by `shared/price_recap_synthesizer.py`,
`shared/forecast_synthesizer.py`, and `shared/bess_estimator.py`). This
module's only job is merging those sections into one `MorningBrief` and
rendering it for each delivery channel.

`render_for_slack` mirrors `shared/slack_notifier.py:_format_event_report_summary`'s
condensed-`mrkdwn`-news-brief shape (headline, short paragraph, one line per
subsection) since Slack readers want a scannable summary, not the full
narrative. `render_for_email` is the fuller version: every forecast horizon
in full, the complete causal-factor explanation, and the full glossary
inline (an email reader has more room/patience than a Slack skim).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape as _esc

# BESS/market jargon a non-technical recipient (the mental model this whole
# pipeline is built for -- see the M5 plan's context) may not know, on top
# of whatever `shared/price_recap_synthesizer.py`'s own JARGON_GLOSSARY
# already covers. Merged into the composed brief's glossary, deduped by dict
# key (a term is never redefined twice even if it would otherwise appear in
# both this dict and the recap's).
BESS_JARGON_GLOSSARY = {
    "full cycle equivalent": "A measure of how hard a battery was worked: total energy "
    "discharged divided by its capacity. 1.0 means 'discharged its full capacity once'.",
    "cycle cap": "A limit on how many times per day a battery is allowed to fully "
    "charge/discharge, to protect its long-term health -- set here at 1.5 cycles/day.",
    "arbitrage revenue": "Money earned by charging when power is cheap and discharging when "
    "it's expensive.",
    "capacity reservation revenue": "Money earned just for being available/on standby to help "
    "the grid, whether or not it's ever actually called on.",
}

# Confidence badges used by both render functions and by the dashboard
# template (services/api/templates/morning_brief_detail.html), one canonical
# place for the low/medium/high -> short glyph mapping.
CONFIDENCE_EMOJI = {"low": "🔸", "medium": "🔶", "high": "🔴"}

HORIZON_LABELS = {"month": "Next month", "quarter": "Next quarter", "year": "Next year"}


@dataclass
class MorningBrief:
    """The full composed Morning Brief -- the JSONB `brief` column payload
    persisted by `DatabaseManager.save_morning_brief` (init-db/05-morning-briefs.sql)."""

    brief_date: date
    headline: str
    zone_summaries: list[str]
    causal_factors: list[str]
    # horizon -> {"narrative", "confidence", "swing_factors"} | None
    forecasts: dict[str, dict | None]
    bess_estimates: list[dict]
    jargon_glossary: dict[str, str] = field(default_factory=dict)


def compose_brief(
    brief_date: date,
    price_recap: dict,
    forecasts: dict[str, dict | None],
    bess_estimates: list[dict],
) -> MorningBrief:
    """
    Pure merge of the three already-synthesized/validated sections into one
    `MorningBrief`. `forecasts` maps horizon ("month"/"quarter"/"year") to
    either the forecast content dict (`{narrative, confidence,
    swing_factors, horizon}`, as produced by
    `shared.forecast_synthesizer.synthesize_forecast`) or `None` if that
    horizon's synthesis+cache-fallback both came up empty -- callers render
    that as an honest "forecast unavailable" rather than omitting the
    section.

    Glossary dedup: `price_recap`'s own `jargon_glossary` and this module's
    `BESS_JARGON_GLOSSARY` are merged into one dict, so a term defined in
    both places (dict keys are the dedup mechanism) is never shown twice.
    """
    glossary = {**BESS_JARGON_GLOSSARY, **(price_recap.get("jargon_glossary") or {})}

    return MorningBrief(
        brief_date=brief_date,
        headline=price_recap.get("headline", ""),
        zone_summaries=list(price_recap.get("zone_summaries") or []),
        causal_factors=list(price_recap.get("causal_factors") or []),
        forecasts=dict(forecasts),
        bess_estimates=list(bess_estimates),
        jargon_glossary=glossary,
    )


def _bess_line(estimate: dict) -> str:
    cap_note = (
        " (cycle cap limited earnings some days)" if estimate.get("cycle_cap_was_binding") else ""
    )
    afrr_eur = estimate.get("total_afrr_activation_revenue_eur", 0)
    # Deliberately kept as a separate clause, not summed into the DKK figure
    # above -- consistent with the "never summed" product decision (mixing
    # EUR into a DKK total would misstate it). Omitted entirely when zero
    # (e.g. no aFRR_capacity committed, or a DK1 run) rather than showing a
    # noisy "+0 EUR".
    afrr_note = f"; +{afrr_eur:,.0f} EUR aFRR activation" if afrr_eur else ""
    return (
        f"{estimate.get('config_label')} in {estimate.get('zone')}: "
        f"{estimate.get('total_revenue_dkk', 0):,.0f} DKK over the window "
        f"({estimate.get('full_cycle_equivalents', 0):.2f} full cycle equivalents)"
        f"{cap_note}{afrr_note}"
    )


def render_for_slack(brief: MorningBrief) -> str:
    """
    Condensed `mrkdwn` brief for Slack -- mirrors
    `shared/slack_notifier.py:_format_event_report_summary`'s shape:
    headline, short recap paragraph, one line per forecast horizon with a
    confidence tag, one line per BESS estimate. The glossary is deliberately
    dropped here (linked out, not inlined) -- a Slack skim isn't the place
    for a full jargon dictionary.
    """
    lines = [f"*Morning Brief — {brief.brief_date}*", "", brief.headline]

    if brief.zone_summaries:
        lines.append("")
        lines.append("*Yesterday:*")
        for line in brief.zone_summaries:
            lines.append(f"• {line}")

    if brief.causal_factors:
        lines.append("")
        for factor in brief.causal_factors:
            lines.append(f"_{factor}_")

    lines.append("")
    lines.append("*Outlook:*")
    for horizon in ("month", "quarter", "year"):
        forecast = brief.forecasts.get(horizon)
        label = HORIZON_LABELS.get(horizon, horizon)
        if forecast is None:
            lines.append(f"• {label}: _forecast unavailable_")
            continue
        confidence = forecast.get("confidence", "low")
        badge = CONFIDENCE_EMOJI.get(confidence, "")
        lines.append(f"• {label} {badge} ({confidence}): {forecast.get('narrative', '')}")

    if brief.bess_estimates:
        lines.append("")
        lines.append("*Illustrative BESS estimates (past month, ~1.5 cycles/day cap):*")
        for estimate in brief.bess_estimates:
            lines.append(f"• {_bess_line(estimate)}")

    lines.append("")
    lines.append("_Full brief with glossary and forecast details: see the dashboard._")

    return "\n".join(lines)


def render_for_email(brief: MorningBrief) -> tuple[str, str, str]:
    """
    Fuller narrative for email: `(subject, html_body, plaintext_body)`.
    Unlike `render_for_slack`, every forecast horizon is shown in full
    (narrative + all swing factors), the complete causal-factor explanation
    is included, and the full jargon glossary is inlined.
    """
    subject = f"AncillaryNews Morning Brief — {brief.brief_date}"

    def _forecast_block(horizon: str) -> tuple[str, str]:
        forecast = brief.forecasts.get(horizon)
        label = HORIZON_LABELS.get(horizon, horizon)
        if forecast is None:
            return (
                f"<h3>{label}</h3><p>Forecast unavailable.</p>",
                f"{label}: forecast unavailable.\n",
            )
        confidence = forecast.get("confidence", "low")
        swing_factors = forecast.get("swing_factors") or []
        html = (
            f"<h3>{_esc(label)} (confidence: {_esc(confidence)})</h3>"
            f"<p>{_esc(forecast.get('narrative', ''))}</p>"
            f"<p><em>Swing factors: {_esc(', '.join(swing_factors))}</em></p>"
        )
        text = (
            f"{label} (confidence: {confidence}):\n{forecast.get('narrative', '')}\n"
            f"Swing factors: {', '.join(swing_factors)}\n"
        )
        return html, text

    # Every string below originates from LLM synthesis (or, for the
    # glossary, price_recap_synthesizer's LLM-adjacent output) and is
    # untrusted -- unlike the dashboard templates, which get Jinja2's
    # autoescaping for free, this f-string-built body must escape by hand.
    zone_html = "".join(f"<li>{_esc(line)}</li>" for line in brief.zone_summaries)
    zone_text = "\n".join(f"- {line}" for line in brief.zone_summaries)

    causal_html = "".join(f"<p>{_esc(factor)}</p>" for factor in brief.causal_factors)
    causal_text = "\n".join(brief.causal_factors)

    bess_html = "".join(f"<li>{_esc(_bess_line(e))}</li>" for e in brief.bess_estimates)
    bess_text = "\n".join(f"- {_bess_line(e)}" for e in brief.bess_estimates)

    glossary_html = "".join(
        f"<dt><strong>{_esc(term)}</strong></dt><dd>{_esc(definition)}</dd>"
        for term, definition in sorted(brief.jargon_glossary.items())
    )
    glossary_text = "\n".join(
        f"{term}: {definition}" for term, definition in sorted(brief.jargon_glossary.items())
    )

    forecast_htmls, forecast_texts = [], []
    for horizon in ("month", "quarter", "year"):
        h, t = _forecast_block(horizon)
        forecast_htmls.append(h)
        forecast_texts.append(t)

    html_body = f"""\
<html><body>
<h1>Morning Brief — {_esc(str(brief.brief_date))}</h1>
<h2>{_esc(brief.headline)}</h2>
<h3>Yesterday</h3>
<ul>{zone_html}</ul>
{causal_html}
<h2>Outlook</h2>
{"".join(forecast_htmls)}
<h2>Illustrative BESS estimates (past month, ~1.5 cycles/day cap)</h2>
<ul>{bess_html}</ul>
<h2>Glossary</h2>
<dl>{glossary_html}</dl>
</body></html>
"""

    plaintext_body = f"""\
Morning Brief — {brief.brief_date}

{brief.headline}

Yesterday:
{zone_text}

{causal_text}

Outlook:
{"".join(forecast_texts)}

Illustrative BESS estimates (past month, ~1.5 cycles/day cap):
{bess_text}

Glossary:
{glossary_text}
"""

    return subject, html_body, plaintext_body
