#!/usr/bin/env python
"""
Reality-check harness for `shared/bess_economics.py` (see the BESS
net-profit cost-layer plan this script was built from). Runs the
co-optimizer (`shared/bess_simulator.py:run_backtest`,
`strategy="cooptimized"`, `foresight="forecast"` -- the realistic,
deployable policy, not the perfect-foresight oracle) for a real 2 MW/4 MWh
DK2 asset over April/May/June 2026, applies `compute_pnl`'s cost stack, and
tabulates the model's P&L against that asset's real retailer-billed
actuals for the same months.

Usage (needs `DATABASE_URL` pointed at a reachable Postgres/TimescaleDB
instance with those months backfilled):

    DATABASE_URL=postgresql://postgres:secret@localhost:5433/energy \\
        PYTHONPATH=. poetry run python scripts/bess_reality_check.py

**Read-only.** Every backtest goes through `run_backtest` directly (which
only reads `market_data`/`market_data_history`); this script never calls
`db.save_bess_run` -- same posture as
`scripts/generate_cooptimizer_ab_report.py`.

**Config.** `_with_zone_capacity_markets(_with_cycle_cap(base, 1.5), "DK2")`
-- the exact same config-construction helpers (imported, not reimplemented)
`shared/bess_estimator.py`'s Morning Brief and
`scripts/generate_cooptimizer_ab_report.py` both use for a DK2 illustrative
battery, so this reality-check answers "how wrong is the number the rest
of the codebase would compute for this exact asset", not a hypothetical
config nobody runs.

**Real actuals.** Hardcoded from the asset's retailer/Aconto billing for
Apr/May/Jun 2026 -- revenue, cost, and profit in EUR (`REAL_ACTUALS`
below). These are the numbers `compute_pnl`'s cost stack and
`implied_spot_cut`'s back-solved spot-cut are checked against.

**What "success" looks like** (plan's verification §2): the implied
spot-cut should land in a tight, plausible band across all three months
(a real "fixed cut on spot"), and the model-gross-revenue/real-revenue
ratio should be stable -- a stable ratio says the revenue side is
trustworthy even if the absolute level differs; divergence in either
figure says which side (revenue modelling vs. cost-stack assumptions)
still needs work.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.bess_economics import (  # noqa: E402
    RetailerCostConfig,
    compute_pnl,
    implied_spot_cut,
)
from shared.bess_estimator import (  # noqa: E402
    _with_cycle_cap,
    _with_zone_capacity_markets,
)
from shared.bess_simulator import BacktestResult, BessConfig, run_backtest  # noqa: E402
from shared.db_manager import DatabaseManager  # noqa: E402
from shared.logging_config import configure_logging  # noqa: E402

configure_logging()
logger = logging.getLogger(__name__)

ZONE = "DK2"

# Real retailer-billed actuals for this asset, EUR. Revenue/cost/profit are
# independently billed figures (profit = revenue - cost, to the cent
# modulo rounding) -- kept as three separate numbers, not derived, so a
# transcription slip in one is visible against the other two.
REAL_ACTUALS: dict[str, dict[str, float]] = {
    "2026-04": {"revenue": 38102.0, "cost": 11897.0, "profit": 26206.0},
    "2026-05": {"revenue": 52762.0, "cost": 12693.0, "profit": 40069.0},
    "2026-06": {"revenue": 74533.0, "cost": 18599.0, "profit": 55934.0},
}

# [start, end) per month, UTC -- matches `run_backtest`'s half-open window
# convention (see its own docstring / `generate_cooptimizer_ab_report.py`'s
# window discovery).
MONTH_WINDOWS: dict[str, tuple[datetime, datetime]] = {
    "2026-04": (datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC)),
    "2026-05": (datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)),
    "2026-06": (datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
}


def _build_config() -> BessConfig:
    base = BessConfig(
        power_mw=2.0,
        capacity_mwh=4.0,
        strategy="cooptimized",
        foresight="forecast",
    )
    return _with_zone_capacity_markets(_with_cycle_cap(base, 1.5), ZONE)


def _fmt(x: float) -> str:
    return f"{x:,.0f}"


def _fmt_pct(x: float) -> str:
    if x != x:  # NaN
        return "n/a"
    return f"{x * 100:,.1f}%"


def main() -> None:
    db = DatabaseManager()
    config = _build_config()
    cost_config = RetailerCostConfig()

    for month_label, (start, end) in MONTH_WINDOWS.items():
        real = REAL_ACTUALS[month_label]

        result: BacktestResult = run_backtest(db, ZONE, start, end, config)
        if not result.ticks:
            print(f"\n=== {month_label} ({ZONE}) ===")
            print("NO DATA")
            continue

        pnl = compute_pnl(result, cost_config)
        cut = implied_spot_cut(result, cost_config, real["cost"])
        revenue_ratio = pnl.gross_revenue / real["revenue"] if real["revenue"] else float("nan")
        profit_delta_pct = (
            (pnl.net_profit - real["profit"]) / real["profit"] if real["profit"] else float("nan")
        )

        print(f"\n=== {month_label} ({ZONE}, 2 MW / 4 MWh, cooptimized/forecast) ===")
        print(f"  gross_discharge_income   {_fmt(pnl.gross_discharge_income):>12} EUR")
        print(f"  capacity_income          {_fmt(pnl.capacity_income):>12} EUR")
        print(f"  gross_revenue (model)    {_fmt(pnl.gross_revenue):>12} EUR")
        print(f"  gross_revenue (real)     {_fmt(real['revenue']):>12} EUR")
        print(f"  revenue ratio (model/real) {revenue_ratio:>10.3f}")
        print(f"  tariff_cost              {_fmt(pnl.tariff_cost):>12} EUR")
        print(f"  service_fee_cost         {_fmt(pnl.service_fee_cost):>12} EUR")
        print(f"  electricity_cost         {_fmt(pnl.electricity_cost):>12} EUR")
        print(f"  net_profit (model)       {_fmt(pnl.net_profit):>12} EUR")
        print(f"  profit (real)            {_fmt(real['profit']):>12} EUR")
        print(f"  profit delta             {_fmt_pct(profit_delta_pct):>12}")
        print(f"  implied_spot_cut         {cut:>12.2f} EUR/MWh")


if __name__ == "__main__":
    main()
