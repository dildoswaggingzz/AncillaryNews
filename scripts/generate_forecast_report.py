#!/usr/bin/env python
"""
Generates `docs/forecast-model-results.md` (M6 P3, `docs/forecast-model-design.md`
§6) -- the quantile-LightGBM model's pinball loss against the strongest P2
baseline, on the trailing-12-month headline window, from the live database.
Mirrors `scripts/generate_baseline_report.py`'s structure and constants
deliberately, so the two reports describe the exact same window.

Usage (needs `DATABASE_URL` pointed at a reachable Postgres/TimescaleDB
instance):

    DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \\
        poetry run python scripts/generate_forecast_report.py

**Why the baselines are recomputed here, not just read from the committed
`docs/forecast-baseline-results.md`:** that document's folds were generated
against `end = datetime.now(UTC)` at ITS run time; this script's folds use
`end = datetime.now(UTC)` at ITS OWN run time, which is a different instant.
`walk_forward_folds`'s fold boundaries are anchored to `end`, so two runs
minutes or days apart do not necessarily produce byte-identical fold windows.
Rather than compare the model against a document that may describe a very
slightly different window, this script reuses `shared.baselines.
walk_forward_folds`/`fit_seasonal_naive`/`fit_conditional_climatology_rolling`/
`run_walk_forward` to recompute B1 (both lags) and B2-rolling on EXACTLY the
same fold list the model is evaluated on, in the same run. `COMMON_WINDOW_START`
below is still the same declared constant as
`scripts/generate_baseline_report.py` (2021-12-31, `down`'s true first day --
see that script's module docstring), so both scripts describe the same market
window even though the exact fold boundaries can drift by a run's worth of
wall-clock time.

**The bar is `min(B1 t-24h, B1 t-168h, B2-rolling)`, per (product, tau)** --
design §0/§6: "the strongest baseline... which is B2-rolling almost
everywhere, NOT B1", formalised there as `min(B1, B2-rolling)`. B2-expanding
is deliberately excluded, not only from the bar but from this script's
recomputation entirely (P2's own results doc: an expanding climatology
trained across FCR-D DK2's ~92% price collapse is a strawman, not a hard
baseline, and this script's bounded fetch window -- see below -- would make
an "expanding" fit here mean something different from P2's full-history one
anyway). `docs/forecast-baseline-results.md` remains the source for
B2-expanding's own numbers, unchanged.

**Feature fetch window is bounded; target fetch window is NOT, and the two
differ deliberately.** The model's own training windows (design §3's bounded
trailing lookback, 12mo/18mo) never look further back than `max(LOOKBACKS.
values())` before the earliest headline fold's own boundary, so there is no
reason to build FEATURES over 4.6 years of history to evaluate a 12-month
headline window with an 18-month lookback ceiling -- `fetch_start` below is
computed from exactly that bound, and `build_features` is only ever called
over `[fetch_start, end]`. The TARGET series is different: B1 (`shared.
baselines.fit_seasonal_naive`) is an *expanding* baseline whose residual
quantiles are fit over a fold's WHOLE `[train_start, train_end)`, and every
fold's `train_start` is `COMMON_WINDOW_START` (2021-12-31) by `walk_forward_
folds`'s own construction -- so recomputing B1 faithfully (matching P2's own
definition, not a silently-truncated one) needs the target series fetched
over the FULL common window, not `fetch_start`. `target_series_full` (fitted
against by every baseline) and the `fetch_start`-bounded slice of it used for
the model's own dataset are both derived from one fetch, below. The §5
coverage gate (reused, unmodified) runs over that same full common window,
matching `scripts/generate_baseline_report.py` exactly.
"""

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.baselines import (  # noqa: E402
    QUANTILES,
    SEASONAL_NAIVE_LAGS,
    TARGET_MARKET,
    TARGET_ZONE,
    WalkForwardResult,
    fetch_and_assert_daily_coverage,
    fetch_target_series,
    fit_conditional_climatology_rolling,
    fit_seasonal_naive,
    run_walk_forward,
    trailing_folds,
    walk_forward_folds,
)
from shared.db_manager import DatabaseManager  # noqa: E402
from shared.feature_store import build_features  # noqa: E402
from shared.forecast_model import (  # noqa: E402
    FEATURE_HORIZON,
    LOOKBACKS,
    ForecastModelConfig,
    join_features_and_target,
    run_model_walk_forward,
)
from shared.logging_config import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

# Identical to scripts/generate_baseline_report.py's own constant -- see that
# script's module docstring for why (`down`'s true first day, verified live).
COMMON_WINDOW_START = datetime(2021, 12, 31, tzinfo=UTC)

HEADLINE_SPAN = timedelta(days=365)

PRODUCTS: tuple[str, ...] = ("up", "down")

# Baselines contributing to `beats_bar` (design §0/§6: "min(B1, B2-rolling)",
# B2-expanding excluded as a strawman -- see module docstring).
BAR_BASELINE_NAMES: tuple[str, ...] = (
    "B1 seasonal-naive (t-24h)",
    "B1 seasonal-naive (t-168h)",
    "B2 conditional climatology (rolling 180d)",
)


def _baseline_configs():
    configs = {}
    for lag_name, lag in SEASONAL_NAIVE_LAGS.items():
        configs[f"B1 seasonal-naive ({lag_name})"] = lambda s, a, b, lag=lag: fit_seasonal_naive(
            s, a, b, lag
        )
    configs["B2 conditional climatology (rolling 180d)"] = fit_conditional_climatology_rolling
    return configs


def _format_headline_table(
    baseline_results: dict[str, dict[str, WalkForwardResult]],
    model_results: dict[str, dict[str, dict[str, WalkForwardResult]]],
) -> str:
    """
    One row per (product, tau, lookback): model pinball loss, the winning
    bar baseline's name and loss, and `beats_bar`.
    """
    lines = [
        "| product | τ | lookback | model pinball | bar baseline | bar pinball | beats_bar |",
        "|---|---|---|---|---|---|---|",
    ]
    for product in PRODUCTS:
        for tau in QUANTILES:
            bar_name, bar_loss = min(
                (
                    (name, baseline_results[name][product].per_quantile_loss[tau])
                    for name in BAR_BASELINE_NAMES
                ),
                key=lambda item: item[1],
            )
            for lookback_name in LOOKBACKS:
                model_loss = model_results[lookback_name][product].per_quantile_loss[tau]
                beats = model_loss < bar_loss
                lines.append(
                    f"| {product} | {tau} | {lookback_name} | {model_loss:.4f} | {bar_name} "
                    f"| {bar_loss:.4f} | {'yes' if beats else 'no'} |"
                )
    return "\n".join(lines)


def _format_full_baseline_table(baseline_results: dict[str, dict[str, WalkForwardResult]]) -> str:
    lines = [
        "| baseline | product | " + " | ".join(f"τ={tau}" for tau in QUANTILES) + " | fold count |",
        "|---" * (3 + len(QUANTILES)) + "|",
    ]
    for name, per_product in baseline_results.items():
        for product, result in per_product.items():
            cells = " | ".join(f"{result.per_quantile_loss[tau]:.4f}" for tau in QUANTILES)
            lines.append(f"| {name} | {product} | {cells} | {result.fold_count} |")
    return "\n".join(lines)


def _format_model_table(
    model_results: dict[str, dict[str, WalkForwardResult]],
) -> str:
    lines = [
        "| lookback | product | " + " | ".join(f"τ={tau}" for tau in QUANTILES) + " | fold count |",
        "|---" * (3 + len(QUANTILES)) + "|",
    ]
    for lookback_name, per_product in model_results.items():
        for product, result in per_product.items():
            cells = " | ".join(f"{result.per_quantile_loss[tau]:.4f}" for tau in QUANTILES)
            lines.append(f"| {lookback_name} | {product} | {cells} | {result.fold_count} |")
    return "\n".join(lines)


def _verdict_lines(
    baseline_results: dict[str, dict[str, WalkForwardResult]],
    model_results: dict[str, dict[str, dict[str, WalkForwardResult]]],
) -> list[str]:
    lines = []
    for product in PRODUCTS:
        for lookback_name in LOOKBACKS:
            wins = 0
            total = 0
            for tau in QUANTILES:
                bar_loss = min(
                    baseline_results[name][product].per_quantile_loss[tau]
                    for name in BAR_BASELINE_NAMES
                )
                model_loss = model_results[lookback_name][product].per_quantile_loss[tau]
                total += 1
                if model_loss < bar_loss:
                    wins += 1
            verdict = (
                "BEATS the bar" if wins == total else f"does NOT beat the bar ({wins}/{total} τ)"
            )
            lines.append(f"- **{product} / {lookback_name} lookback**: {verdict}")
    return lines


def _overall_win_rate(
    baseline_results: dict[str, dict[str, WalkForwardResult]],
    model_results: dict[str, dict[str, dict[str, WalkForwardResult]]],
) -> str:
    wins = 0
    total = 0
    for product in PRODUCTS:
        for tau in QUANTILES:
            bar_loss = min(
                baseline_results[name][product].per_quantile_loss[tau]
                for name in BAR_BASELINE_NAMES
            )
            for lookback_name in LOOKBACKS:
                total += 1
                model_loss = model_results[lookback_name][product].per_quantile_loss[tau]
                if model_loss < bar_loss:
                    wins += 1
    return f"{wins}/{total} (product, τ, lookback) cells beat the bar"


def main() -> None:
    db = DatabaseManager()
    end = datetime.now(UTC)

    max_lookback = max(LOOKBACKS.values())

    # Folds over the full common window (identical constant to P2's own
    # report -- see module docstring) so fold cadence/boundaries match P2's
    # convention; only used to derive the headline (trailing 12mo) subset --
    # see module docstring for why the underlying data fetch is bounded
    # separately from this.
    folds = walk_forward_folds(COMMON_WINDOW_START, end)
    headline_folds = trailing_folds(folds, HEADLINE_SPAN)
    if not headline_folds:
        raise RuntimeError("trailing_folds returned no folds for the headline window")

    fetch_start = max(COMMON_WINDOW_START, headline_folds[0].train_end - max_lookback)
    logger.info(
        "headline folds: %d, span [%s, %s]; data fetch window [%s, %s]",
        len(headline_folds),
        headline_folds[0].train_start,
        headline_folds[-1].test_end,
        fetch_start,
        end,
    )

    # B1 is an EXPANDING baseline (design/`shared/baselines.py`: residual
    # quantiles over the WHOLE `[fold.train_start, fold.train_end)`, and
    # every fold's `train_start` is `COMMON_WINDOW_START` -- `walk_forward_
    # folds`'s own expanding-window construction). Feeding it a target series
    # bounded to `fetch_start` (2024+) instead of the true `COMMON_WINDOW_
    # START` (2021-12-31) would silently change what "B1" means here --
    # residual quantiles fit on a truncated ~2.5-year sample rather than the
    # full ~4.6-year one P2's committed report used -- and make the bar not
    # a faithful recomputation of P2's own baseline definition. So the
    # target series fetched for BASELINE fitting spans the full common
    # window (identical to `scripts/generate_baseline_report.py`); the
    # bounded slice used for the MODEL's own dataset (which never looks
    # further back than `max(LOOKBACKS.values())` by design) is derived from
    # it below, not fetched separately.
    target_series_full: dict[str, list[tuple]] = {}
    target_series: dict[str, list[tuple]] = {}
    for product in PRODUCTS:
        # Reused, unmodified §5 coverage gate -- over the full common window,
        # matching `scripts/generate_baseline_report.py` exactly (B1's own
        # training data needs that same full span -- see above).
        fetch_and_assert_daily_coverage(
            db, TARGET_MARKET, TARGET_ZONE, product, COMMON_WINDOW_START, end
        )
        target_series_full[product] = fetch_target_series(db, product, COMMON_WINDOW_START, end)
        target_series[product] = [
            (t, v) for t, v in target_series_full[product] if t >= fetch_start
        ]
        logger.info(
            "product=%s points_full=%d points_bounded_for_model=%d",
            product,
            len(target_series_full[product]),
            len(target_series[product]),
        )

    logger.info(
        "building features via build_features(db, 'DK2', %s, %s, horizon=%s)",
        fetch_start,
        end,
        FEATURE_HORIZON,
    )
    feature_rows = build_features(db, TARGET_ZONE, fetch_start, end, horizon=FEATURE_HORIZON)
    logger.info("feature rows: %d", len(feature_rows))

    # --- baselines, recomputed fresh on headline_folds, on the FULL common
    # window (module docstring: B1 is expanding, needs the true history) ---
    baseline_results: dict[str, dict[str, WalkForwardResult]] = {}
    for name, fit_fn in _baseline_configs().items():
        baseline_results[name] = {}
        for product in PRODUCTS:
            baseline_results[name][product] = run_walk_forward(
                target_series_full[product], headline_folds, fit_fn
            )

    # --- model, both lookbacks, per-fold refit ---
    model_config = ForecastModelConfig()
    model_results: dict[str, dict[str, WalkForwardResult]] = {}
    for lookback_name, lookback in LOOKBACKS.items():
        model_results[lookback_name] = {}
        for product in PRODUCTS:
            dataset = join_features_and_target(feature_rows, target_series[product])
            logger.info(
                "product=%s lookback=%s joined rows=%d dropped_no_target=%d dropped_no_feature=%d",
                product,
                lookback_name,
                len(dataset.times),
                dataset.dropped_no_target,
                dataset.dropped_no_feature,
            )
            model_results[lookback_name][product] = run_model_walk_forward(
                dataset, headline_folds, lookback, model_config
            )

    generated_at = datetime.now(UTC).isoformat()
    lines = [
        "# M6 P3 model results: quantile LightGBM vs the P2 bar",
        "",
        f"Generated {generated_at} by `scripts/generate_forecast_report.py` against the live",
        "database (`docs/forecast-model-design.md` §6). FCR-D DK2 capacity price, both",
        "directions, walk-forward CV -- identical fold generator/config to P2",
        "(`shared/baselines.py`'s `walk_forward_folds`: 90-day minimum initial train span,",
        "30-day test folds, 30-day step), evaluated on the trailing-12-month headline window",
        "(`trailing_folds(folds, timedelta(days=365))`) -- the same window as",
        "`docs/forecast-baseline-results.md`. Per-fold refit throughout: the model is a fresh",
        "LightGBM quantile fit per fold, on that fold's own bounded trailing lookback window",
        "only (design §3), never global.",
        "",
        f"**Window**: headline folds span `[{headline_folds[0].train_start.date()}, "
        f"{headline_folds[-1].test_end.date()}]`, {len(headline_folds)} folds. Feature fetch",
        f"window: `[{fetch_start.date()}, {end.date()}]` (bounded to the longest declared",
        "lookback before the earliest headline fold). Target fetch window (for B1's own",
        f"expanding fit, and for the model's dataset): the FULL common window, "
        f"`[{COMMON_WINDOW_START.date()}, {end.date()}]` -- see module docstring.",
        "",
        "## Headline: model vs the bar (`min(B1 t-24h, B1 t-168h, B2-rolling)`), per (product, τ)",
        "",
        "`beats_bar` is `yes` only if the model's pinball loss is strictly lower than the",
        "strongest of the three bar baselines, for that exact (product, τ, lookback).",
        "",
        _format_headline_table(baseline_results, model_results),
        "",
        f"**Overall**: {_overall_win_rate(baseline_results, model_results)}.",
        "",
        "## Verdict",
        "",
        *_verdict_lines(baseline_results, model_results),
        "",
        "## Model, full detail (both lookbacks)",
        "",
        _format_model_table(model_results),
        "",
        "## Baselines, recomputed fresh on this run's exact headline folds (for reference)",
        "",
        "B1 (both lags) and B2-rolling only -- the three bar candidates (design §0/§6). B2-",
        "expanding is not recomputed here: P2 already established it as a strawman (trained",
        "across FCR-D DK2's ~92% price collapse) and it never contributes to `beats_bar`; see",
        "`docs/forecast-baseline-results.md` for its numbers on the full historical window.",
        "",
        _format_full_baseline_table(baseline_results),
        "",
    ]

    output_path = Path(__file__).resolve().parent.parent / "docs" / "forecast-model-results.md"
    output_path.write_text("\n".join(lines) + "\n")
    logger.info("wrote %s", output_path)


if __name__ == "__main__":
    main()
