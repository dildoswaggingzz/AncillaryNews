#!/usr/bin/env python
"""
Generates `docs/forecast-baseline-results.md` -- the committed pinball-loss
bar (design §6) for FCR-D DK2, from the real database, per
`shared/baselines.py`. Not part of any always-on service loop -- run this
manually whenever the baseline numbers need refreshing (matching
`scripts/backfill_history.py`'s "occasional CLI" posture).

Usage (needs `DATABASE_URL` pointed at a reachable Postgres/TimescaleDB
instance):

    DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \\
        poetry run python scripts/generate_baseline_report.py

**Common window for both products** (coordinator directive, superseding
this script's original per-product-window approach): both `up` and `down`
are scored on one identical window, `COMMON_WINDOW_START` (2021-12-31) to
present, with identical walk-forward folds. `up` is complete from
2021-12-01 (design §1's own claim, verified true); `down` has no data for
its first 30 days (2021-12-01..2021-12-30 inclusive), so the common window
starts at `down`'s true first day instead of `up`'s. This costs `up` 30
days (1.8% of its history) in exchange for every number in this report
being directly, fold-for-fold comparable across products. The §5 coverage
gate still runs, unmodified and un-caught, against `COMMON_WINDOW_START`
for both products before anything is fit -- if it ever fails again (a new
gap), this script raises and produces no report, exactly as before; there
is no silent-adjustment fallback path any more (that logic, when it
existed here, is gone -- see `shared/baselines.py`'s module docstring).

**Two reported time windows, not one** (coordinator directive: "the
headline bar must be regime-recent, not a 4.6-year average"). FCR-D DK2's
mean clearing price has fallen by roughly an order of magnitude since 2021
(battery-fleet growth cannibalising a market with fixed TSO demand volume;
see the generated document's yearly-means table, computed directly from the
same series fed to the baselines, not a separate query). A pinball loss
averaged over 2021-2026 is dominated by a market that no longer exists and
is not a meaningful bar for a P3 model being deployed into 2026 conditions.
So this script reports:

- **Headline**: pinball loss over `shared.baselines.trailing_folds`' last
  12 months of folds only -- the number P3 must clear.
- **Secondary**: the full-history (2021-12-31 to present) average, labelled
  as spanning a regime change.
- **Per-fold-over-time**: every fold's own (quantile-averaged) pinball
  loss, so the regime shift is visible in the numbers, not only asserted.

**B2 has two variants, both reported** (coordinator directive: an
expanding climatology trained on ~€70 prices to predict ~€5 ones is a
strawman, not a hard baseline) -- `fit_conditional_climatology` (expanding)
and `fit_conditional_climatology_rolling` (180-day trailing window) --
reported side by side deliberately, since the *gap* between them is the
evidence for the regime shift.
"""

import logging
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.baselines import (  # noqa: E402
    QUANTILES,
    SEASONAL_NAIVE_LAGS,
    TARGET_MARKET,
    TARGET_ZONE,
    Fold,
    WalkForwardResult,
    fetch_and_assert_daily_coverage,
    fetch_target_series,
    fit_conditional_climatology,
    fit_conditional_climatology_rolling,
    fit_seasonal_naive,
    run_walk_forward,
    trailing_folds,
    walk_forward_folds,
)
from shared.db_manager import DatabaseManager  # noqa: E402
from shared.logging_config import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

# `down`'s true first day (design §1 claims 2021-12-01 for both; verified
# false for `down` -- see module docstring). Coordinator directive: use this
# ONE common start for both products rather than a per-product window.
COMMON_WINDOW_START = datetime(2021, 12, 31, tzinfo=UTC)

HEADLINE_SPAN = timedelta(days=365)

PRODUCTS: tuple[str, ...] = ("up", "down")

BaselineFn = object  # fit_fn(series, train_start, train_end) -> object with .predict(t, series_map)


def _baseline_configs() -> dict[str, BaselineFn]:
    """
    Every baseline config this report scores, name -> `fit_fn` (design §3's
    B1 t-24h/t-168h "report both", plus the coordinator's B2
    expanding/rolling pair -- see module docstring).
    """
    configs: dict[str, BaselineFn] = {}
    for lag_name, lag in SEASONAL_NAIVE_LAGS.items():
        configs[f"B1 seasonal-naive ({lag_name})"] = lambda s, a, b, lag=lag: fit_seasonal_naive(
            s, a, b, lag
        )
    configs["B2 conditional climatology (expanding)"] = fit_conditional_climatology
    configs["B2 conditional climatology (rolling 180d)"] = fit_conditional_climatology_rolling
    return configs


def _yearly_stats(series: list[tuple[datetime, float]]) -> dict[int, tuple[float, int]]:
    """
    `{year: (mean, point_count)}`, computed directly from the same
    (already deduped, gate-verified) series fed to the baselines -- not a
    separate query -- so this report's regime-shift evidence can't drift
    out of sync with the numbers it's next to. `point_count` is reported
    alongside every mean because the window's own boundary years are
    partial (2021: one day only, since `COMMON_WINDOW_START` is
    2021-12-31; the current year: partial, through "present") and a bare
    mean would silently misrepresent them as full-year figures.
    """
    by_year: dict[int, list[float]] = defaultdict(list)
    for t, value in series:
        by_year[t.year].append(value)
    return {
        year: (statistics.mean(values), len(values)) for year, values in sorted(by_year.items())
    }


# ~91% of a non-leap 8760-hour year -- "not a partial-year fragment".
FULL_YEAR_POINT_THRESHOLD = 8000


def _mean_across_quantiles(per_quantile: dict[float, float]) -> float:
    # Drop NaN (an empty fold's per-quantile loss -- shouldn't occur here, but tolerated).
    values = [v for v in per_quantile.values() if v == v]
    return statistics.mean(values) if values else float("nan")


def _format_loss_table(
    results: dict[str, dict[str, dict[str, WalkForwardResult]]], window_key: str
) -> str:
    """`window_key` selects `.per_quantile_loss` from either the headline or full-history result."""
    lines = [
        "| baseline | product | " + " | ".join(f"τ={tau}" for tau in QUANTILES) + " | fold count |",
        "|---" * (3 + len(QUANTILES)) + "|",
    ]
    for baseline_name, per_product in results.items():
        for product, by_window in per_product.items():
            result = by_window[window_key]
            cells = " | ".join(f"{result.per_quantile_loss[tau]:.4f}" for tau in QUANTILES)
            lines.append(f"| {baseline_name} | {product} | {cells} | {result.fold_count} |")
    return "\n".join(lines)


def _format_per_fold_table(
    folds: list[Fold], results: dict[str, dict[str, dict[str, WalkForwardResult]]]
) -> str:
    """
    Fold-start-date -> quantile-averaged pinball loss, one column per
    (baseline, product) pair, so the regime shift is visible directly in
    the numbers (coordinator directive) rather than only in the yearly-
    means table.
    """
    columns = [(name, product) for name in results for product in results[name]]
    header = ["| fold test_start | " + " | ".join(f"{n} / {p}" for n, p in columns) + " |"]
    header.append("|---" * (1 + len(columns)) + "|")
    rows = []
    for i, fold in enumerate(folds):
        cells = []
        for name, product in columns:
            per_fold_losses = results[name][product]["full"].per_fold_quantile_loss[i]
            cells.append(f"{_mean_across_quantiles(per_fold_losses):.4f}")
        rows.append(f"| {fold.test_start.date()} | " + " | ".join(cells) + " |")
    return "\n".join(header + rows)


def main() -> None:
    db = DatabaseManager()
    end = datetime.now(UTC)

    series_by_product: dict[str, list[tuple[datetime, float]]] = {}
    for product in PRODUCTS:
        # Strict, un-caught -- design §5's gate stays exactly as strict as
        # `shared/baselines.py`'s own tests exercise it (see module docstring).
        fetch_and_assert_daily_coverage(
            db, TARGET_MARKET, TARGET_ZONE, product, COMMON_WINDOW_START, end
        )
        series_by_product[product] = fetch_target_series(db, product, COMMON_WINDOW_START, end)
        logger.info(
            "product=%s window=[%s, %s] points=%d",
            product,
            COMMON_WINDOW_START,
            end,
            len(series_by_product[product]),
        )

    # One shared folds list -- both products use the identical common window,
    # so there is exactly one walk-forward schedule, not one per product.
    folds = walk_forward_folds(COMMON_WINDOW_START, end)
    headline_folds = trailing_folds(folds, HEADLINE_SPAN)
    if not headline_folds:
        raise RuntimeError(
            "trailing_folds returned no folds for the headline window -- check HEADLINE_SPAN"
        )

    results: dict[str, dict[str, dict[str, WalkForwardResult]]] = {}
    for baseline_name, fit_fn in _baseline_configs().items():
        results[baseline_name] = {}
        for product, series in series_by_product.items():
            full_result = run_walk_forward(series, folds, fit_fn)
            headline_result = run_walk_forward(series, headline_folds, fit_fn)
            results[baseline_name][product] = {"full": full_result, "headline": headline_result}

    yearly_stats = {product: _yearly_stats(series) for product, series in series_by_product.items()}
    all_years = sorted({year for stats in yearly_stats.values() for year in stats})

    generated_at = datetime.now(UTC).isoformat()
    lines = [
        "# M6 P2 baseline results: the pinball-loss bar P3 must clear",
        "",
        f"Generated {generated_at} by `scripts/generate_baseline_report.py` against the live",
        "database (`docs/forecast-baseline-design.md` §6). FCR-D DK2 capacity price, both",
        "directions, walk-forward CV (90-day minimum initial train span, 30-day test folds,",
        "30-day step -- design §4). Every baseline's parameters are fit per fold, on that",
        "fold's training window only (`shared/baselines.py`).",
        "",
        "## Window",
        "",
        f"**Common window for both products**: `[{COMMON_WINDOW_START.date()}, {end.date()}]`,",
        f"{len(folds)} walk-forward folds, identical for `up` and `down`. `down` has no data for",
        '2021-12-01..2021-12-30 inclusive (the design doc\'s §1 claim of "0 missing days of 1693"',
        "is true for `up`, false for `down` -- caught live by this module's own §5 coverage gate,",
        "run strictly against this window before anything below was fit). The window starts at",
        "`down`'s true first day rather than `up`'s, costing `up` ~30 days (1.8% of its history),",
        "so every number below is directly comparable fold-for-fold across both products.",
        "",
        "## FCR-D DK2 has undergone an order-of-magnitude structural decline",
        "",
        "Battery-fleet growth has been cannibalising a market with a fixed TSO demand volume.",
        "Mean clearing price by year, computed directly from the series scored below (not a",
        "separate query):",
        "",
        "| year | " + " | ".join(f"`{p}` mean (n)" for p in PRODUCTS) + " |",
        "|---" * (1 + len(PRODUCTS)) + "|",
    ]
    for year in all_years:
        cells = []
        for p in PRODUCTS:
            if year in yearly_stats[p]:
                mean, count = yearly_stats[p][year]
                cells.append(f"€{mean:.2f} (n={count})")
            else:
                cells.append("—")
        lines.append(f"| {year} | " + " | ".join(cells) + " |")

    # First/last calendar years in this window are partial (2021: one day,
    # since COMMON_WINDOW_START is 2021-12-31; the current year: through
    # "present") -- the decline figure below uses the first FULL year
    # instead of the window's first (partial) year, so it isn't distorted
    # by a single unrepresentative day.
    full_years = [
        y for y in all_years if yearly_stats["up"].get(y, (0, 0))[1] >= FULL_YEAR_POINT_THRESHOLD
    ]
    baseline_year = full_years[0] if full_years else all_years[0]
    latest_year = all_years[-1]
    up_baseline = yearly_stats["up"][baseline_year][0]
    up_latest = yearly_stats["up"][latest_year][0]
    decline_pct = (1 - up_latest / up_baseline) * 100 if up_baseline else float("nan")
    lines += [
        "",
        f"(n) is the point count backing each mean -- {all_years[0]} is a single partial day",
        f"({COMMON_WINDOW_START.date()} only, since that's the common window's start) and",
        f"{latest_year} is partial too (through {end.date()}); neither is a full-year figure.",
        "",
        f"`up` fell {decline_pct:.0f}% from {baseline_year} (first full year in this window) to",
        f"{latest_year}. This means **long history is not straightforwardly an asset for level",
        "prediction** here -- a baseline (or a P3 model) that trains on the full history without",
        "accounting for the trend will be biased high in recent, deployment-relevant conditions.",
        "This motivates both the headline/full-history split and the B2 expanding/rolling split",
        "below.",
        "",
        "## Headline bar (trailing 12 months of folds) -- the number P3 must clear",
        "",
        "Scored on `trailing_folds(folds, timedelta(days=365))` only -- see `shared/baselines.py`.",
        "This is the deployment-relevant bar; the full-history table further down is NOT the bar.",
        "",
        _format_loss_table(results, "headline"),
        "",
        "## Secondary: full-history average (2021-12-31 to present) -- NOT the bar",
        "",
        "Spans the regime change documented above -- dominated by a market that no longer exists.",
        "Reported for completeness/context only; a P3 model should not be judged against this row.",
        "",
        _format_loss_table(results, "full"),
        "",
        "## Per-fold pinball loss over time (quantile-averaged)",
        "",
        "One row per walk-forward fold's test window, so the regime shift documented above is",
        "visible directly in the loss numbers, not only in the yearly-means table. The last 12",
        "rows are exactly the folds the headline bar above is computed from.",
        "",
        _format_per_fold_table(folds, results),
        "",
    ]

    output_path = Path(__file__).resolve().parent.parent / "docs" / "forecast-baseline-results.md"
    output_path.write_text("\n".join(lines) + "\n")
    logger.info("wrote %s", output_path)


if __name__ == "__main__":
    main()
