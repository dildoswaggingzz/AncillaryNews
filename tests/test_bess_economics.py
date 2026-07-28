"""
Tests for `shared/bess_economics.py`. Every fixture is synthetic/hand-built
-- no database, matching `tests/test_economic_eval.py`'s own convention for
a module that is a pure function of a `BacktestResult`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.bess_economics import (
    RetailerCostConfig,
    compute_pnl,
    implied_spot_cut,
)
from shared.bess_simulator import BacktestResult, BessConfig, BessTick
from shared.units import DKK_PER_EUR

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _tick(
    hour: int,
    *,
    soc_mwh: float,
    action: str,
    arbitrage_revenue_dkk: float,
    energy_discharged_mwh: float,
    cumulative_arbitrage_revenue_dkk: float,
    cumulative_capacity_revenue_dkk: float = 0.0,
    cumulative_capacity_revenue_eur: float = 0.0,
    cumulative_afrr_activation_revenue_eur: float = 0.0,
) -> BessTick:
    """Minimal `BessTick` factory -- fills every field `BacktestResult`'s
    properties don't read (per-tick, non-cumulative capacity fields;
    `day_ahead_price`; `capacity_reserved_mw`) with inert defaults, since
    `compute_pnl` only ever reads `arbitrage_revenue_dkk`,
    `energy_discharged_mwh`, `soc_mwh`, and the last tick's cumulative
    fields (via `BacktestResult.total_*` properties)."""
    return BessTick(
        time=BASE + timedelta(hours=hour),
        soc_mwh=soc_mwh,
        soc_fraction=soc_mwh / 4.0,
        action=action,
        day_ahead_price=100.0,
        energy_discharged_mwh=energy_discharged_mwh,
        arbitrage_revenue_dkk=arbitrage_revenue_dkk,
        capacity_reserved_mw=0.0,
        capacity_revenue_dkk=0.0,
        capacity_revenue_by_market={},
        cumulative_arbitrage_revenue_dkk=cumulative_arbitrage_revenue_dkk,
        cumulative_capacity_revenue_dkk=cumulative_capacity_revenue_dkk,
        cumulative_total_revenue_dkk=cumulative_arbitrage_revenue_dkk
        + cumulative_capacity_revenue_dkk,
        cumulative_capacity_revenue_eur=cumulative_capacity_revenue_eur,
        cumulative_afrr_activation_revenue_eur=cumulative_afrr_activation_revenue_eur,
    )


def _result(ticks: list[BessTick], config: BessConfig | None = None) -> BacktestResult:
    return BacktestResult(
        zone="DK2",
        start_time=ticks[0].time,
        end_time=ticks[-1].time,
        config=config or BessConfig(),
        ticks=ticks,
    )


def test_compute_pnl_line_items_synthetic_ticks() -> None:
    """3 hand-built ticks: idle (start SoC), charge (2 MWh in, -100 DKK
    spot cost), discharge (2 MWh out, +200 DKK sale) -- plus a nonzero
    capacity/aFRR-activation tail on the last tick, so every `BessPnl`
    field is exercised, not just the arbitrage split."""
    ticks = [
        _tick(
            0,
            soc_mwh=1.0,
            action="idle",
            arbitrage_revenue_dkk=0.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=0.0,
        ),
        _tick(
            1,
            soc_mwh=3.0,  # +2 MWh charged
            action="charge",
            arbitrage_revenue_dkk=-100.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=-100.0,
        ),
        _tick(
            2,
            soc_mwh=1.0,  # -2 MWh discharged
            action="discharge",
            arbitrage_revenue_dkk=200.0,
            energy_discharged_mwh=2.0,
            cumulative_arbitrage_revenue_dkk=100.0,
            cumulative_capacity_revenue_dkk=74.6,  # 10 EUR at the fixed peg
            cumulative_capacity_revenue_eur=5.0,
            cumulative_afrr_activation_revenue_eur=2.0,
        ),
    ]
    result = _result(ticks)

    pnl = compute_pnl(result)

    assert pnl.volume_mwh == pytest.approx(2.0)
    assert pnl.charged_mwh == pytest.approx(2.0)

    expected_gross_discharge_income = 200.0 / DKK_PER_EUR
    expected_charging_spot_cost = 100.0 / DKK_PER_EUR
    expected_capacity_income = 74.6 / DKK_PER_EUR + 5.0 + 2.0  # == 10 + 5 + 2 == 17.0
    assert pnl.gross_discharge_income == pytest.approx(expected_gross_discharge_income)
    assert pnl.charging_spot_cost == pytest.approx(expected_charging_spot_cost)
    assert pnl.capacity_income == pytest.approx(expected_capacity_income)

    expected_gross_revenue = expected_gross_discharge_income + expected_capacity_income
    assert pnl.gross_revenue == pytest.approx(expected_gross_revenue)

    # Defaults: energy tax reimbursed, VAT excluded -> effective tariff is
    # just the bare grid tariff (21 EUR/MWh), billed on discharged MWh.
    expected_tariff_cost = 2.0 * 21.0
    expected_service_fee_cost = 2.0 * 3.75
    assert pnl.tariff_cost == pytest.approx(expected_tariff_cost)
    assert pnl.service_fee_cost == pytest.approx(expected_service_fee_cost)

    # spot_cut_eur_per_mwh defaults to 0, so electricity_cost is exactly
    # the charging spot cost split out of the ticks above.
    assert pnl.electricity_cost == pytest.approx(expected_charging_spot_cost)

    expected_net_profit = (
        expected_gross_revenue
        - expected_charging_spot_cost
        - expected_tariff_cost
        - expected_service_fee_cost
    )
    assert pnl.net_profit == pytest.approx(expected_net_profit)


def test_compute_pnl_applies_spot_cut_to_charged_mwh() -> None:
    """A nonzero `spot_cut_eur_per_mwh` adds to `electricity_cost` scaled
    by `charged_mwh` (not `volume_mwh`) -- the two are deliberately
    different volumes in this fixture (2 MWh charged, 1 MWh discharged) so
    a bug that used the wrong one would fail this assertion."""
    ticks = [
        _tick(
            0,
            soc_mwh=0.0,
            action="idle",
            arbitrage_revenue_dkk=0.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=0.0,
        ),
        _tick(
            1,
            soc_mwh=2.0,  # +2 MWh charged
            action="charge",
            arbitrage_revenue_dkk=-50.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=-50.0,
        ),
        _tick(
            2,
            soc_mwh=1.0,  # -1 MWh discharged
            action="discharge",
            arbitrage_revenue_dkk=80.0,
            energy_discharged_mwh=1.0,
            cumulative_arbitrage_revenue_dkk=30.0,
        ),
    ]
    result = _result(ticks)
    cost = RetailerCostConfig(spot_cut_eur_per_mwh=10.0)

    pnl = compute_pnl(result, cost)

    assert pnl.charged_mwh == pytest.approx(2.0)
    assert pnl.volume_mwh == pytest.approx(1.0)
    expected_electricity_cost = 50.0 / DKK_PER_EUR + 10.0 * 2.0
    assert pnl.electricity_cost == pytest.approx(expected_electricity_cost)


def test_implied_spot_cut_recovers_known_cut() -> None:
    """Round-trips `implied_spot_cut`: build a `BessPnl` with a known
    `spot_cut_eur_per_mwh`, feed its total cost back in as `real_cost_eur`,
    and check the recovered cut matches the one used to construct it."""
    ticks = [
        _tick(
            0,
            soc_mwh=0.0,
            action="idle",
            arbitrage_revenue_dkk=0.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=0.0,
        ),
        _tick(
            1,
            soc_mwh=4.0,
            action="charge",
            arbitrage_revenue_dkk=-200.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=-200.0,
        ),
        _tick(
            2,
            soc_mwh=0.0,
            action="discharge",
            arbitrage_revenue_dkk=400.0,
            energy_discharged_mwh=4.0,
            cumulative_arbitrage_revenue_dkk=200.0,
        ),
    ]
    result = _result(ticks)
    known_cut = 7.5
    cost = RetailerCostConfig(spot_cut_eur_per_mwh=known_cut)
    pnl = compute_pnl(result, cost)

    real_cost_eur = pnl.electricity_cost + pnl.tariff_cost + pnl.service_fee_cost

    recovered_cut = implied_spot_cut(result, cost, real_cost_eur)
    assert recovered_cut == pytest.approx(known_cut)


def test_implied_spot_cut_guards_zero_charged_mwh() -> None:
    """No charging at all (a pure-discharge or empty run) -> nothing to fit
    a per-MWh cut against; returns 0.0 rather than dividing by zero."""
    ticks = [
        _tick(
            0,
            soc_mwh=2.0,
            action="idle",
            arbitrage_revenue_dkk=0.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=0.0,
        ),
        _tick(
            1,
            soc_mwh=2.0,
            action="idle",
            arbitrage_revenue_dkk=0.0,
            energy_discharged_mwh=0.0,
            cumulative_arbitrage_revenue_dkk=0.0,
        ),
    ]
    result = _result(ticks)

    assert implied_spot_cut(result, RetailerCostConfig(), real_cost_eur=1000.0) == 0.0


def test_aconto_reconciliation_with_tax_and_vat() -> None:
    """Reconciles against the real operator's "Aconto" cost model at 240
    MWh/month (~2 cycles/day for a 4 MWh unit) -- the spreadsheet's own
    rounded rates are 147 EUR/MWh (tariff+tax, VAT-inclusive) and 26
    EUR/MWh (tariff only, tax reimbursed, VAT-inclusive), giving rounded
    totals of 35,223 / 6,230 EUR/mo (module docstring / plan). This test
    asserts the *exact* unrounded arithmetic instead: 21 EUR/MWh grid
    tariff + 96 EUR/MWh energy tax = 117, x1.25 VAT = 146.25 (not the
    spreadsheet's rounded 147); grid tariff alone x1.25 VAT = 26.25 (not
    the rounded 26). 240 MWh x these exact rates are the values asserted
    below -- close to, but not identical to, the spreadsheet's rounded
    35,223 / 6,230 totals."""
    volume_mwh = 240.0
    ticks = [
        _tick(
            0,
            soc_mwh=0.0,
            action="discharge",
            arbitrage_revenue_dkk=0.0,
            energy_discharged_mwh=volume_mwh,
            cumulative_arbitrage_revenue_dkk=0.0,
        ),
    ]
    result = _result(ticks)

    with_tax_and_vat = RetailerCostConfig(energy_tax_reimbursed=False, include_vat=True)
    pnl_with_tax = compute_pnl(result, with_tax_and_vat)
    assert pnl_with_tax.tariff_cost == pytest.approx(240.0 * 146.25)

    reimbursed_with_vat = RetailerCostConfig(energy_tax_reimbursed=True, include_vat=True)
    pnl_reimbursed = compute_pnl(result, reimbursed_with_vat)
    assert pnl_reimbursed.tariff_cost == pytest.approx(240.0 * 26.25)
