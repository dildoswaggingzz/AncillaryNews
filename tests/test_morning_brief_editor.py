from datetime import date

from shared.morning_brief_editor import (
    BESS_JARGON_GLOSSARY,
    MorningBrief,
    _bess_line,
    compose_brief,
    render_for_email,
    render_for_slack,
)

BRIEF_DATE = date(2026, 7, 17)

PRICE_RECAP = {
    "headline": "Prices were mild across DK1/DK2 yesterday.",
    "zone_summaries": [
        "DK1: day-ahead averaged 450.0 DKK/MWh.",
        "DK2: day-ahead averaged 460.0 DKK/MWh.",
    ],
    "causal_factors": [
        "Wind output was 20% above the trailing 30-day average, pulling prices down."
    ],
    "jargon_glossary": {"day-ahead price": "The price for next-day delivery."},
}

FORECASTS = {
    "month": {
        "narrative": "Prices are likely to stay roughly flat next month.",
        "confidence": "medium",
        "swing_factors": ["wind output"],
        "horizon": "month",
    },
    "quarter": None,
    "year": {
        "narrative": "Prices could trend upward over the next year, driven by gas.",
        "confidence": "low",
        "swing_factors": ["gas prices", "interconnector capacity"],
        "horizon": "year",
    },
}

# P5 (current) shape: every summary from shared.bess_estimator.run_illustrative_backtests
# now carries BOTH an achievable (causal lag-24h forecast) headline and a
# labelled perfect-foresight theoretical ceiling -- see that module's
# docstring. This is the fixture that reflects what the pipeline actually
# produces today.
BESS_ESTIMATES = [
    {
        "config_label": "Small commercial (1 MW / 2 MWh)",
        "zone": "DK1",
        "achievable_run_id": 101,
        "ceiling_run_id": 201,
        "total_revenue_dkk_achievable": 9000.0,
        "total_revenue_dkk_ceiling": 15000.0,
        "total_revenue_all_dkk_achievable": 12345.6,
        "total_revenue_all_dkk_ceiling": 20000.0,
        "total_revenue_all_eur_achievable": 1654.0,
        "total_revenue_all_eur_ceiling": 2681.0,
        "full_cycle_equivalents_achievable": 20.0,
        "cycle_cap_was_binding_achievable": True,
        "total_afrr_activation_revenue_eur_achievable": 0.0,
        "total_capacity_revenue_eur_achievable": 0.0,
        "currencies_present": ["DKK"],
        "zero_price_periods_by_leg": {},
    },
    {
        "config_label": "Utility-scale (10 MW / 40 MWh)",
        "zone": "DK2",
        "achievable_run_id": 102,
        "ceiling_run_id": 202,
        "total_revenue_dkk_achievable": 70000.0,
        "total_revenue_dkk_ceiling": 110000.0,
        "total_revenue_all_dkk_achievable": 98765.4,
        "total_revenue_all_dkk_ceiling": 150000.0,
        "total_revenue_all_eur_achievable": 13240.0,
        "total_revenue_all_eur_ceiling": 20107.0,
        "full_cycle_equivalents_achievable": 18.5,
        "cycle_cap_was_binding_achievable": False,
        "total_afrr_activation_revenue_eur_achievable": 0.0,
        "total_capacity_revenue_eur_achievable": 0.0,
        "currencies_present": ["DKK"],
        "zero_price_periods_by_leg": {},
    },
]

# Pre-P5 shape: what's actually sitting in `bess_simulation_runs`/persisted
# morning briefs from before this migration -- a single threshold-engine
# total, no achievable/ceiling keys at all. `_bess_line` must still render
# this correctly (see the backward-compat tests below) rather than crashing
# or silently showing zeroes.
OLD_SHAPE_BESS_ESTIMATES = [
    {
        "config_label": "Small commercial (1 MW / 2 MWh)",
        "zone": "DK1",
        "run_id": 101,
        "total_revenue_dkk": 12345.6,
        "total_arbitrage_revenue_dkk": 10000.0,
        "total_capacity_revenue_dkk": 2345.6,
        "full_cycle_equivalents": 20.0,
        "cycle_cap_was_binding": True,
    },
    {
        "config_label": "Utility-scale (10 MW / 40 MWh)",
        "zone": "DK2",
        "run_id": 102,
        "total_revenue_dkk": 98765.4,
        "total_arbitrage_revenue_dkk": 80000.0,
        "total_capacity_revenue_dkk": 18765.4,
        "full_cycle_equivalents": 18.5,
        "cycle_cap_was_binding": False,
    },
]


def test_compose_brief_builds_morning_brief():
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, BESS_ESTIMATES)

    assert isinstance(brief, MorningBrief)
    assert brief.brief_date == BRIEF_DATE
    assert brief.headline == PRICE_RECAP["headline"]
    assert brief.zone_summaries == PRICE_RECAP["zone_summaries"]
    assert brief.causal_factors == PRICE_RECAP["causal_factors"]
    assert brief.forecasts == FORECASTS
    assert brief.bess_estimates == BESS_ESTIMATES


def test_compose_brief_dedupes_jargon_glossary_across_sections():
    recap_with_overlap = {
        **PRICE_RECAP,
        "jargon_glossary": {
            **PRICE_RECAP["jargon_glossary"],
            "cycle cap": "Recap's own (should NOT override the canonical BESS glossary entry).",
        },
    }

    brief = compose_brief(BRIEF_DATE, recap_with_overlap, FORECASTS, BESS_ESTIMATES)

    # Every term appears exactly once as a dict key -- no duplicate/repeated definitions.
    assert list(brief.jargon_glossary.keys()) == list(dict(brief.jargon_glossary).keys())
    assert "day-ahead price" in brief.jargon_glossary
    assert "cycle cap" in brief.jargon_glossary
    assert "full cycle equivalent" in brief.jargon_glossary
    # The recap's own overlapping definition wins (merged last).
    assert brief.jargon_glossary["cycle cap"] == recap_with_overlap["jargon_glossary"]["cycle cap"]


def test_compose_brief_handles_missing_forecast_horizon_as_none():
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, BESS_ESTIMATES)
    assert brief.forecasts["quarter"] is None


def test_render_for_slack_returns_condensed_mrkdwn():
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, BESS_ESTIMATES)

    text = render_for_slack(brief)

    assert str(BRIEF_DATE) in text
    assert PRICE_RECAP["headline"] in text
    assert "DK1: day-ahead averaged 450.0 DKK/MWh." in text
    assert "Wind output was 20% above" in text
    assert "Small commercial (1 MW / 2 MWh)" in text
    assert "Utility-scale (10 MW / 40 MWh)" in text
    # Quarter forecast is unavailable -- rendered honestly, not omitted.
    assert "forecast unavailable" in text
    # Confidence tags present for available horizons.
    assert "medium" in text
    assert "low" in text
    # Both the achievable headline and the labelled theoretical ceiling
    # appear -- the threshold engine's single number is retired.
    assert "achievable" in text
    assert "theoretical ceiling, not achievable" in text
    # Glossary is dropped/linked-out in the Slack rendering, not inlined --
    # a term's *definition* never appears, even though the BESS estimate
    # lines themselves legitimately use jargon terms like "full cycle
    # equivalents" without spelling out what they mean.
    assert "The price for next-day delivery." not in text
    assert "A measure of how hard a battery was worked" not in text


def test_render_for_email_returns_subject_html_and_plaintext():
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, BESS_ESTIMATES)

    subject, html_body, plaintext_body = render_for_email(brief)

    assert str(BRIEF_DATE) in subject
    for body in (html_body, plaintext_body):
        assert PRICE_RECAP["headline"] in body
        assert "Wind output was 20% above" in body
        assert "Small commercial (1 MW / 2 MWh)" in body
        assert "Utility-scale (10 MW / 40 MWh)" in body
        # Full glossary is inlined in the email (unlike Slack) -- including
        # the new achievable/theoretical-ceiling entries.
        assert "day-ahead price" in body
        assert "cycle cap" in body
        assert "achievable" in body
        assert "theoretical ceiling" in body
        # All three forecast horizons are shown, including the unavailable one.
        assert "Next month" in body
        assert "Next quarter" in body
        assert "Next year" in body
        assert "gas prices" in body

    assert "<html>" in html_body
    assert "<html>" not in plaintext_body


def test_render_for_email_escapes_untrusted_llm_output_in_html():
    # headline/causal_factors/glossary all originate from LLM synthesis and
    # must be treated as untrusted -- render_for_email builds the HTML body
    # via plain f-strings (no Jinja autoescaping), so it must escape by hand.
    malicious_recap = {
        **PRICE_RECAP,
        "headline": "<script>alert(1)</script>",
        "causal_factors": ["<img src=x onerror=alert(2)>"],
        "jargon_glossary": {"<b>term</b>": "<i>definition</i>"},
    }
    brief = compose_brief(BRIEF_DATE, malicious_recap, FORECASTS, BESS_ESTIMATES)

    _subject, html_body, plaintext_body = render_for_email(brief)

    assert "<script>" not in html_body
    assert "<img src=x" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "&lt;img src=x onerror=alert(2)&gt;" in html_body
    # Plaintext body is not HTML and needs no escaping.
    assert "<script>alert(1)</script>" in plaintext_body


def test_bess_jargon_glossary_covers_core_terms():
    assert "full cycle equivalent" in BESS_JARGON_GLOSSARY
    assert "cycle cap" in BESS_JARGON_GLOSSARY
    # P5: these are the two terms that make the achievable/ceiling framing
    # understandable to a non-technical recipient.
    assert "achievable" in BESS_JARGON_GLOSSARY
    assert "theoretical ceiling" in BESS_JARGON_GLOSSARY


# --- P5: achievable headline + labelled theoretical ceiling -----------------


def test_bess_line_renders_achievable_and_ceiling():
    line = _bess_line(BESS_ESTIMATES[0])

    assert "12,346 DKK achievable" in line
    assert "20,000 DKK with perfect foresight" in line
    assert "theoretical ceiling, not achievable" in line
    assert "simple day-ahead persistence forecast" in line
    # Cycle-cap detail is sourced from the achievable run only.
    assert "(cycle cap limited earnings some days)" in line


def test_bess_line_no_cycle_cap_note_when_achievable_run_not_binding():
    line = _bess_line(BESS_ESTIMATES[1])

    assert "cycle cap limited earnings" not in line


# --- Backward compat: pre-P5 persisted briefs (old summary shape) ----------


def test_bess_line_backward_compat_with_old_shape_estimate():
    # Old-shape estimates (no achievable/ceiling keys) must still render via
    # the old single-number format, not crash or show blank/zero fields.
    line = _bess_line(OLD_SHAPE_BESS_ESTIMATES[0])

    assert "12,346 DKK over the window" in line
    assert "(20.00 full cycle equivalents)" in line
    assert "(cycle cap limited earnings some days)" in line
    assert "achievable" not in line
    assert "theoretical ceiling" not in line


def test_render_for_slack_backward_compat_with_old_shape_brief():
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, OLD_SHAPE_BESS_ESTIMATES)

    text = render_for_slack(brief)

    assert "Small commercial (1 MW / 2 MWh)" in text
    # The per-estimate line itself (not the section header, which always
    # frames the section generically) must use the old single-number
    # rendering, not the achievable/ceiling wording.
    assert "12,346 DKK over the window (20.00 full cycle equivalents)" in text


def test_render_for_email_backward_compat_with_old_shape_brief():
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, OLD_SHAPE_BESS_ESTIMATES)

    _subject, html_body, plaintext_body = render_for_email(brief)

    for body in (html_body, plaintext_body):
        assert "Small commercial (1 MW / 2 MWh)" in body
        assert "12,346 DKK over the window" in body


# --- aFRR activation EUR clause (never summed into the DKK figure) ---------


def test_bess_line_includes_eur_clause_when_activation_revenue_nonzero():
    estimates_with_activation = [
        {**BESS_ESTIMATES[0], "total_afrr_activation_revenue_eur_achievable": 1234.0},
        BESS_ESTIMATES[1],
    ]
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, estimates_with_activation)

    slack_text = render_for_slack(brief)
    _subject, html_body, plaintext_body = render_for_email(brief)

    for body in (slack_text, html_body, plaintext_body):
        assert "1,234 EUR aFRR activation" in body


def test_bess_line_omits_eur_clause_when_activation_revenue_zero():
    estimates_without_activation = [
        {**BESS_ESTIMATES[0], "total_afrr_activation_revenue_eur_achievable": 0.0},
        BESS_ESTIMATES[1],
    ]
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, estimates_without_activation)

    slack_text = render_for_slack(brief)
    _subject, html_body, plaintext_body = render_for_email(brief)

    for body in (slack_text, html_body, plaintext_body):
        assert "EUR aFRR activation" not in body


def test_bess_line_omits_eur_clause_when_field_missing():
    # Estimates predating this feature (or from a DK1 run with no
    # aFRR_capacity commitment) may simply lack the key -- handled the same
    # as an explicit 0.0, not a crash.
    brief = compose_brief(BRIEF_DATE, PRICE_RECAP, FORECASTS, BESS_ESTIMATES)

    slack_text = render_for_slack(brief)

    assert "EUR aFRR activation" not in slack_text
